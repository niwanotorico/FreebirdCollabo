# SPDX-License-Identifier: GPL-2.0-or-later
"""
VPS relay (relay/collab_relay.py RelayHub) regression test.  Plain Python, no Blender.

    python3 tests/test_relay_vps.py

  1  routing is unchanged: the whole unit_hub suite of test_relay_traffic.py
     (welcome.batch, batch unbatching, to/from, ping, errors, peer_leave, rejoin) on RelayHub
  2  GET /stats -> {"sockets", "rooms", ...} JSON; GET / still "relay ok"; a batch counts as 1 frame
  3  max_connections: the next WebSocket gets HTTP 503, the next raw TCP an "error"; others stay
  4  max_rooms: a new room is refused with an "error", joining an existing room still works
  5  max_msgs_per_min: a flooding client gets "relay limit" + disconnect, its room mate stays
  6  max_message_mb: an oversized message (WebSocket and TCP) gets the sender disconnected
  7  max_mb_per_min: too many bytes per minute gets the sender disconnected
  8  hello_timeout: a silent connection is closed
"""

import json
import os
import socket
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "relay"))
sys.path.insert(0, HERE)

import collab_relay  # noqa: E402
import test_relay_traffic as trt  # noqa: E402

nl = trt._netlib()


def relay(**cfg):
    cfg.setdefault("stats_interval", 0)
    r = collab_relay.RelayHub("127.0.0.1", 0, log=lambda *a: None, **cfg)
    port = r.start()
    return r, port


def get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
        return resp.status, resp.read().decode()


