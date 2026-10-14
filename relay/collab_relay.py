#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""
Freebird Collab relay server.  Pure stdlib, no dependencies.
Speaks raw TCP *and* WebSocket on the same port (auto-detected), and answers
plain HTTP GET with 200 so PaaS health checks pass.

    python3 collab_relay.py            # port 7788 (or $PORT if set, e.g. 7860 on HF Spaces)
    python3 collab_relay.py 9000       # custom port
    python3 collab_relay.py --help     # limits / stats options (VPS use, see relay/vps/)

Users only ever type the 6-letter room code; the relay URL lives in the
add-on preferences and is set once.

Routing is freebird_collab/hub.py (the same code the HOST runs in direct mode),
so batch / ping / welcome.batch behave exactly like the Cloudflare relay.  This
file adds what a long-running public server needs, all settable by flag or env:

  * limits: concurrent connections, rooms, per-connection messages/bytes per
    minute (token bucket, so short bursts like a scene upload pass), single
    message size, time to say hello, idle time.  Over a limit the client gets an
    "error" message (shown in the add-on) or an HTTP 503, then is disconnected;
    nobody else in the room is affected.
  * GET /stats -> {"sockets": N, "rooms": M, ...} (same two keys as the
    Cloudflare relay, plus per-minute counters), and a one-line stats log.
"""

import argparse
import json
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _netlib import hub, protocol, ws as wsmod  # noqa: E402  (no bpy needed)

serve_forever = hub.serve_forever  # plain relay, no limits (kept for old callers)

# name -> (env var, default, help).  0 = unlimited / off.
SETTINGS = {
    "max_connections": ("FREEBIRD_MAX_CONNECTIONS", 200, "concurrent connections (WebSocket + TCP)"),
    "max_rooms": ("FREEBIRD_MAX_ROOMS", 100, "rooms open at the same time"),
    "max_msgs_per_min": ("FREEBIRD_MAX_MSGS_PER_MIN", 6000, "frames one connection may send per minute (a batch = 1)"),
    "max_mb_per_min": ("FREEBIRD_MAX_MB_PER_MIN", 512, "MiB one connection may send per minute"),
    "max_message_mb": ("FREEBIRD_MAX_MESSAGE_MB", 4, "largest single message in MiB (add-on chunks at 0.7)"),
    "hello_timeout": ("FREEBIRD_HELLO_TIMEOUT", 20, "seconds a new connection has to join a room"),
    "idle_timeout": ("FREEBIRD_IDLE_TIMEOUT", 300, "seconds without any data before disconnect (add-on pings every 20)"),
    "stats_interval": ("FREEBIRD_STATS_INTERVAL", 60, "seconds between stats log lines"),
    "stats_endpoint": ("FREEBIRD_STATS_ENDPOINT", 1, "1 = answer GET /stats, 0 = off"),
}


class LimitExceeded(ConnectionError):
    pass


class _Bucket:
    """Token bucket: `per_min` tokens per minute, burst of one minute's worth."""

    def __init__(self, per_min):
        self.rate = per_min / 60.0
        self.cap = float(per_min)
        self.tokens = self.cap
        self.t = time.monotonic()

    def take(self, n=1):
        if self.rate <= 0:
            return True
        now = time.monotonic()
        self.tokens = min(self.cap, self.tokens + (now - self.t) * self.rate)
        self.t = now
        self.tokens -= n
        return self.tokens >= 0


class _MeteredSocket:
    """Wraps a client socket: counts received bytes and enforces the byte budget."""

    def __init__(self, sock, relay, bucket):
        self._sock = sock
        self._relay = relay
        self._bucket = bucket

    def recv(self, n, flags=0):
        data = self._sock.recv(n, flags)
        if not flags & socket.MSG_PEEK and data:
            self._relay._count("bytes", len(data))
            if not self._bucket.take(len(data)):
                raise LimitExceeded("too much data per minute")
        return data

    def sendall(self, data):
        try:
            self._sock.sendall(data)
        except OSError:
            # stuck / gone receiver: unblock its own read loop so it is dropped promptly
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            raise

    def __getattr__(self, name):
        return getattr(self._sock, name)


class _SizedFrameReader(wsmod.FrameReader):
    def __init__(self, sock, max_bytes):
        super().__init__(sock)
        self._max = max_bytes

    def _need(self, n):
        if self._max and n + len(self._msg) > self._max:
            raise LimitExceeded("message too large")
        return super()._need(n)


