# SPDX-License-Identifier: GPL-2.0-or-later
"""
Room hub: accepts TCP clients, groups them into rooms, forwards frames.

Used in two ways:
  * standalone relay server  (relay/collab_relay.py wraps this)      -> Room-code UX
  * embedded in the HOST's Blender (direct mode, host = hub)         -> IP:port debug UX

Pure stdlib, no bpy dependency, so it is unit-testable outside Blender.
"""

import secrets
import socket
import threading
import time

from . import ws as wsmod
from .protocol import Decoder, encode, make

ROOM_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I


def new_room_code(n=6):
    return "".join(secrets.choice(ROOM_ALPHABET) for _ in range(n))


class _Client:
    def __init__(self, sock, addr, uid):
        self.sock = sock
        self.addr = addr
        self.uid = uid
        self.name = "?"
        self.role = "guest"
        self.color = [1, 1, 1]
        self.room = None
        self.lock = threading.Lock()
        self.alive = True
        self.ws = False  # True after a WebSocket upgrade

    def send(self, msg):
        try:
            if self.ws:
                data = wsmod.build_frame(encode(msg)[4:], wsmod.OP_TEXT, mask=False)
            else:
                data = encode(msg)
            with self.lock:
                self.sock.sendall(data)
        except OSError:
            self.alive = False


class Hub:
    def __init__(self, host="0.0.0.0", port=0, fixed_room=None, log=print):
        """
        fixed_room: when set (direct mode), the first host hello gets this code
                    instead of a random one, so guests can join with any code.
        """
        self.host = host
        self.port = port
        self.fixed_room = fixed_room
        self.log = log
        self.rooms = {}  # code -> {uid: _Client}
        self.clients = {}
        self._lock = threading.RLock()
        self._srv = None
        self._thread = None
        self._uid_counter = 0
        self.running = False

    # ---- lifecycle ---------------------------------------------------
    def start(self):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((self.host, self.port))
        self._srv.listen(16)
        self.port = self._srv.getsockname()[1]
        self.running = True
        self._thread = threading.Thread(target=self._accept_loop, name="collab-hub", daemon=True)
        self._thread.start()
        self.log(f"[hub] listening on {self.host}:{self.port}")
        return self.port

    def stop(self):
        self.running = False
        try:
            self._srv.close()
        except OSError:
            pass
        with self._lock:
            for c in list(self.clients.values()):
                try:
                    c.sock.close()
                except OSError:
                    pass
            self.clients.clear()
            self.rooms.clear()

    # ---- networking --------------------------------------------------
    def _accept_loop(self):
        while self.running:
            try:
                sock, addr = self._srv.accept()
            except OSError:
                break
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._lock:
                self._uid_counter += 1
                uid = f"u{self._uid_counter}"
                c = _Client(sock, addr, uid)
                self.clients[uid] = c
            threading.Thread(target=self._client_loop, args=(c,), daemon=True).start()

    def _client_loop(self, c: _Client):
        try:
            # one port, two dialects: raw length-prefixed TCP, or HTTP -> WebSocket
            first = c.sock.recv(4, socket.MSG_PEEK)
            if not first:
                return
            if first.startswith(b"GET ") or first.startswith(b"HEAD") or first.startswith(b"POST"):
                if not wsmod.server_handshake(c.sock):
                    return  # plain HTTP health check, already answered
                c.ws = True
                self._ws_loop(c)
            else:
                self._tcp_loop(c)
        except (OSError, ValueError, ConnectionError):
            pass
        finally:
            self._drop(c)

    def _tcp_loop(self, c: _Client):
        dec = Decoder()
        while self.running and c.alive:
            data = c.sock.recv(65536)
            if not data:
                break
            for msg in dec.feed(data):
                self._handle(c, msg)

    def _ws_loop(self, c: _Client):
        import json

        reader = wsmod.FrameReader(c.sock)
        while self.running and c.alive:
            op, payload = reader.read_message()
            if op == wsmod.OP_CLOSE:
                break
            if op == wsmod.OP_PING:
                with c.lock:
                    c.sock.sendall(wsmod.build_frame(payload, wsmod.OP_PONG))
                continue
            if op in (wsmod.OP_TEXT, wsmod.OP_BIN):
                self._handle(c, json.loads(payload.decode("utf-8")))

    # ---- room logic --------------------------------------------------
    def _handle(self, c: _Client, msg: dict):
        t = msg.get("t")
        if t == "hello":
            self._hello(c, msg)
            return
        if t == "ping":
            c.send(make("pong"))
            return
        if c.room is None:
            c.send(make("error", msg="not in a room"))
            return
        msg["from"] = c.uid
        to = msg.get("to")
        with self._lock:
            peers = list(self.rooms.get(c.room, {}).values())
        for p in peers:
            if p is c:
                continue
            if to and p.uid != to:
                continue
            p.send(msg)

    def _hello(self, c: _Client, msg: dict):
        c.name = str(msg.get("name", "user"))[:32]
        c.role = "host" if msg.get("role") == "host" else "guest"
        c.color = msg.get("color", [1, 1, 1])
        code = (msg.get("room") or "").strip().upper()
        with self._lock:
            if c.role == "host":
                code = self.fixed_room or code or new_room_code()
                if code in self.rooms and any(p.role == "host" for p in self.rooms[code].values()):
                    c.send(make("error", msg=f"room {code} already has a host"))
                    return
                self.rooms.setdefault(code, {})
            else:
                if self.fixed_room:
                    code = self.fixed_room
                if code not in self.rooms:
                    c.send(make("error", msg=f"room {code} not found"))
                    return
            room = self.rooms[code]
            room[c.uid] = c
            c.room = code
            peers = [
                {"uid": p.uid, "name": p.name, "role": p.role, "color": p.color} for p in room.values() if p is not c
            ]
            host_uid = next((p.uid for p in room.values() if p.role == "host"), None)
        c.send(make("welcome", uid=c.uid, room=code, peers=peers, host_uid=host_uid, version=1))
        self._broadcast(c, make("peer_join", uid=c.uid, name=c.name, role=c.role, color=c.color))
        self.log(f"[hub] {c.name}({c.role}) joined room {code}")

    def _broadcast(self, sender: _Client, msg: dict):
        with self._lock:
            peers = [p for p in self.rooms.get(sender.room, {}).values() if p is not sender]
        for p in peers:
            p.send(msg)

    def _drop(self, c: _Client):
        c.alive = False
        with self._lock:
            self.clients.pop(c.uid, None)
            room = self.rooms.get(c.room)
            if room:
                room.pop(c.uid, None)
                if not room:
                    del self.rooms[c.room]
        try:
            c.sock.close()
        except OSError:
            pass
        if c.room:
            self._broadcast(c, make("peer_leave", uid=c.uid))
            self.log(f"[hub] {c.name} left room {c.room}")


def serve_forever(host="0.0.0.0", port=7788):
    hub = Hub(host, port)
    hub.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        hub.stop()
