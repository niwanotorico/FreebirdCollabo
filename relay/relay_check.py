#!/usr/bin/env python3
"""Check that a relay is reachable from this PC (no Blender needed).

    python3 relay_check.py wss://xxx-freebird-relay.hf.space
    python3 relay_check.py ws://192.168.1.10:7788
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _netlib import link as _link, protocol as _protocol  # plain Python, no bpy

Link, make = _link.Link, _protocol.make

url = sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:7788"
t0 = time.time()
try:
    link = Link(url)
    link.connect(timeout=15)
    link.send(make("hello", name="check", role="host", room="", color=[1, 1, 1]))
    deadline = time.time() + 15
    while time.time() < deadline:
        for m in link.poll():
            if m.get("t") == "welcome":
                print(f"OK  relay reachable, test room {m['room']} created  ({(time.time()-t0)*1000:.0f} ms)")
                link.close(); sys.exit(0)
            if m.get("t") in ("error", "_disconnected"):
                print("NG ", m); sys.exit(1)
        time.sleep(0.05)
    print("NG  no answer from relay (timeout)"); sys.exit(1)
except Exception as e:
    print("NG ", e); sys.exit(1)
