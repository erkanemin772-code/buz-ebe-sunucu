#!/usr/bin/env python3
"""Bounded 2–5 player TLS/WebSocket relay. Simulation stays on the host phone."""
from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import contextlib
from dataclasses import dataclass, field
import json
import logging
import math
import secrets
import ssl
import time
from typing import Any
from http import HTTPStatus

MAX_LINE = 16_384
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class ProtocolError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def encode(message: dict[str, Any]) -> bytes:
    payload = json.dumps(message, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
    if len(payload) > MAX_LINE:
        raise ProtocolError("message_too_large", "Oyun verisi izin verilen boyutu aşıyor.")
    return payload


def reject_constant(value: str) -> None:
    raise ValueError("Non-finite number")


@dataclass(eq=False)
class Peer:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    room: Room | None = None
    is_host: bool = False
    slot: int = -1
    request_id: str = ""
    name: str = "Oyuncu"
    tokens: float = 120.0
    rate_at: float = field(default_factory=time.monotonic)
    sequence: int = -1
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, message: dict[str, Any]) -> None:
        async with self.send_lock:
            if self.writer.is_closing():
                raise ConnectionError("Closed")
            self.writer.write(encode(message))
            await asyncio.wait_for(self.writer.drain(), 2.0)

    def check_rate(self) -> None:
        now = time.monotonic()
        self.tokens = min(120.0, self.tokens + (now - self.rate_at) * 90.0)
        self.rate_at = now
        if self.tokens < 1.0:
            raise ProtocolError("rate_limited", "Çok fazla istek gönderildi.")
        self.tokens -= 1.0


@dataclass(eq=False)
class Room:
    code: str
    host: Peer
    name: str = "Buz Ebe"
    capacity: int = 2
    guests: dict[int, Peer] = field(default_factory=dict)
    pending: dict[str, Peer] = field(default_factory=dict)
    snapshot: dict[str, Any] | None = None


class WebSocketReader:
    """Present one WebSocket text message as one newline JSON protocol frame."""
    def __init__(self, connection: Any):
        self.connection = connection

    async def readline(self) -> bytes:
        from websockets.exceptions import ConnectionClosed
        try:
            message = await self.connection.recv()
        except ConnectionClosed as exc:
            raise EOFError from exc
        if not isinstance(message, str):
            raise ProtocolError("bad_message", "WebSocket ile metin verisi gönderilmeli.")
        return message.encode("utf-8") + b"\n"


class WebSocketWriter:
    """Use the same bounded relay logic for TCP/TLS and WebSocket transports."""
    def __init__(self, connection: Any):
        self.connection = connection
        self.pending: str | None = None
        self.closing = False
        self.close_task: asyncio.Task | None = None

    def write(self, payload: bytes) -> None:
        self.pending = payload[:-1].decode("utf-8")

    async def drain(self) -> None:
        from websockets.exceptions import ConnectionClosed
        if self.pending is None:
            return
        payload, self.pending = self.pending, None
        try:
            await self.connection.send(payload)
        except ConnectionClosed as exc:
            raise ConnectionError("Closed") from exc

    def is_closing(self) -> bool:
        return self.closing or self.connection.close_code is not None

    def close(self) -> None:
        if not self.closing:
            self.closing = True
            self.close_task = asyncio.create_task(self.connection.close())

    async def wait_closed(self) -> None:
        if self.close_task is not None:
            await self.close_task
        await self.connection.wait_closed()