def closed(client, timeout=3.0):
    """True once the relay has cut the link (link reports _disconnected)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if any(m.get("t") == "_disconnected" for m in client.pump() + client.got):
            return True
        time.sleep(0.02)
    return False


def t1_routing():
    r, port = relay()
    try:
        trt.unit_hub(f"ws://127.0.0.1:{port}")
        trt.unit_hub(f"tcp://127.0.0.1:{port}")
    finally:
        r.stop()
    print("1 routing (unit_hub on RelayHub, ws + tcp) OK")


def t2_stats():
    r, port = relay()
    try:
        st, body = get(port, "/")
        assert st == 200 and "relay ok" in body, body
        s = json.loads(get(port, "/stats")[1])
        assert s["sockets"] == 0 and s["rooms"] == 0, s
        h = trt.RawClient(nl, f"ws://127.0.0.1:{port}", "Host", "host")
        g = trt.RawClient(nl, f"tcp://127.0.0.1:{port}", "G", "guest", h.welcome["room"])
        h.send({"t": "batch", "m": [{"t": "xform", "i": i} for i in range(10)]})
        g.wait(lambda m: m.get("i") == 9)
        s = json.loads(get(port, "/stats?x=1")[1])
        assert s["sockets"] == 2 and s["rooms"] == 1, s
        for k in ("frames_per_min", "messages_per_min", "kb_per_min", "rejected", "kicked", "uptime_s"):
            assert k in s, s
        assert r.totals["frames"] == 3 and r.totals["msgs"] == 12, r.totals  # 2 hello + 1 batch / 2 hello + 10
        h.close()
        g.close()
    finally:
        r.stop()
    print("2 /stats OK:", s)


def t3_max_connections():
    r, port = relay(max_connections=2)
    try:
        h = trt.RawClient(nl, f"ws://127.0.0.1:{port}", "Host", "host")
        g = trt.RawClient(nl, f"ws://127.0.0.1:{port}", "G", "guest", h.welcome["room"])
        try:
            trt.RawClient(nl, f"ws://127.0.0.1:{port}", "X", "guest", h.welcome["room"])
            raise AssertionError("3rd websocket was accepted")
        except ConnectionError as e:
            assert "503" in str(e), e
        x = trt.RawClient(nl, f"tcp://127.0.0.1:{port}", "X", "guest", h.welcome["room"])
        assert x.welcome["t"] == "error" and "full" in x.welcome["msg"], x.welcome
        assert r.stats()["sockets"] == 2 and r.totals["rejected"] == 2
        g.send({"t": "xform", "k": 1})
        h.wait(lambda m: m.get("k") == 1)
        g.close()
        time.sleep(0.3)
        y = trt.RawClient(nl, f"ws://127.0.0.1:{port}", "Y", "guest", h.welcome["room"])
        assert y.welcome["t"] == "welcome", "a freed slot must be usable again"
        for c in (h, x, y):
            c.close()
    finally:
        r.stop()
    print("3 max_connections OK")


def t4_max_rooms():
    r, port = relay(max_rooms=1)
    try:
        h = trt.RawClient(nl, f"ws://127.0.0.1:{port}", "Host", "host")
        h2 = trt.RawClient(nl, f"ws://127.0.0.1:{port}", "Host2", "host")
        assert h2.welcome["t"] == "error" and "rooms" in h2.welcome["msg"], h2.welcome
        g = trt.RawClient(nl, f"ws://127.0.0.1:{port}", "G", "guest", h.welcome["room"])
        assert g.welcome["t"] == "welcome"
        h2.send({"t": "hello", "name": "again", "role": "host", "room": "", "color": [1, 1, 1]})
        assert h2.wait(lambda m: m["t"] == "error" and m is not h2.welcome)
        h.close()
        g.close()
        time.sleep(0.3)
        h3 = trt.RawClient(nl, f"ws://127.0.0.1:{port}", "Host3", "host")
        assert h3.welcome["t"] == "welcome", "room slot frees when the room empties"
        h2.close()
        h3.close()
    finally:
        r.stop()
    print("4 max_rooms OK")


def t5_msg_rate():
    r, port = relay(max_msgs_per_min=60)
    try:
        h = trt.RawClient(nl, f"ws://127.0.0.1:{port}", "Host", "host")
        g = trt.RawClient(nl, f"ws://127.0.0.1:{port}", "Flood", "guest", h.welcome["room"])
        g.send({"t": "batch", "m": [{"t": "xform", "i": i} for i in range(200)]})  # 1 frame
        h.wait(lambda m: m.get("i") == 199)
        assert not closed(g, 0.3), "a batch is one frame"
        for i in range(80):
            g.send({"t": "ping"})
        err = g.wait(lambda m: m["t"] == "error")
        assert "relay limit" in err["msg"], err
        assert closed(g)
        h.wait(lambda m: m["t"] == "peer_leave")
        h.send({"t": "ping"})
        h.wait(lambda m: m["t"] == "pong")
        assert r.totals["kicked"] == 1
        h.close()
    finally:
        r.stop()
    print("5 max_msgs_per_min OK:", err["msg"])


def t6_message_size():
    r, port = relay(max_message_mb=0.01)  # ~10 KB
    try:
        for scheme in ("ws", "tcp"):
            h = trt.RawClient(nl, f"{scheme}://127.0.0.1:{port}", "Host", "host")
            g = trt.RawClient(nl, f"{scheme}://127.0.0.1:{port}", "G", "guest", h.welcome["room"])
            g.send({"t": "xform", "pad": "x" * 5000})
            h.wait(lambda m: m.get("t") == "xform")
            g.send({"t": "xform", "pad": "x" * 50000})
            err = g.wait(lambda m: m["t"] == "error")
            assert "too large" in err["msg"], err
            assert closed(g)
            h.wait(lambda m: m["t"] == "peer_leave")
            h.close()
    finally:
        r.stop()
    print("6 max_message_mb OK (ws + tcp)")


def t7_byte_rate():
    r, port = relay(max_mb_per_min=0.05)  # ~52 KB per minute
    try:
        h = trt.RawClient(nl, f"ws://127.0.0.1:{port}", "Host", "host")
        g = trt.RawClient(nl, f"ws://127.0.0.1:{port}", "G", "guest", h.welcome["room"])
        for _ in range(6):
            g.send({"t": "xform", "pad": "x" * 15000})
        err = g.wait(lambda m: m["t"] == "error")
        assert "data per minute" in err["msg"], err
        assert closed(g)
        h.close()
    finally:
        r.stop()
    print("7 max_mb_per_min OK")


def t8_hello_timeout():
    r, port = relay(hello_timeout=1)
    try:
        so = socket.create_connection(("127.0.0.1", port))
        so.settimeout(5)
        t0 = time.time()
        assert so.recv(10) == b"", "relay must close a silent connection"
        assert time.time() - t0 < 3
        so.close()
        h = trt.RawClient(nl, f"ws://127.0.0.1:{port}", "Host", "host", "")
        time.sleep(1.5)  # after hello the idle timeout (default 300 s) applies instead
        h.send({"t": "ping"})
        h.wait(lambda m: m["t"] == "pong")
        h.close()
    finally:
        r.stop()
    print("8 hello_timeout OK")


def main():
    t1_routing()
    t2_stats()
    t3_max_connections()
    t4_max_rooms()
    t5_msg_rate()
    t6_message_size()
    t7_byte_rate()
    t8_hello_timeout()
    print("VPS RELAY PASS")


if __name__ == "__main__":
    main()