class RelayHub(hub.Hub):
    """hub.Hub + limits + stats.  Routing (hello / batch / ping / to / from) is untouched."""

    def __init__(self, host="0.0.0.0", port=7788, log=print, **cfg):
        super().__init__(host, port, log=log)
        for name, (_env, default, _help) in SETTINGS.items():
            setattr(self, name, cfg.get(name, default))
        self.started = time.time()
        self.totals = {"frames": 0, "msgs": 0, "bytes": 0, "rejected": 0, "kicked": 0}
        self._samples = [(time.monotonic(), dict(self.totals))]  # [(monotonic, totals copy)] for per-minute rates
        self._tl = threading.local()
        self._count_lock = threading.Lock()

    # ---- counters ------------------------------------------------------
    def _count(self, key, n=1):
        with self._count_lock:
            self.totals[key] += n

    def stats(self):
        with self._lock:
            sockets = sum(1 for c in self.clients.values() if getattr(c, "proto", None))
            rooms = len(self.rooms)
        with self._count_lock:
            now, totals = time.monotonic(), dict(self.totals)
        old = next((s for s in self._samples if now - s[0] <= 65), (now, totals))
        span = max(now - old[0], 60.0)  # just started: count so far, not extrapolated
        per_min = {k: round((totals[k] - old[1][k]) * 60.0 / span) for k in ("frames", "msgs", "bytes")}
        return {
            "sockets": sockets,
            "rooms": rooms,
            "frames_per_min": per_min["frames"],  # what Cloudflare would bill: frames / 20 = requests
            "messages_per_min": per_min["msgs"],  # after unbatching (what peers receive, per receiver)
            "kb_per_min": per_min["bytes"] // 1024,
            "rejected": totals["rejected"],  # connections / rooms refused (full)
            "kicked": totals["kicked"],  # connections cut for exceeding a per-connection limit
            "uptime_s": int(time.time() - self.started),
        }

    def _stats_loop(self):
        last_log = time.monotonic()
        while self.running:
            time.sleep(5)
            with self._count_lock:
                self._samples.append((time.monotonic(), dict(self.totals)))
            cutoff = time.monotonic() - 70
            self._samples = [s for s in self._samples if s[0] >= cutoff]
            if self.stats_interval and time.monotonic() - last_log >= self.stats_interval:
                last_log = time.monotonic()
                s = self.stats()
                self.log(
                    f"[stats] sockets={s['sockets']} rooms={s['rooms']} frames/min={s['frames_per_min']} "
                    f"msgs/min={s['messages_per_min']} kB/min={s['kb_per_min']} "
                    f"rejected={s['rejected']} kicked={s['kicked']}"
                )

    def start(self):
        port = super().start()
        threading.Thread(target=self._stats_loop, name="collab-stats", daemon=True).start()
        return port

    # ---- connection handling ---------------------------------------------
    def _client_loop(self, c):
        try:
            if self.hello_timeout:
                c.sock.settimeout(self.hello_timeout)
            first = c.sock.recv(4, socket.MSG_PEEK)
            if not first:
                return
            is_http = first.startswith((b"GET ", b"HEAD", b"POST"))
            if is_http and self.stats_endpoint and self._is_stats_request(c.sock):
                return self._answer_http(c.sock, "200 OK", json.dumps(self.stats()), "application/json")
            with self._lock:
                active = sum(1 for p in self.clients.values() if getattr(p, "proto", None))
                full = bool(self.max_connections) and active >= self.max_connections
                if not full:
                    c.proto = "http" if is_http else "tcp"
            if full:
                self._count("rejected")
                self.log(f"[limit] connection refused: {active} connections (max {self.max_connections})")
                if is_http:
                    return self._answer_http(c.sock, "503 Service Unavailable", "relay is full, try again later\n")
                return c.sock.sendall(protocol.encode(protocol.make("error", msg="relay is full, try again later")))
            c.sock = _MeteredSocket(c.sock, self, _Bucket(self.max_mb_per_min * 1024 * 1024))
            c.msg_bucket = _Bucket(self.max_msgs_per_min)
            if is_http:
                if not wsmod.server_handshake(c.sock):
                    return  # plain HTTP health check, already answered
                c.ws = True
                self._ws_loop(c)
            else:
                self._tcp_loop(c)
        except LimitExceeded as e:
            self._kick(c, str(e))
        except (OSError, ValueError, ConnectionError):
            pass
        finally:
            self._drop(c)

    def _kick(self, c, reason):
        self._count("kicked")
        self.log(f"[limit] {c.name} ({c.uid}) disconnected: {reason}")
        if c.ws or getattr(c, "proto", None) == "tcp":  # speaks our protocol: tell the add-on why
            c.send(protocol.make("error", msg=f"relay limit: {reason}"))
        # Close gently: closing with unread input would send a TCP reset that can destroy the error
        # message above before the add-on reads it.  Stop sending, then discard input for <= 2 s.
        try:
            c.sock.shutdown(socket.SHUT_WR)
            c.sock.settimeout(0.2)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and c.sock._sock.recv(65536):
                pass
        except (OSError, AttributeError):
            pass

    @staticmethod
    def _is_stats_request(sock):
        head = b""
        for _ in range(20):  # request line may arrive in pieces
            head = sock.recv(256, socket.MSG_PEEK)
            if b"\r\n" in head or len(head) >= 256:
                break
            time.sleep(0.01)
        parts = head.split(b"\r\n", 1)[0].split(b" ")
        return len(parts) >= 2 and parts[1].split(b"?", 1)[0] == b"/stats"

    @staticmethod
    def _answer_http(sock, status, body, ctype="text/plain; charset=utf-8"):
        try:
            wsmod._read_http_head(sock)
        except (OSError, ValueError, ConnectionError):
            pass
        data = body.encode("utf-8")
        sock.sendall(
            f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\nContent-Length: {len(data)}\r\n"
            "Cache-Control: no-store\r\nConnection: close\r\n\r\n".encode() + data
        )

    def _tcp_loop(self, c):
        # same framing as protocol.Decoder, but the length is checked before the body is buffered
        buf = bytearray()
        size = protocol.HEADER.size
        limit = int(self.max_message_mb * 1024 * 1024) or protocol.MAX_FRAME
        while self.running and c.alive:
            data = c.sock.recv(65536)
            if not data:
                break
            buf.extend(data)
            while len(buf) >= size:
                (n,) = protocol.HEADER.unpack_from(buf, 0)
                if n > limit:
                    raise LimitExceeded("message too large")
                if len(buf) < size + n:
                    break
                body = bytes(buf[size : size + n])
                del buf[: size + n]
                self._handle(c, json.loads(body.decode("utf-8")))

    def _ws_loop(self, c):
        reader = _SizedFrameReader(c.sock, int(self.max_message_mb * 1024 * 1024))
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

    # ---- room logic hooks -------------------------------------------------
    def _handle(self, c, msg):
        if not isinstance(msg, dict):
            return
        nested = getattr(self._tl, "depth", 0)
        if not nested:  # one received frame (a batch counts once, like Cloudflare)
            self._count("frames")
            if not c.msg_bucket.take():
                raise LimitExceeded("too many messages per minute")
        if msg.get("t") != "batch":
            self._count("msgs")
        self._tl.depth = nested + 1
        try:
            super()._handle(c, msg)
        finally:
            self._tl.depth = nested

    def _hello(self, c, msg):
        if c.room is not None:
            return  # one hello per connection
        code = (msg.get("room") or "").strip().upper()
        if msg.get("role") == "host" and self.max_rooms:
            with self._lock:
                full = code not in self.rooms and len(self.rooms) >= self.max_rooms
            if full:
                self._count("rejected")
                self.log(f"[limit] room refused: {len(self.rooms)} rooms (max {self.max_rooms})")
                c.send(protocol.make("error", msg="relay is full (too many rooms), try again later"))
                return
        super()._hello(c, msg)
        if c.room is not None:
            c.sock.settimeout(self.idle_timeout or None)