class Relay:
    def __init__(self, max_rooms: int = 500, idle_timeout: float = 20.0,
                 handshake_timeout: float = 10.0):
        self.max_rooms = max_rooms
        self.max_connections = max_rooms * 6 + 32
        self.idle_timeout = idle_timeout
        self.handshake_timeout = handshake_timeout
        self.rooms: dict[str, Room] = {}
        self.peers: set[Peer] = set()

    async def read(self, peer: Peer, timeout: float) -> dict[str, Any]:
        try:
            line = await asyncio.wait_for(peer.reader.readline(), timeout)
        except (ValueError, asyncio.LimitOverrunError) as exc:
            raise ProtocolError("message_too_large", "Oyun verisi izin verilen boyutu aşıyor.") from exc
        except asyncio.TimeoutError as exc:
            raise ProtocolError("timeout", "Bağlantı zaman aşımına uğradı.") from exc
        if not line:
            raise EOFError
        if len(line) > MAX_LINE or not line.endswith(b"\n"):
            raise ProtocolError("message_too_large", "Eksik veya çok büyük oyun verisi.")
        peer.check_rate()
        try:
            value = json.loads(line, parse_constant=reject_constant)
        except (ValueError, UnicodeDecodeError, RecursionError) as exc:
            raise ProtocolError("bad_message", "Geçersiz oyun verisi.") from exc
        if not isinstance(value, dict) or not isinstance(value.get("type"), str):
            raise ProtocolError("bad_message", "Oyun verisinin türü eksik.")
        return value

    async def accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = Peer(reader, writer)
        if len(self.peers) >= self.max_connections:
            with contextlib.suppress(Exception):
                await peer.send({"type": "error", "code": "server_full", "message": "Sunucu dolu; daha sonra yeniden dene."})
            writer.close()
            return
        self.peers.add(peer)
        try:
            first = await self.read(peer, self.handshake_timeout)
            await self.handshake(peer, first)
            while True:
                message = await self.read(peer, self.idle_timeout)
                if message["type"] == "leave":
                    break
                await self.handle(peer, message)
        except ProtocolError as exc:
            with contextlib.suppress(Exception):
                await peer.send({"type": "error", "code": exc.code, "message": str(exc)})
        except (EOFError, ConnectionError, OSError, asyncio.TimeoutError):
            pass
        finally:
            await self.detach(peer)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            self.peers.discard(peer)

    async def handshake(self, peer: Peer, message: dict[str, Any]) -> None:
        if message["type"] == "host":
            if len(self.rooms) >= self.max_rooms:
                raise ProtocolError("server_full", "Sunucu dolu; daha sonra yeniden dene.")
            while True:
                code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
                if code not in self.rooms:
                    break
            name = message.get("name", "Buz Ebe")
            if not isinstance(name, str):
                name = "Buz Ebe"
            name = "".join(ch for ch in name if ch.isprintable()).strip()[:32] or "Buz Ebe"
            capacity = message.get("capacity", 2)
            if type(capacity) is not int or not 2 <= capacity <= 5:
                raise ProtocolError("bad_capacity", "Oyuncu sayısı 2 ile 5 arasında olmalı.")
            room = Room(code, peer, name, capacity)
            self.rooms[code] = room
            peer.room, peer.is_host = room, True
            peer.slot = 0
            await peer.send({"type": "hosted", "code": code, "name": name, "capacity": capacity, "id": 0})
        elif message["type"] == "join":
            code = message.get("code", "")
            if not isinstance(code, str) or len(code) != 8:
                raise ProtocolError("invalid_code", "8 karakterli oda kodunu yaz.")
            room = self.rooms.get(code.upper())
            if room is None or room.host.writer.is_closing():
                raise ProtocolError("room_not_found", "Oda bulunamadı. Kodu kontrol et.")
            if 1 + len(room.guests) >= room.capacity:
                raise ProtocolError("room_full", "Bu oda dolu.")
            if len(room.pending) >= 8:
                raise ProtocolError("too_many_requests", "Odanın katılma istekleri dolu; birazdan yeniden dene.")
            name = message.get("name", "Oyuncu")
            if not isinstance(name, str):
                name = "Oyuncu"
            peer.name = "".join(ch for ch in name if ch.isprintable()).strip()[:32] or "Oyuncu"
            peer.request_id = secrets.token_hex(8)
            peer.room = room
            room.pending[peer.request_id] = peer
            await peer.send({"type": "pending", "requestId": peer.request_id})
            await room.host.send({"type": "join_request", "requestId": peer.request_id, "name": peer.name})
        elif message["type"] == "list":
            await self.list_rooms(peer, message)
        else:
            raise ProtocolError("handshake_required", "Önce oda oluştur veya bir odaya katıl.")

    async def list_rooms(self, peer: Peer, message: dict[str, Any]) -> None:
        cursor = message.get("cursor", 0)
        if type(cursor) is not int or not 0 <= cursor <= self.max_rooms:
            raise ProtocolError("bad_cursor", "Geçersiz oda listesi sayfası.")
        rooms = [room for room in self.rooms.values() if not room.host.writer.is_closing()]
        entries = [{"code": room.code, "name": room.name,
                    "players": 1 + len(room.guests), "capacity": room.capacity}
                   for room in rooms[cursor:cursor + 64]]
        next_cursor = cursor + len(entries)
        await peer.send({"type": "rooms", "rooms": entries,
                         "next": next_cursor if next_cursor < len(rooms) else None})

    async def decide_request(self, room: Room, message: dict[str, Any]) -> None:
        request_id = message.get("requestId")
        if not isinstance(request_id, str) or request_id not in room.pending:
            await room.host.send({"type": "request_error", "requestId": request_id if isinstance(request_id, str) else "",
                                  "code": "request_not_found", "message": "Katılma isteği artık geçerli değil."})
            return
        guest = room.pending.pop(request_id)
        guest.request_id = ""
        if message["type"] == "reject":
            guest.room = None
            with contextlib.suppress(Exception):
                await guest.send({"type": "error", "code": "rejected", "message": "Oda sahibi katılma isteğini reddetti."})
            guest.writer.close()
            return
        slot = next((i for i in range(1, room.capacity) if i not in room.guests), None)
        if slot is None or guest.writer.is_closing():
            code = "room_full" if slot is None else "request_not_found"
            explanation = "Bu oda dolu." if slot is None else "Oyuncu bağlantıyı kapattı."
            guest.room = None
            with contextlib.suppress(Exception):
                await guest.send({"type": "error", "code": code, "message": explanation})
            guest.writer.close()
            await room.host.send({"type": "request_error", "requestId": request_id, "code": code, "message": explanation})
            return
        guest.slot = slot
        room.guests[slot] = guest
        try:
            await guest.send({"type": "joined", "code": room.code, "id": slot, "capacity": room.capacity})
            if room.snapshot is not None:
                await guest.send(room.snapshot)
        except (ConnectionError, OSError, asyncio.TimeoutError):
            room.guests.pop(slot, None)
            guest.room = None
            guest.writer.close()
            await room.host.send({"type": "request_error", "requestId": request_id,
                                  "code": "request_not_found", "message": "Oyuncu bağlantıyı kapattı."})
            return
        await room.host.send({"type": "peer_joined", "id": slot, "requestId": request_id, "name": guest.name})

    async def handle(self, peer: Peer, message: dict[str, Any]) -> None:
        kind = message["type"]
        if kind == "ping":
            await peer.send({"type": "pong"})
            return
        if kind == "list":
            await self.list_rooms(peer, message)
            return
        room = peer.room
        if room is None:
            if kind in ("host", "join"):
                await self.handshake(peer, message)
                return
            raise ProtocolError("room_closed", "Önce oda oluştur veya bir odaya katıl.")
        if peer.is_host and kind in ("accept", "reject"):
            await self.decide_request(room, message)
        elif kind == "voice":
            if peer.slot < 0 or (not peer.is_host and room.guests.get(peer.slot) is not peer):
                raise ProtocolError("approval_required", "Oda sahibinin onayı bekleniyor.")
            data = message.get("data")
            if not isinstance(data, str) or len(data) > 1800:
                raise ProtocolError("bad_voice", "Geçersiz ses paketi.")
            try:
                audio = base64.b64decode(data, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ProtocolError("bad_voice", "Geçersiz ses paketi.") from exc
            if len(audio) != 1280:
                raise ProtocolError("bad_voice", "Ses paketi 40 milisaniye olmalı.")
            # Forward live audio only. Never attach it to snapshots or retain it
            # in room state; the sender slot comes from the accepted socket.
            packet = {"type": "voice", "data": data, "from": peer.slot}
            for recipient in [room.host] + list(room.guests.values()):
                if recipient is peer:
                    continue
                try:
                    await recipient.send(packet)
                except (ConnectionError, OSError, asyncio.TimeoutError):
                    recipient.writer.close()
                    await self.detach(recipient)
        elif peer.is_host and kind == "state":
            # The host owns simulation. Only host sockets can publish snapshots.
            room.snapshot = message
            for guest in list(room.guests.values()):
                try:
                    await guest.send(message)
                except (ConnectionError, OSError, asyncio.TimeoutError):
                    guest.writer.close()
                    await self.detach(guest)
        elif not peer.is_host and kind == "input":
            if peer.slot < 1 or room.guests.get(peer.slot) is not peer:
                raise ProtocolError("approval_required", "Oda sahibinin onayı bekleniyor.")
            sequence = message.get("seq")
            if type(sequence) is not int or not 0 <= sequence <= 2**53:
                raise ProtocolError("bad_input", "Geçersiz kontrol sırası.")
            if sequence <= peer.sequence:
                return
            peer.sequence = sequence
            x, z = message.get("x", 0), message.get("z", 0)
            if type(x) not in (int, float) or type(z) not in (int, float):
                raise ProtocolError("bad_input", "Geçersiz hareket verisi.")
            try:
                x, z = float(x), float(z)
            except (ValueError, OverflowError) as exc:
                raise ProtocolError("bad_input", "Geçersiz hareket verisi.") from exc
            if not math.isfinite(x) or not math.isfinite(z):
                raise ProtocolError("bad_input", "Geçersiz hareket verisi.")
            active = message.get("active", True) is True
            packet = {"type": "input", "id": peer.slot, "seq": sequence,
                      "x": max(-1.0, min(1.0, x)) if active else 0.0,
                      "z": max(-1.0, min(1.0, z)) if active else 0.0,
                      "drop": message.get("drop") is True if active else False,
                      "swap": message.get("swap") is True if active else False,
                      "active": active}
            await room.host.send(packet)
        else:
            raise ProtocolError("wrong_direction", "Bu bağlantı bu işlem için yetkili değil.")

    async def detach(self, peer: Peer) -> None:
        room, peer.room = peer.room, None
        if room is None:
            return
        if peer.is_host:
            self.rooms.pop(room.code, None)
            others = list(room.guests.values()) + list(room.pending.values())
            room.guests.clear()
            room.pending.clear()
            for guest in others:
                guest.room = None
                with contextlib.suppress(Exception):
                    await guest.send({"type": "error", "code": "host_left", "message": "Oda sahibi ayrıldı. Yeni bir oda açabilirsin."})
                guest.writer.close()
        elif room.guests.get(peer.slot) is peer:
            room.guests.pop(peer.slot, None)
            with contextlib.suppress(Exception):
                await room.host.send({"type": "peer_left", "id": peer.slot})
        elif room.pending.get(peer.request_id) is peer:
            room.pending.pop(peer.request_id, None)
            with contextlib.suppress(Exception):
                await room.host.send({"type": "request_left", "requestId": peer.request_id})

    async def close(self) -> None:
        for peer in list(self.peers):
            peer.writer.close()
        await asyncio.sleep(0)


async def websocket_server(relay: Relay, host: str, port: int, tls: ssl.SSLContext | None = None):
    """RFC6455 framing, masking, fragmentation and backpressure use websockets."""
    try:
        from websockets.asyncio.server import serve as ws_serve
    except ImportError as exc:
        raise SystemExit("WebSocket mode requires: python3 -m pip install -r relay/requirements.txt") from exc

    async def handler(connection):
        await relay.accept(WebSocketReader(connection), WebSocketWriter(connection))

    def request(connection, incoming):
        if incoming.path == "/":
            return connection.respond(HTTPStatus.OK, "Buz Ebe relay ready\n")
        if incoming.path != "/ws":
            return connection.respond(HTTPStatus.NOT_FOUND, "Not found\n")
        return None

    # Native clients send no Origin. Reject browser-origin connections so an
    # unrelated webpage cannot silently create or browse game sessions.
    logger = logging.getLogger("buzebe.websocket.silent")
    logger.disabled = True
    options: dict[str, Any] = {}
    if tls is not None:
        options["ssl"] = tls
        options["ssl_handshake_timeout"] = 10.0
    return await ws_serve(handler, host, port, origins=[None],
                          process_request=request, compression=None,
                          open_timeout=10.0, ping_interval=10.0, ping_timeout=10.0,
                          close_timeout=2.0, max_size=MAX_LINE - 1, max_queue=8,
                          write_limit=32_768, server_header=None, logger=logger,
                          **options)


def tls_context(args: argparse.Namespace) -> ssl.SSLContext | None:
    tls: ssl.SSLContext | None = None
    if getattr(args, "behind_proxy", False):
        if not args.websocket or args.dev_loopback or args.cert or args.key:
            raise SystemExit("--behind-proxy requires --websocket and cannot be combined with certificates or --dev-loopback.")
        # Render terminates public WSS at its managed edge. This HTTP listener
        # belongs only on the provider's isolated upstream network.
    elif args.dev_loopback:
        if args.bind not in ("127.0.0.1", "::1", "localhost"):
            raise SystemExit("Plain TCP is restricted to --bind 127.0.0.1 or ::1.")
        if args.cert or args.key:
            raise SystemExit("Use TLS mode instead of --dev-loopback when supplying certificates.")
    else:
        if not args.cert or not args.key:
            raise SystemExit("Internet mode requires --cert and --key. Plain local testing requires --dev-loopback.")
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        tls.load_cert_chain(args.cert, args.key)
    return tls


async def serve(args: argparse.Namespace) -> None:
    tls = tls_context(args)
    relay = Relay(max_rooms=args.max_rooms)
    options: dict[str, Any] = {"limit": MAX_LINE}
    if tls:
        options["ssl"] = tls
        options["ssl_handshake_timeout"] = 10.0
    if args.websocket:
        server = await websocket_server(relay, args.bind, args.port, tls)
        transport = "WSS" if tls else "managed TLS proxy upstream" if getattr(args, "behind_proxy", False) else "loopback WebSocket origin for TLS tunnel"
    else:
        server = await asyncio.start_server(relay.accept, args.bind, args.port, **options)
        transport = "TLS" if tls else "loopback development TCP"
    print(f"Buz Ebe relay ready; {transport}; port {args.port}; capacity {args.max_rooms} rooms.", flush=True)
    try:
        async with server:
            await server.serve_forever()
    finally:
        await relay.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", "--host", dest="bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--cert", help="Publicly trusted certificate full chain PEM")
    parser.add_argument("--key", help="Private key PEM; keep outside the project")
    parser.add_argument("--max-rooms", type=int, default=500)
    parser.add_argument("--dev-loopback", action="store_true")
    parser.add_argument("--behind-proxy", action="store_true", help="WebSocket HTTP upstream behind a managed TLS proxy only")
    parser.add_argument("--websocket", action="store_true", help="WebSocket on /ws; GET / is health only")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.max_rooms <= 5000:
        parser.error("port must be 1..65535 and max-rooms 1..5000")
    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
