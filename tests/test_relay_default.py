# SPDX-License-Identifier: GPL-2.0-or-later
"""
Relay URL preset test (single process, no VR).

    python3 tests/test_relay_default.py          # preferences / public API only
    python3 tests/test_relay_default.py live     # + Check Relay against the real preset relay (needs internet)

Checks:
  1  Relay URL field defaults to DEFAULT_RELAY_URL, Connection defaults to RELAY, bl_info == ADDON_VERSION
  2  create_room() / join_room(code) hand the preset URL to the session (user types only the Room Code)
  3  a user-edited Relay URL is used as-is; an empty field falls back to the preset
  4  Reset Relay URL operator restores the preset
  5  DIRECT mode still passes host / port and never touches the relay URL
  6  end-to-end through the public API: create_room() on a local relay, then join_room(code)
"""

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bpy  # noqa: E402  (must come first: it puts addon_utils on sys.path)
import addon_utils  # noqa: E402


def main():
    live = "live" in sys.argv[1:]
    mod = addon_utils.enable("freebird_collab", default_set=True, persistent=True)
    assert mod is not None, "add-on failed to enable"
    fc = sys.modules["freebird_collab"]
    p = bpy.context.preferences.addons["freebird_collab"].preferences

    # 1 defaults
    assert fc.DEFAULT_RELAY_URL.startswith("wss://"), fc.DEFAULT_RELAY_URL
    assert p.relay_url == fc.DEFAULT_RELAY_URL, p.relay_url
    assert p.mode == "RELAY", p.mode
    from freebird_collab import session as sessmod

    assert fc.bl_info["version"] == (0, 11, 1), fc.bl_info["version"]
    assert sessmod.ADDON_VERSION == ".".join(map(str, fc.bl_info["version"])), sessmod.ADDON_VERSION
    print("PASS 1 defaults:", p.mode, p.relay_url, "v" + sessmod.ADDON_VERSION)

    # 2/3/5 capture what the public API hands to the session
    calls = []
    real_create, real_join = fc._session.create_room, fc._session.join_room
    fc._session.create_room = lambda *a, **k: calls.append(("create", a, k)) or True
    fc._session.join_room = lambda *a, **k: calls.append(("join", a, k)) or True
    try:
        fc.create_room()
        fc.join_room(" abc123 ")
        assert calls[0][2]["relay_url"] == fc.DEFAULT_RELAY_URL
        assert calls[1][2]["relay_url"] == fc.DEFAULT_RELAY_URL and calls[1][2]["code"] == "ABC123"
        print("PASS 2 room-code only: preset URL used, code normalised")

        calls.clear()
        p.relay_url = "  ws://192.168.1.20:7788 "
        fc.create_room()
        assert calls[-1][2]["relay_url"] == "ws://192.168.1.20:7788"
        p.relay_url = ""
        fc.join_room("XYZ999")
        assert calls[-1][2]["relay_url"] == fc.DEFAULT_RELAY_URL
        print("PASS 3 custom URL kept, empty field falls back to preset")

        p.relay_url = "wss://someone-else.example"
        assert bpy.ops.collab.reset_relay_url() == {"FINISHED"}
        assert p.relay_url == fc.DEFAULT_RELAY_URL
        print("PASS 4 Reset Relay URL")

        calls.clear()
        p.mode = "DIRECT"
        p.direct_port = 7790
        fc.create_room()
        fc.join_room("192.168.1.5:7791")
        fc.join_room("10.0.0.2")
        assert calls[0][1][0] == "DIRECT" and calls[0][2]["direct_port"] == 7790
        assert calls[1][1][0] == "DIRECT" and calls[1][2] == {"direct_host": "192.168.1.5", "direct_port": 7791}
        assert calls[2][2] == {"direct_host": "10.0.0.2", "direct_port": 7790}
        print("PASS 5 DIRECT unchanged")
    finally:
        fc._session.create_room, fc._session.join_room = real_create, real_join
        p.mode = "RELAY"

    # 6 real round trip through a local relay using only the public API
    from freebird_collab import hub as hubmod

    relay = hubmod.Hub("127.0.0.1", 7811, log=lambda *a: None)
    relay.start()
    p.relay_url = "tcp://127.0.0.1:7811"
    s = fc._session
    assert fc.create_room()
    t0 = time.time()
    while not s.room and time.time() - t0 < 10:
        s.tick()
        time.sleep(0.02)
    assert s.room, f"no room code ({s.status} {s.error})"
    code = s.room
    print("PASS 6a create_room() on relay ->", code)
    fc.leave_room()
    # guest side: join with the code only (room is gone after leave, so expect a clean 'not found')
    assert fc.join_room(code.lower())
    t0 = time.time()
    while time.time() - t0 < 5 and not s.error:
        s.tick()
        time.sleep(0.02)
    print("PASS 6b join_room(code) reached the relay:", s.error or s.status)
    fc.leave_room()
    relay.stop()
    bpy.ops.collab.reset_relay_url()

    if live:
        try:
            bpy.ops.collab.check_relay()
        except RuntimeError:
            pass  # the operator reports NG as an ERROR, which bpy.ops raises in background mode
        print("LIVE Check Relay:", fc._relay_check_result)
        assert fc._relay_check_result.startswith("OK"), fc._relay_check_result
        print("PASS live preset relay answers")

    print("ALL PASS")


if __name__ == "__main__":
    main()