def _config(argv):
    ap = argparse.ArgumentParser(description="Freebird Collab relay (tcp + websocket on one port)")
    ap.add_argument("port", nargs="?", type=int, default=int(os.environ.get("PORT", "7788")))
    ap.add_argument("--host", default=os.environ.get("FREEBIRD_HOST", "0.0.0.0"),
                    help="address to listen on (127.0.0.1 behind Caddy)  [$FREEBIRD_HOST]")
    for name, (env, default, help_) in SETTINGS.items():
        typ = float if name.endswith("_mb") else int
        ap.add_argument("--" + name.replace("_", "-"), type=typ, default=typ(os.environ.get(env, default)),
                        help=f"{help_}; 0 = unlimited/off  [${env}, default {default}]")
    return ap.parse_args(argv)


def main(argv=None):
    import signal

    args = _config(sys.argv[1:] if argv is None else argv)
    cfg = {k: getattr(args, k) for k in SETTINGS}
    relay = RelayHub(args.host, args.port, log=lambda m: print(m, flush=True), **cfg)
    relay.start()
    print(f"Freebird Collab relay on {args.host}:{relay.port} (tcp + websocket)  Ctrl+C to stop", flush=True)
    print("[config] " + " ".join(f"{k}={v}" for k, v in cfg.items()), flush=True)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())  # systemd / docker stop
    try:
        while not stop.wait(1):
            pass
    except KeyboardInterrupt:
        pass
    relay.stop()
    print("relay stopped", flush=True)


if __name__ == "__main__":
    main()
