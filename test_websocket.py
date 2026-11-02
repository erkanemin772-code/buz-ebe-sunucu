import asyncio
import contextlib
import json
import unittest
from relay import MAX_LINE, Relay, websocket_server

try:
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed, InvalidStatus
except ImportError:
    connect = None


@unittest.skipUnless(connect, "Install relay/requirements.txt to test WebSocket transport")
class WebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.relay = Relay(max_rooms=5)
        self.server = await websocket_server(self.relay, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{self.port}/ws"
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()
        self.server.close()
        await self.server.wait_closed()
        await self.relay.close()

    async def client(self, first):
        client = await connect(self.url, compression=None, proxy=None)
        self.clients.append(client)
        await client.send(json.dumps(first))
        return client

    async def read(self, client):
        return json.loads(await asyncio.wait_for(client.recv(), 1))

    async def test_room_listing_join_and_authoritative_frames(self):
        host = await self.client({"type": "host", "name": "İstanbul Buz"})
        hosted = await self.read(host)
        browser = await self.client({"type": "list"})
        listed = await self.read(browser)
        self.assertEqual(listed["rooms"][0]["name"], "İstanbul Buz")
        guest = await self.client({"type": "join", "code": listed["rooms"][0]["code"]})
        pending = await self.read(guest)
        self.assertEqual(pending["type"], "pending")
        self.assertEqual((await self.read(host))["type"], "join_request")
        await host.send(json.dumps({"type": "accept", "requestId": pending["requestId"]}))
        self.assertEqual((await self.read(guest))["type"], "joined")
        self.assertEqual((await self.read(host))["type"], "peer_joined")
        state = {"type": "state", "players": [{"score": 9}, {"score": 3}]}
        await host.send(json.dumps(state))
        self.assertEqual(await self.read(guest), state)
        await guest.send(json.dumps({"type": "input", "seq": 1, "x": .5, "z": 0, "active": False}))
        self.assertFalse((await self.read(host))["active"])
        await browser.send('{"type":"list"}')
        self.assertEqual((await self.read(browser))["rooms"][0]["players"], 2)
        await host.close()
        self.assertEqual((await self.read(guest))["code"], "host_left")
        await browser.send('{"type":"list"}')
        self.assertEqual((await self.read(browser))["rooms"], [])

    async def test_http_health_and_wrong_path(self):
        for path, status in (("/", b"200"), ("/other", b"404")):
            reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
            writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode())
            await writer.drain()
            self.assertIn(status, await reader.readline())
            writer.close()
            await writer.wait_closed()

    async def test_browser_origin_is_rejected(self):
        with self.assertRaises(InvalidStatus) as failure:
            await connect(self.url, origin="https://unrelated.example", proxy=None)
        self.assertEqual(failure.exception.response.status_code, 403)

    async def test_fragmented_text_accepted_and_binary_rejected(self):
        client = await connect(self.url, compression=None, proxy=None)
        self.clients.append(client)
        await client.send(['{"type":', '"list"}'])
        self.assertEqual((await self.read(client))["type"], "rooms")
        await client.send(b'{"type":"list"}')
        self.assertEqual((await self.read(client))["code"], "bad_message")

    async def test_oversized_websocket_frame_is_closed(self):
        client = await connect(self.url, compression=None, proxy=None)
        self.clients.append(client)
        await client.send("x" * (MAX_LINE + 1))
        with self.assertRaises(ConnectionClosed) as failure:
            await client.recv()
        self.assertEqual(failure.exception.rcvd.code, 1009)


if __name__ == "__main__":
    unittest.main()
