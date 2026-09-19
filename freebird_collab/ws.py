# SPDX-License-Identifier: GPL-2.0-or-later
"""
Minimal WebSocket (RFC 6455) over a plain socket, stdlib only.
Enough for the relay: text frames carrying one JSON message each,
ping/pong/close handling, client-side masking, 64-bit lengths.

Why: free hosting (Hugging Face Spaces, Render, Cloudflare tunnels...) only
forwards HTTP/WebSocket, not raw TCP. The hub speaks both on one port.
"""

import base64
import hashlib
import os
import struct
from urllib.parse import urlparse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(n - len(buf), 1 << 20))
        if not chunk:
            raise ConnectionError("socket closed")
        buf.extend(chunk)
    return bytes(buf)


def _read_http_head(sock, first=b""):
    data = bytearray(first)
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("socket closed during HTTP head")
        data.extend(chunk)
        if len(data) > 65536:
            raise ValueError("HTTP head too large")
    head, _, rest = bytes(data).partition(b"\r\n\r\n")
    return head.decode("latin-1"), rest


def _parse_headers(head):
    lines = head.split("\r\n")
    start = lines[0]
    headers = {}
    for ln in lines[1:]:
        k, _, v = ln.partition(":")
        headers[k.strip().lower()] = v.strip()
    return start, headers


# ----------------------------------------------------------------------
# server side
# ----------------------------------------------------------------------
def server_handshake(sock, first_bytes=b""):
    """
    Call after peeking that the connection starts with an HTTP request.
    Returns True when upgraded to WebSocket, False when it was a plain HTTP
    request (a 200 health response was sent; caller should close).
    """
    head, _rest = _read_http_head(sock, first_bytes)
    start, h = _parse_headers(head)
    if h.get("upgrade", "").lower() != "websocket" or "sec-websocket-key" not in h:
        body = b"freebird-collab relay ok\n"
        sock.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        return False
    accept = base64.b64encode(hashlib.sha1((h["sec-websocket-key"] + GUID).encode()).digest()).decode()
    sock.sendall(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode()
    )
    return True


# ----------------------------------------------------------------------
# client side
# ----------------------------------------------------------------------
def parse_url(url: str):
    """'wss://host/path' | 'ws://host:port' | 'tcp://host:port' | 'host:port' -> (scheme, host, port, path)"""
    url = url.strip()
    if "://" not in url:
        url = "tcp://" + url
    u = urlparse(url)
    scheme = u.scheme.lower()
    if scheme not in ("ws", "wss", "tcp"):
        raise ValueError(f"unsupported scheme: {scheme}")
    port = u.port or {"ws": 80, "wss": 443, "tcp": 7788}[scheme]
    return scheme, u.hostname, port, (u.path or "/")


def client_handshake(sock, host, path="/", port=None):
    key = base64.b64encode(os.urandom(16)).decode()
    hosthdr = host if port in (None, 80, 443) else f"{host}:{port}"
    sock.sendall(
        (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {hosthdr}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "User-Agent: freebird-collab/0.2\r\n\r\n"
        ).encode()
    )
    head, rest = _read_http_head(sock)
    start, h = _parse_headers(head)
    if " 101 " not in start:
        raise ConnectionError(f"websocket handshake refused: {start}")
    expect = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    if h.get("sec-websocket-accept") != expect:
        raise ConnectionError("bad Sec-WebSocket-Accept")
    return rest  # leftover bytes (start of first frame, if any)


# ----------------------------------------------------------------------
# frames
# ----------------------------------------------------------------------
def build_frame(payload: bytes, opcode=OP_TEXT, mask=False) -> bytes:
    n = len(payload)
    head = bytearray([0x80 | opcode])
    mbit = 0x80 if mask else 0
    if n < 126:
        head.append(mbit | n)
    elif n < 65536:
        head.append(mbit | 126)
        head += struct.pack(">H", n)
    else:
        head.append(mbit | 127)
        head += struct.pack(">Q", n)
    if mask:
        key = os.urandom(4)
        head += key
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload)) if n < 4096 else _mask_fast(payload, key)
    return bytes(head) + payload


def _mask_fast(payload: bytes, key: bytes) -> bytes:
    # xor with the 4-byte key using int arithmetic on big chunks
    n = len(payload)
    full = n - (n % 4)
    k = int.from_bytes(key * (full // 4), "big") if full else 0
    out = (int.from_bytes(payload[:full], "big") ^ k).to_bytes(full, "big") if full else b""
    tail = bytes(b ^ key[i % 4] for i, b in enumerate(payload[full:], start=full))
    return out + tail


class FrameReader:
    """Reads complete messages from a socket. Yields (opcode, payload)."""

    def __init__(self, sock, leftover=b""):
        self.sock = sock
        self._buf = bytearray(leftover)
        self._msg = bytearray()
        self._msg_op = None

    def _need(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(1 << 20)
            if not chunk:
                raise ConnectionError("socket closed")
            self._buf.extend(chunk)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def read_message(self):
        while True:
            b0, b1 = self._need(2)
            fin, op = b0 & 0x80, b0 & 0x0F
            masked, n = b1 & 0x80, b1 & 0x7F
            if n == 126:
                (n,) = struct.unpack(">H", self._need(2))
            elif n == 127:
                (n,) = struct.unpack(">Q", self._need(8))
            key = self._need(4) if masked else None
            payload = self._need(n)
            if key:
                payload = _mask_fast(payload, key)
            if op in (OP_PING, OP_PONG, OP_CLOSE):
                return op, payload
            if op != OP_CONT:
                self._msg_op = op
                self._msg = bytearray()
            self._msg.extend(payload)
            if fin:
                out = bytes(self._msg)
                self._msg = bytearray()
                return self._msg_op, out
