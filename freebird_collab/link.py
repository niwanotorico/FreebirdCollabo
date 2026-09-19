# SPDX-License-Identifier: GPL-2.0-or-later
"""
Client link: one connection to a hub, driven by background threads.
Main thread (Blender timer) calls poll() to drain incoming messages and
send() to enqueue outgoing ones. No bpy here.

Supports three URL forms (see ws.parse_url):
    tcp://host:port   raw length-prefixed frames   (direct / LAN)
    ws://host:port    WebSocket                     (local relay, tunnels)
    wss://host/path   WebSocket over TLS            (internet relay: HF Spaces, Render, ...)
"""

import json
import os
import queue
import socket
import ssl
import threading

from . import ws as wsmod
from .protocol import Decoder, encode


CHUNK_BYTES = 700 * 1024  # Cloudflare Workers/DO cap WebSocket messages at 1 MiB; split bigger ones


class Link:
    def __init__(self, url: str, log=print):
        self.url = url
        self.scheme, self.host, self.port, self.path = wsmod.parse_url(url)
        self.log = log
        self.inbox = queue.Queue()
        self.outbox = queue.Queue()
        self.connected = False
        self.error = None
        self._sock = None
        self._leftover = b""
        self._chunks = {}  # (from, id) -> {"n": int, "parts": {i: str}}
        self._stop = threading.Event()
        self._rx = None
        self._tx = None

    # ------------------------------------------------------------------
    def connect(self, timeout=10.0):
        sock = socket.create_connection((self.host, self.port), timeout=timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if self.scheme == "wss":
            ctx = ssl.create_default_context()
            try:  # Blender bundles certifi; prefer its CA bundle over the (sometimes empty) system store
                import certifi

                ctx.load_verify_locations(certifi.where())
            except Exception:
                pass
            sock = ctx.wrap_socket(sock, server_hostname=self.host)
        if self.scheme in ("ws", "wss"):
            self._leftover = wsmod.client_handshake(sock, self.host, self.path, self.port)
        sock.settimeout(None)
        self._sock = sock
        self.connected = True
        self._rx = threading.Thread(target=self._rx_loop, name="collab-rx", daemon=True)
        self._tx = threading.Thread(target=self._tx_loop, name="collab-tx", daemon=True)
        self._rx.start()
        self._tx.start()

    def close(self):
        self._stop.set()
        self.connected = False
        self.outbox.put(None)
        try:
            if self._sock:
                self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass

    def send(self, msg: dict):
        if self.connected:
            self.outbox.put(msg)

    def poll(self, max_items=200):
        out = []
        for _ in range(max_items):
            try:
                out.append(self.inbox.get_nowait())
            except queue.Empty:
                break
        return out

    # ------------------------------------------------------------------
    def _frame(self, body: bytes) -> bytes:
        if self.scheme == "tcp":
            return wsmod.struct.pack(">I", len(body)) + body
        return wsmod.build_frame(body, wsmod.OP_TEXT, mask=True)

    def _encode(self, msg: dict) -> bytes:
        body = encode(msg)[4:]
        if len(body) <= CHUNK_BYTES:
            return self._frame(body)
        # split into chunk messages; the hub forwards them like any other message
        text = body.decode("utf-8")
        cid = os.urandom(4).hex()
        n = (len(text) + CHUNK_BYTES - 1) // CHUNK_BYTES
        out = bytearray()
        for i in range(n):
            part = {"t": "chunk", "id": cid, "i": i, "n": n, "d": text[i * CHUNK_BYTES : (i + 1) * CHUNK_BYTES]}
            if "to" in msg:
                part["to"] = msg["to"]
            out += self._frame(encode(part)[4:])
        return bytes(out)

    def _deliver(self, m: dict):
        if m.get("t") != "chunk":
            self.inbox.put(m)
            return
        key = (m.get("from"), m["id"])
        entry = self._chunks.setdefault(key, {"n": m["n"], "parts": {}})
        entry["parts"][m["i"]] = m["d"]
        if len(entry["parts"]) == entry["n"]:
            del self._chunks[key]
            full = json.loads("".join(entry["parts"][i] for i in range(entry["n"])))
            if "from" in m:
                full["from"] = m["from"]
            self.inbox.put(full)

    def _rx_loop(self):
        try:
            if self.scheme == "tcp":
                dec = Decoder()
                while not self._stop.is_set():
                    data = self._sock.recv(1 << 20)
                    if not data:
                        break
                    for m in dec.feed(data):
                        self._deliver(m)
            else:
                reader = wsmod.FrameReader(self._sock, self._leftover)
                while not self._stop.is_set():
                    op, payload = reader.read_message()
                    if op == wsmod.OP_CLOSE:
                        break
                    if op == wsmod.OP_PING:
                        self.outbox.put(("_raw", wsmod.build_frame(payload, wsmod.OP_PONG, mask=True)))
                        continue
                    if op in (wsmod.OP_TEXT, wsmod.OP_BIN):
                        self._deliver(json.loads(payload.decode("utf-8")))
        except (OSError, ValueError, ConnectionError) as e:
            if not self._stop.is_set():
                self.error = str(e)
        finally:
            self.connected = False
            self.inbox.put({"t": "_disconnected"})

    def _tx_loop(self):
        try:
            while not self._stop.is_set():
                m = self.outbox.get()
                if m is None:
                    break
                if isinstance(m, tuple) and m[0] == "_raw":
                    self._sock.sendall(m[1])
                else:
                    self._sock.sendall(self._encode(m))
        except (OSError, ValueError) as e:
            if not self._stop.is_set():
                self.error = str(e)
            self.connected = False
