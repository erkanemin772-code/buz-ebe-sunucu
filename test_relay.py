"""Protocol tests use ephemeral loopback sockets; no Internet service is created."""
import asyncio
import base64
import contextlib
import json
import time
import unittest
from relay import MAX_LINE, Relay


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.relay = Relay(max_rooms=3, idle_timeout=1, handshake_timeout=.3)
        self.server = await asyncio.start_server(self.relay.accept, "127.0.0.1", 0, limit=MAX_LINE)
        self.port = self.server.sockets[0].getsockname()[1]
        self.connections = []

    async def asyncTearDown(self):
        for _, writer in self.connections:
            writer.close()
        for _, writer in self.connections:
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        self.server.close()
        await self.server.wait_closed()
        await self.relay.close()
        await asyncio.sleep(.01)

    async def connect(self, first):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.connections.append((reader, writer))
        await self.send(writer, first)
        return reader, writer

    async def send(self, writer, message):
        writer.write(json.dumps(message).encode() + b"\n")
        await writer.drain()

    async def read(self, reader):
        return json.loads(await asyncio.wait_for(reader.readline(), 1))

    async def room(self):
        host, host_writer = await self.connect({"type": "host"})
        hosted = await self.read(host)
        guest, guest_writer = await self.connect({"type": "join", "code": hosted["code"]})
        self.assertEqual(await self.approve(host, host_writer, guest), 1)
        return host, host_writer, guest, guest_writer, hosted["code"]

    async def approve(self, host, host_writer, guest):
        pending = await self.read(guest)
        self.assertEqual(pending["type"], "pending")
        request = await self.read(host)
        self.assertEqual(request["type"], "join_request")
        self.assertEqual(request["requestId"], pending["requestId"])
        await self.send(host_writer, {"type": "accept", "requestId": pending["requestId"]})
        joined = await self.read(guest)
        self.assertEqual(joined["type"], "joined")
        notice = await self.read(host)
        self.assertEqual((notice["type"], notice["id"]), ("peer_joined", joined["id"]))
        return joined["id"]

    async def test_room_state_input_and_inactive_player(self):
        host, hw, guest, gw, code = await self.room()
        self.assertEqual(len(code), 8)
        state = {"type": "state", "phase": "playing", "players": [{"x": 1}, {"x": 2}]}
        await self.send(hw, state)
        self.assertEqual(await self.read(guest), state)
        await self.send(gw, {"type": "input", "seq": 0, "x": 7, "z": -9, "drop": True, "active": True})
        packet = await self.read(host)
        self.assertEqual((packet["id"], packet["x"], packet["z"], packet["drop"]), (1, 1, -1, True))
        await self.send(gw, {"type": "input", "seq": 1, "x": 1, "z": 1, "drop": True, "active": False})
        packet = await self.read(host)
        self.assertEqual((packet["x"], packet["z"], packet["drop"], packet["active"]), (0, 0, False, False))

    async def test_full_room_and_peer_replacement(self):
        host, hw, guest, gw, code = await self.room()
        extra, _ = await self.connect({"type": "join", "code": code})
        self.assertEqual((await self.read(extra))["code"], "room_full")
        await self.send(gw, {"type": "leave"})
        self.assertEqual((await self.read(host))["type"], "peer_left")
        replacement, _ = await self.connect({"type": "join", "code": code.lower()})
        self.assertEqual(await self.approve(host, hw, replacement), 1)

    async def test_host_departure_closes_room(self):
        _, hw, guest, _, code = await self.room()
        hw.close()
        self.assertEqual((await self.read(guest))["code"], "host_left")
        newcomer, _ = await self.connect({"type": "join", "code": code})
        self.assertEqual((await self.read(newcomer))["code"], "room_not_found")

    async def test_guest_cannot_publish_state(self):
        host, _, guest, gw, _ = await self.room()
        await self.send(gw, {"type": "state", "players": []})
        self.assertEqual((await self.read(guest))["code"], "wrong_direction")
        self.assertEqual((await self.read(host))["type"], "peer_left")

    async def test_invalid_code_and_oversized_message(self):
        reader, _ = await self.connect({"type": "join", "code": "A"})
        self.assertEqual((await self.read(reader))["code"], "invalid_code")
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.connections.append((reader, writer))
        writer.write(b"x" * (MAX_LINE + 1) + b"\n")
        await writer.drain()
        self.assertEqual((await self.read(reader))["code"], "message_too_large")

    async def test_stale_input_is_ignored_and_ping_works(self):
        host, _, guest, gw, _ = await self.room()
        await self.send(gw, {"type": "input", "seq": 5, "x": 0, "z": 0})
        self.assertEqual((await self.read(host))["seq"], 5)
        await self.send(gw, {"type": "input", "seq": 4, "x": 1, "z": 1})
        await self.send(gw, {"type": "ping"})
        self.assertEqual((await self.read(guest))["type"], "pong")
        await self.send(gw, {"type": "input", "seq": 6, "x": 0, "z": 0})
        self.assertEqual((await self.read(host))["seq"], 6)

    async def test_non_finite_input_rejected(self):
        host, _, guest, gw, _ = await self.room()
        await self.send(gw, {"type": "input", "seq": 0, "x": float("nan"), "z": 0})
        self.assertEqual((await self.read(guest))["code"], "bad_message")
        self.assertEqual((await self.read(host))["type"], "peer_left")

    async def test_handshake_timeout(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.connections.append((reader, writer))
        self.assertEqual((await self.read(reader))["code"], "timeout")

    async def test_oversized_numeric_input_rejected(self):
        host, _, guest, gw, _ = await self.room()
        await self.send(gw, {"type": "input", "seq": 0, "x": 10**1000, "z": 0})
        self.assertEqual((await self.read(guest))["code"], "bad_input")
        self.assertEqual((await self.read(host))["type"], "peer_left")

    async def test_rate_limit_disconnects_only_abusive_peer(self):
        host, hw, guest, gw, _ = await self.room()
        peer = next(p for p in self.relay.peers if not p.is_host)
        peer.tokens = 0
        peer.rate_at = time.monotonic()
        await self.send(gw, {"type": "ping"})
        self.assertEqual((await self.read(guest))["code"], "rate_limited")
        self.assertEqual((await self.read(host))["type"], "peer_left")
        await self.send(hw, {"type": "ping"})
        self.assertEqual((await self.read(host))["type"], "pong")

    async def test_live_room_listing_names_full_rooms_and_host_departure(self):
        browser, browser_writer = await self.connect({"type": "list"})
        self.assertEqual((await self.read(browser))["rooms"], [])
        host, host_writer = await self.connect({"type": "host", "name": "Buz Pisti\n"})
        hosted = await self.read(host)
        await self.send(browser_writer, {"type": "list"})
        listed = await self.read(browser)
        self.assertEqual(listed["rooms"], [{"code": hosted["code"], "name": "Buz Pisti", "players": 1, "capacity": 2}])
        guest, _ = await self.connect({"type": "join", "code": hosted["code"]})
        await self.approve(host, host_writer, guest)
        await self.send(browser_writer, {"type": "list"})
        self.assertEqual((await self.read(browser))["rooms"][0]["players"], 2)
        host_writer.close()
        await self.read(guest)
        await self.send(browser_writer, {"type": "list"})
        self.assertEqual((await self.read(browser))["rooms"], [])

    async def test_listing_pagination_is_bounded(self):
        self.relay.max_rooms = 100
        self.relay.max_connections = 300
        for i in range(70):
            reader, _ = await self.connect({"type": "host", "name": "🧊" * 32})
            await self.read(reader)
        browser, writer = await self.connect({"type": "list"})
        page = await self.read(browser)
        self.assertEqual((len(page["rooms"]), page["next"]), (64, 64))
        await self.send(writer, {"type": "list", "cursor": page["next"]})
        page = await self.read(browser)
        self.assertEqual((len(page["rooms"]), page["next"]), (6, None))

    async def test_browser_can_join_selected_room_without_retyping_code(self):
        host, host_writer = await self.connect({"type": "host", "name": "Arkadaşım"})
        await self.read(host)
        browser, writer = await self.connect({"type": "list"})
        code = (await self.read(browser))["rooms"][0]["code"]
        await self.send(writer, {"type": "join", "code": code})
        self.assertEqual(await self.approve(host, host_writer, browser), 1)

    async def test_five_players_broadcast_inputs_and_disconnected_slot_reuse(self):
        host, hw = await self.connect({"type": "host", "capacity": 5})
        hosted = await self.read(host)
        self.assertEqual(hosted["capacity"], 5)
        guests = []
        for slot in range(1, 5):
            guest, writer = await self.connect({"type": "join", "code": hosted["code"], "name": f"Oyuncu {slot}"})
            self.assertEqual(await self.approve(host, hw, guest), slot)
            guests.append((guest, writer))
        state = {"type": "state", "players": [{"score": slot} for slot in range(5)]}
        await self.send(hw, state)
        for slot, (guest, writer) in enumerate(guests, 1):
            self.assertEqual(await self.read(guest), state)
            await self.send(writer, {"type": "input", "seq": 0, "x": 0, "z": 0})
            self.assertEqual((await self.read(host))["id"], slot)
        extra, _ = await self.connect({"type": "join", "code": hosted["code"]})
        self.assertEqual((await self.read(extra))["code"], "room_full")
        guests[1][1].close()
        self.assertEqual((await self.read(host))["id"], 2)
        replacement, _ = await self.connect({"type": "join", "code": hosted["code"]})
        self.assertEqual(await self.approve(host, hw, replacement), 2)
        self.assertEqual(await self.read(replacement), state)

    async def test_pending_is_not_counted_and_host_can_reject(self):
        host, hw = await self.connect({"type": "host", "capacity": 5})
        code = (await self.read(host))["code"]
        guest, _ = await self.connect({"type": "join", "code": code, "name": "Patenli"})
        pending = await self.read(guest)
        request = await self.read(host)
        self.assertEqual(request["name"], "Patenli")
        browser, _ = await self.connect({"type": "list"})
        self.assertEqual((await self.read(browser))["rooms"][0]["players"], 1)
        await self.send(hw, {"type": "reject", "requestId": pending["requestId"]})
        self.assertEqual((await self.read(guest))["code"], "rejected")
        await self.send(hw, {"type": "ping"})
        self.assertEqual((await self.read(host))["type"], "pong")

    async def test_pending_input_is_blocked_and_request_removed(self):
        host, _ = await self.connect({"type": "host"})
        code = (await self.read(host))["code"]
        guest, gw = await self.connect({"type": "join", "code": code})
        pending = await self.read(guest)
        await self.read(host)
        await self.send(gw, {"type": "input", "seq": 0, "x": 1, "z": 1})
        self.assertEqual((await self.read(guest))["code"], "approval_required")
        self.assertEqual(await self.read(host), {"type": "request_left", "requestId": pending["requestId"]})

    async def test_invalid_capacity_rejected(self):
        for capacity in (1, 6, True, "5", 2.5):
            host, _ = await self.connect({"type": "host", "capacity": capacity})
            self.assertEqual((await self.read(host))["code"], "bad_capacity")

    async def test_capacity_race_declines_late_accept_without_closing_host(self):
        host, hw = await self.connect({"type": "host", "capacity": 2})
        code = (await self.read(host))["code"]
        pending_guests = []
        for _ in range(2):
            guest, _ = await self.connect({"type": "join", "code": code})
            pending = await self.read(guest)
            await self.read(host)
            pending_guests.append((guest, pending["requestId"]))
        await self.send(hw, {"type": "accept", "requestId": pending_guests[0][1]})
        self.assertEqual((await self.read(pending_guests[0][0]))["type"], "joined")
        await self.read(host)
        await self.send(hw, {"type": "accept", "requestId": pending_guests[1][1]})
        self.assertEqual((await self.read(pending_guests[1][0]))["code"], "room_full")
        self.assertEqual((await self.read(host))["type"], "request_error")
        await self.send(hw, {"type": "ping"})
        self.assertEqual((await self.read(host))["type"], "pong")

    async def test_voice_broadcast_excludes_sender_pending_and_overwrites_identity(self):
        host, hw = await self.connect({"type": "host", "capacity": 4})
        code = (await self.read(host))["code"]
        guests = []
        for slot in (1, 2):
            guest, writer = await self.connect({"type": "join", "code": code})
            self.assertEqual(await self.approve(host, hw, guest), slot)
            guests.append((guest, writer))
        pending, pending_writer = await self.connect({"type": "join", "code": code})
        await self.read(pending)
        await self.read(host)
        data = base64.b64encode(b"\x00" * 1280).decode()
        await self.send(guests[0][1], {"type": "voice", "data": data, "from": 0, "id": 0})
        expected = {"type": "voice", "data": data, "from": 1}
        self.assertEqual(await self.read(host), expected)
        self.assertEqual(await self.read(guests[1][0]), expected)
        await self.send(guests[0][1], {"type": "ping"})
        self.assertEqual((await self.read(guests[0][0]))["type"], "pong")
        await self.send(pending_writer, {"type": "ping"})
        self.assertEqual((await self.read(pending))["type"], "pong")
        await self.send(hw, {"type": "voice", "data": data})
        for guest, _ in guests:
            self.assertEqual((await self.read(guest))["from"], 0)
        self.assertIsNone(self.relay.rooms[code].snapshot)

    async def test_pending_voice_is_denied(self):
        host, _ = await self.connect({"type": "host"})
        code = (await self.read(host))["code"]
        guest, gw = await self.connect({"type": "join", "code": code})
        pending = await self.read(guest)
        await self.read(host)
        await self.send(gw, {"type": "voice", "data": base64.b64encode(b"\x00" * 1280).decode()})
        self.assertEqual((await self.read(guest))["code"], "approval_required")
        self.assertEqual(await self.read(host), {"type": "request_left", "requestId": pending["requestId"]})

    async def test_malformed_and_oversized_voice_packets_are_rejected(self):
        host, hw = await self.connect({"type": "host"})
        code = (await self.read(host))["code"]
        for data in ("not base64", "A" * 1801, base64.b64encode(b"short").decode(), "🧊"):
            guest, gw = await self.connect({"type": "join", "code": code})
            await self.approve(host, hw, guest)
            await self.send(gw, {"type": "voice", "data": data})
            self.assertEqual((await self.read(guest))["code"], "bad_voice")
            self.assertEqual((await self.read(host))["type"], "peer_left")

    async def test_voice_stops_for_departed_players(self):
        host, hw = await self.connect({"type": "host", "capacity": 3})
        code = (await self.read(host))["code"]
        first, fw = await self.connect({"type": "join", "code": code})
        await self.approve(host, hw, first)
        second, sw = await self.connect({"type": "join", "code": code})
        await self.approve(host, hw, second)
        await self.send(fw, {"type": "leave"})
        self.assertEqual((await self.read(host))["id"], 1)
        self.assertEqual(await asyncio.wait_for(first.readline(), 1), b"")
        data = base64.b64encode(b"\x01" * 1280).decode()
        await self.send(sw, {"type": "voice", "data": data})
        self.assertEqual((await self.read(host))["from"], 2)
        self.assertEqual(set(self.relay.rooms[code].guests), {2})


if __name__ == "__main__":
    unittest.main()
