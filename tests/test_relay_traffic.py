# SPDX-License-Identifier: GPL-2.0-or-later
"""
Relay traffic reduction + compatibility regression test (docs/relay-load-analysis.md).

    python3 tests/test_relay_traffic.py unit          # plain Python, no Blender: traffic.py + hub routing
    python3 tests/test_relay_traffic.py live          # bpy: 3 Blender processes, one of them v0.12.1
    python3 tests/test_relay_traffic.py live-oldrelay # same, but the relay is the v0.12.1 hub.py
    python3 tests/test_relay_traffic.py               # all of the above

    COLLAB_RELAY_URL=ws://127.0.0.1:8787 python3 tests/test_relay_traffic.py unit   # against `wrangler dev`
    COLLAB_RELAY_URL=ws://127.0.0.1:8787 python3 tests/test_relay_traffic.py live

The v0.12.1 add-on (and relay) is taken from git (LEGACY_REF), or from COLLAB_LEGACY_ROOT
(a folder that contains freebird_collab/ and relay/ of an old release).

unit
  1  presence dead band / heartbeat / alone / reset, batch_groups order and size limit
  2  4 people join one room (host + 3 guests); welcome.batch = 1; everyone gets peer_join
  3  one batch -> every peer receives the separate messages, "from" stamped, "to" honoured,
     nested hello / batch ignored; no receiver ever sees a "batch" (= what a v0.12.1 add-on gets)
  4  ping -> pong; unknown room -> error; message before hello -> error
  5  disconnect -> peer_leave to the rest; reconnect with the same code -> peer_join; the host
     drops and comes back to the same room while guests stay
live (host = new, guest A = new, guest B = v0.12.1)
  6  3 people join; host alone sends no presence while waiting
  7  host Cube move reaches both guests (the old guest gets it through the relay's unbatching)
  8  6 s idle: host sends only heartbeats (counted on its socket) and nobody's presence ever goes
     stale on either side (old receivers hide a peer after 3 s)
  9  the v0.12.1 guest moves the Cube -> host and new guest see it
 10  guest A leaves -> host and B see peer_leave; A rejoins -> scene, Cube, presence again
"""

import io
import json
import os
import socket
import subprocess
import sys
import tarfile
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 7811
STATE = os.environ.get("COLLAB_TRAFFIC_STATE") or os.path.join(tempfile.gettempdir(), "collab_traffic_state.json")
LEGACY_REF = "da4b3fd"  # v0.12.1 (networking identical to the released zip)
STALE_SECONDS = 3.0  # presence.STALE_SECONDS of v0.12.x receivers


def _netlib():
    sys.path.insert(0, os.path.join(ROOT, "relay"))
    import _netlib

    return _netlib


def _load_traffic():
    import importlib.util

    spec = importlib.util.spec_from_file_location("fb_traffic", os.path.join(ROOT, "freebird_collab", "traffic.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ----------------------------------------------------------------------
# unit
# ----------------------------------------------------------------------
def unit_traffic():
    tr = _load_traffic()
    base = {"vr": True, "head": [0, 0, 1.6, 1, 0, 0, 0], "hands": {"L": [0, 0, 1, 1, 0, 0, 0], "R": [0, 0, 1, 1, 0, 0, 0]},
            "tool": "t", "sel": [], "mode": "OBJECT", "name": "a", "color": [1, 1, 1]}
    jitter = json.loads(json.dumps(base))
    jitter["head"][0] += 0.0005
    jitter["hands"]["R"][3] -= 0.001
    assert not tr.presence_changed(base, jitter), "tracking noise must not count as a change"
    moved = json.loads(json.dumps(base))
    moved["head"][1] += 0.01
    assert tr.presence_changed(base, moved)
    sel = dict(base, sel=["Cube"])
    assert tr.presence_changed(base, sel)
    assert tr.presence_changed(base, dict(base, hands=None))
    assert tr.presence_changed(dict(base, vr=False, head=None, hands=None), base)

    g = tr.PresenceThrottle()
    assert not g.should_send(0.0, base, has_peers=False), "alone: nothing goes out"
    assert g.should_send(0.1, base, True), "first one after a peer arrives goes out at once"
    sent = [t / 20 for t in range(3, 60) if g.should_send(t / 20, base, True)]  # 3 s unchanged
    assert 2 <= len(sent) <= 3 and all(b - a >= tr.PRESENCE_HEARTBEAT - 1e-9 for a, b in zip([0.1] + sent, sent)), sent
    assert g.should_send(3.1, sel, True), "a change goes out at once"
    assert not g.should_send(3.12, dict(sel, sel=["X"]), True), "but never faster than PRESENCE_HZ"
    g.reset()
    assert g.should_send(3.2, sel, True), "reset (peer_join) sends at once"
    assert tr.PRESENCE_HEARTBEAT < STALE_SECONDS / 2

    msgs = [{"t": "presence", "a": 1}, {"t": "xform", "objs": {}}, {"t": "scene", "blend_b64": "x"},
            {"t": "ver", "to": "u1", "v": "1"}, {"t": "ping"}, {"t": "pose", "name": "A"}]
    out = tr.batch_groups(msgs)
    assert [m["t"] for m in out] == ["batch", "scene", "ver", "ping", "pose"], out
    flat = []
    for m in out:
        flat += m["m"] if m["t"] == "batch" else [m]
    assert flat == msgs, "batching must keep the order"
    big = [{"t": "xform", "objs": {"o%d" % i: [0.123456] * 16 for i in range(1500)}} for _ in range(3)]
    for m in tr.batch_groups(big):
        assert len(json.dumps(m)) <= tr.BATCH_MAX_BYTES + 100
    print("1 traffic.py OK")


class RawClient:
    """A client that speaks the wire protocol directly through link.Link (never batches on its own,
    understands no "batch") = what a v0.12.1 add-on sees on the wire."""

    def __init__(self, nl, url, name, role, room=""):
        self.l = nl.link.Link(url, log=lambda *a: None)
        self.l.connect()
        self.l.send({"t": "hello", "name": name, "role": role, "room": room, "color": [1, 0, 0]})
        self.got = []
        self.welcome = self.wait(lambda m: m["t"] in ("welcome", "error"))

    def pump(self):
        new = self.l.poll()
        self.got += new
        return new

    def wait(self, pred, timeout=5.0):
        t0 = time.time()
        seen = 0
        while time.time() - t0 < timeout:
            self.pump()
            for m in self.got[seen:]:
                if pred(m):
                    return m
            seen = len(self.got)
            time.sleep(0.01)
        raise AssertionError(f"timeout; got {[m.get('t') for m in self.got]}")

    def quiet(self, secs=0.3):
        time.sleep(secs)
        return self.pump()

    def send(self, m):
        self.l.send(m)

    def close(self):
        self.l.close()


def unit_hub(url=None):
    nl = _netlib()
    hub = None
    if not url:
        hub = nl.hub.Hub("127.0.0.1", 0, log=lambda *a: None)
        url = f"ws://127.0.0.1:{hub.start()}"
    try:
        h = RawClient(nl, url, "Host", "host")
        assert h.welcome["t"] == "welcome" and h.welcome.get("batch") == 1, h.welcome
        code = h.welcome["room"]
        guests = []
        for i in range(3):
            g = RawClient(nl, url, f"G{i}", "guest", code)
            assert g.welcome["t"] == "welcome" and len(g.welcome["peers"]) == i + 1, g.welcome
            guests.append(g)
        for c in [h] + guests[:-1]:
            c.wait(lambda m: m["t"] == "peer_join" and m["name"] == "G2")
        print("2 host + 3 guests in room", code, "OK")

        g0, g1, g2 = guests
        uid_g2 = g2.welcome["uid"]
        batch = {"t": "batch", "m": [
            {"t": "presence", "sel": ["Cube"]},
            {"t": "xform", "objs": {"Cube": [1] * 16}},
            {"t": "ver", "to": uid_g2, "v": "x"},
            {"t": "hello", "name": "evil", "role": "host", "room": "ZZZZZZ"},
            {"t": "batch", "m": [{"t": "obj_del", "names": ["nested"]}]},
            "junk",
        ]}
        g0.send(batch)
        uid_g0 = g0.welcome["uid"]
        for c in (h, g1, g2):
            c.wait(lambda m: m["t"] == "xform")
            got = c.quiet()
            types = [m["t"] for m in c.got if m["t"] not in ("peer_join",)]
            assert "batch" not in types and "obj_del" not in types and "welcome" not in types[1:], types
            pres = [m for m in c.got if m["t"] == "presence"]
            assert pres and pres[-1]["from"] == uid_g0 and pres[-1]["sel"] == ["Cube"], pres
            vers = [m for m in c.got if m["t"] == "ver"]
            assert (len(vers) == 1) == (c is g2), (c.welcome["uid"], vers)
            i_p = types.index("presence")
            assert types[i_p + 1] == "xform", f"order kept: {types}"
            del got
        g0.quiet()
        assert not any(m["t"] in ("presence", "xform") for m in g0.got), "never echoed to the sender"
        print("3 batch unpacked: order, from, to, nested hello/batch ignored, old receivers see plain messages OK")

        g1.send({"t": "ping"})
        g1.wait(lambda m: m["t"] == "pong")
        bad = RawClient(nl, url, "X", "guest", "NOROOM")
        assert bad.welcome["t"] == "error" and "not found" in bad.welcome["msg"], bad.welcome
        bad.close()
        lone = nl.link.Link(url, log=lambda *a: None)
        lone.connect()
        lone.send({"t": "xform", "objs": {}})
        t0 = time.time()
        err = None
        while time.time() - t0 < 5 and err is None:
            err = next((m for m in lone.poll() if m.get("t") == "error"), None)
            time.sleep(0.01)
        assert err and "not in a room" in err["msg"], err
        lone.close()
        print("4 ping/pong, unknown room, not in a room OK")

        g2.close()
        for c in (h, g0, g1):
            c.wait(lambda m: m["t"] == "peer_leave" and m["uid"] == uid_g2)
        g2b = RawClient(nl, url, "G2", "guest", code)
        assert g2b.welcome["t"] == "welcome" and len(g2b.welcome["peers"]) == 3, g2b.welcome
        for c in (h, g0, g1):
            c.wait(lambda m: m["t"] == "peer_join" and m["uid"] == g2b.welcome["uid"])
        uid_h = h.welcome["uid"]
        h.close()
        for c in (g0, g1, g2b):
            c.wait(lambda m: m["t"] == "peer_leave" and m["uid"] == uid_h)
        h2 = RawClient(nl, url, "Host", "host", code)
        assert h2.welcome["t"] == "welcome" and h2.welcome["room"] == code and len(h2.welcome["peers"]) == 3, h2.welcome
        for c in (g0, g1, g2b):
            c.wait(lambda m: m["t"] == "peer_join" and m["role"] == "host")
        h2.send({"t": "batch", "m": [{"t": "obj_del", "names": ["A"]}, {"t": "obj_del", "names": ["B"]}]})
        for c in (g0, g1, g2b):
            c.wait(lambda m: m["t"] == "obj_del" and m["names"] == ["B"])
        print("5 disconnect / reconnect (guest and host) OK")
        for c in (h2, g0, g1, g2b):
            c.close()
    finally:
        if hub:
            hub.stop()


# ----------------------------------------------------------------------
# live (bpy, one process per user)
# ----------------------------------------------------------------------
def _write_state(**kw):
    for k, v in kw.items():
        tmp = STATE + f".{k}.tmp"
        with open(tmp, "w") as f:
            json.dump(v, f)
        os.replace(tmp, STATE + f".{k}")


def _read_state():
    import glob

    d = {}
    for f in glob.glob(STATE + ".*"):
        if f.endswith(".tmp"):
            continue
        try:
            with open(f) as fh:
                d[f.rsplit(".", 1)[1]] = json.load(fh)
        except Exception:
            pass
    return d


def _pump(s, secs, every=None):
    t0 = time.time()
    while time.time() - t0 < secs:
        s.tick()
        if every:
            every()
        time.sleep(0.02)


def _wait(cond, timeout, s, what, every=None):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s.tick()
        if every:
            every()
        if cond():
            return True
        time.sleep(0.02)
    raise AssertionError(f"timeout waiting for {what}")


class GapMeter:
    """Longest time a peer's presence was older than now (what presence.py compares with STALE_SECONDS)."""

    def __init__(self, s, name):
        self.s, self.name, self.max = s, name, 0.0

    def __call__(self):
        p = next((p for p in self.s.peers.values() if p.name == self.name), None)
        if p is not None and p.presence:
            self.max = max(self.max, time.time() - p.last_seen)


def _session(root):
    sys.path.insert(0, root)
    import bpy  # noqa: F401
    from freebird_collab.session import CollabSession

    return CollabSession()


def live_host(url):
    import bpy

    s = _session(ROOT)
    bpy.data.objects["Cube"].location = (0.0, 0.0, 0.0)
    for ob in bpy.context.view_layer.objects:
        ob.select_set(False)
    assert s.create_room("RELAY", "Host", (1, 0.5, 0.2), relay_url=url)
    _wait(lambda: s.uid is not None, 10, s, "welcome")
    batching = s.link.can_batch
    _pump(s, 0.5)
    f0 = s.link.frames_tx
    _pump(s, 2.0)
    alone = s.link.frames_tx - f0
    assert alone <= 1, f"alone in the room the host sent {alone} messages in 2 s"
    print(f"HOST 6 alone for 2 s: {alone} messages (batching={batching})")
    _write_state(room=s.room)
    _wait(lambda: len(s.peers) == 2, 60, s, "both guests")
    names = sorted(p.name for p in s.peers.values())
    assert names == ["GuestA", "GuestOld"], names

    bpy.data.objects["Cube"].location = (5.0, 0.0, 0.0)
    bpy.data.objects["Cube"].select_set(True)
    _wait(lambda: _read_state().get("a_saw5") and _read_state().get("b_saw5"), 30, s, "guests see Cube at 5")
    print("HOST 7 Cube move reached the new and the v0.12.1 guest")

    _write_state(idle_start=True)
    ga, gb = GapMeter(s, "GuestA"), GapMeter(s, "GuestOld")
    _pump(s, 1.0, lambda: (ga(), gb()))
    f0, t0 = s.link.frames_tx, time.time()
    _pump(s, 6.0, lambda: (ga(), gb()))
    idle = s.link.frames_tx - f0
    rate = idle / (time.time() - t0)
    print(f"HOST 8 idle 6 s with 2 peers: {idle} messages ({rate:.1f}/s; v0.12.1 sends ~17-20/s); "
          f"presence age max: GuestA {ga.max:.2f}s, GuestOld {gb.max:.2f}s")
    assert idle <= 10, idle
    assert ga.max < STALE_SECONDS and gb.max < STALE_SECONDS, (ga.max, gb.max)
    _write_state(idle_done=True)

    _wait(lambda: abs(bpy.data.objects["Cube"].location.x + 2.0) < 1e-3, 30, s, "old guest Cube move")
    print("HOST 9 v0.12.1 guest moved the Cube")

    _wait(lambda: len(s.peers) == 1, 30, s, "GuestA leaves")
    _write_state(host_saw_leave=True)
    _wait(lambda: len(s.peers) == 2, 30, s, "GuestA rejoins")
    a = next(p for p in s.peers.values() if p.name == "GuestA")
    _wait(lambda: a.presence is not None, 15, s, "GuestA presence after rejoin")
    print("HOST 10 GuestA left and rejoined")
    _wait(lambda: _read_state().get("a_done") and _read_state().get("b_done"), 60, s, "guests done")
    _pump(s, 0.3)
    s.leave()
    print("HOST PASS")


def live_guest_a(url):
    import bpy

    s = _session(ROOT)
    room = _wait_room()
    assert s.join_room("RELAY", "GuestA", (0.2, 1, 0.2), code=room, relay_url=url)
    _wait(lambda: s._scene_loaded_from_host and abs(bpy.data.objects["Cube"].location.x - 5.0) < 1e-3, 60, s, "Cube at 5")
    _write_state(a_saw5=True)
    gh, gb = GapMeter(s, "Host"), GapMeter(s, "GuestOld")
    _wait(lambda: _read_state().get("idle_start"), 30, s, "idle start")
    _pump(s, 1.0)  # skip the burst of the move itself
    gh.max = gb.max = 0.0
    _wait(lambda: _read_state().get("idle_done"), 30, s, "idle done", every=lambda: (gh(), gb()))
    print(f"GUEST A 8 presence age max: Host {gh.max:.2f}s, GuestOld {gb.max:.2f}s")
    assert gh.max < STALE_SECONDS and gb.max < STALE_SECONDS, (gh.max, gb.max)
    _wait(lambda: abs(bpy.data.objects["Cube"].location.x + 2.0) < 1e-3, 30, s, "old guest Cube move")
    s.leave()
    _write_state(a_left=True)
    _wait_state("host_saw_leave", 30)
    assert s.join_room("RELAY", "GuestA", (0.2, 1, 0.2), code=room, relay_url=url)
    _wait(lambda: s._scene_loaded_from_host and abs(bpy.data.objects["Cube"].location.x + 2.0) < 1e-3, 60, s, "scene again")
    _wait(lambda: len(s.peers) == 2 and all(p.presence for p in s.peers.values()), 15, s, "presence after rejoin")
    print("GUEST A 10 rejoined: scene, Cube at -2, both peers' presence")
    _write_state(a_done=True)
    _pump(s, 0.5)
    s.leave()
    print("GUEST A PASS")


def live_guest_old(url, legacy_root):
    import bpy

    s = _session(legacy_root)
    import freebird_collab.session as sm

    assert sm.ADDON_VERSION == "0.12.1" and os.path.abspath(sm.__file__).startswith(os.path.abspath(legacy_root)), sm.__file__
    room = _wait_room()
    assert s.join_room("RELAY", "GuestOld", (0.2, 0.2, 1), code=room, relay_url=url)
    _wait(lambda: s._scene_loaded_from_host and abs(bpy.data.objects["Cube"].location.x - 5.0) < 1e-3, 60, s, "Cube at 5")
    host = next(p for p in s.peers.values() if p.name == "Host")
    _wait(lambda: host.presence and "Cube" in host.presence.get("sel", []), 15, s, "host selection")
    _write_state(b_saw5=True)
    gh, ga = GapMeter(s, "Host"), GapMeter(s, "GuestA")
    _wait(lambda: _read_state().get("idle_start"), 30, s, "idle start")
    _pump(s, 1.0)
    gh.max = ga.max = 0.0
    _wait(lambda: _read_state().get("idle_done"), 30, s, "idle done", every=lambda: (gh(), ga()))
    print(f"GUEST OLD 8 (v0.12.1) presence age max: Host {gh.max:.2f}s, GuestA {ga.max:.2f}s")
    assert gh.max < STALE_SECONDS and ga.max < STALE_SECONDS, (gh.max, ga.max)
    bpy.data.objects["Cube"].location = (-2.0, 0.0, 0.0)
    _wait(lambda: _read_state().get("a_left") and not any(p.name == "GuestA" for p in s.peers.values()), 30, s, "GuestA leave")
    _wait(lambda: any(p.name == "GuestA" and p.presence for p in s.peers.values()), 60, s, "GuestA back")
    print("GUEST OLD 10 saw GuestA leave and come back")
    _write_state(b_done=True)
    _pump(s, 0.5)
    s.leave()
    print("GUEST OLD PASS")


def _wait_state(key, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if _read_state().get(key):
            return
        time.sleep(0.05)
    raise AssertionError(f"timeout waiting for {key}")


def _wait_room():
    _wait_state("room", 60)
    return _read_state()["room"]


def legacy_root():
    root = os.environ.get("COLLAB_LEGACY_ROOT")
    if root:
        return root
    out = os.path.join(tempfile.gettempdir(), f"collab_legacy_{LEGACY_REF}")
    if not os.path.isdir(os.path.join(out, "freebird_collab")):
        data = subprocess.check_output(["git", "-C", ROOT, "archive", LEGACY_REF, "freebird_collab", "relay"])
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            tf.extractall(out)
    return out


def _port_free(port):
    with socket.socket() as so:
        return so.connect_ex(("127.0.0.1", port)) != 0


def run_live(old_relay):
    import glob

    for f in glob.glob(STATE + "*"):
        os.remove(f)
    legacy = legacy_root()
    procs = []
    url = os.environ.get("COLLAB_RELAY_URL")
    if not url:
        assert _port_free(PORT), f"port {PORT} busy"
        relay_root = legacy if old_relay else ROOT
        procs.append(subprocess.Popen([sys.executable, os.path.join(relay_root, "relay", "collab_relay.py"), str(PORT)]))
        url = f"ws://127.0.0.1:{PORT}"
        time.sleep(1.0)
    env = dict(os.environ, COLLAB_TEST_URL=url, COLLAB_LEGACY_ROOT=legacy)
    me = os.path.abspath(__file__)
    env["COLLAB_TRAFFIC_STATE"] = STATE

    def spawn(role):  # own temp dir each: the guests would otherwise both write <tmp>/collab_host_<file>.blend
        tmp = os.path.join(tempfile.gettempdir(), f"collab_traffic{role}")
        os.makedirs(tmp, exist_ok=True)
        return subprocess.Popen([sys.executable, me, role], env=dict(env, TMPDIR=tmp))

    host, a, b = spawn("_host"), spawn("_a"), spawn("_b")
    rcs = [p.wait(240) for p in (host, a, b)]
    for p in procs:
        p.terminate()
        p.wait(10)  # free the port before the next run_live() checks it
    label = "v0.12.1 relay" if old_relay else (os.environ.get("COLLAB_RELAY_URL") or "new relay")
    print(f"\n=== LIVE ({label}): host rc={rcs[0]} guestA rc={rcs[1]} guestOld rc={rcs[2]} ===")
    return all(rc == 0 for rc in rcs)


def _child(fn, *args):
    try:
        fn(*args)
        code = 0
    except BaseException:
        import traceback

        traceback.print_exc()
        code = 1
    sys.stdout.flush()
    os._exit(code)  # the bpy wheel can hang in its exit handlers


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else "all"
    url = os.environ.get("COLLAB_TEST_URL", "")
    if arg == "_host":
        return _child(live_host, url)
    if arg == "_a":
        return _child(live_guest_a, url)
    if arg == "_b":
        return _child(live_guest_old, url, os.environ["COLLAB_LEGACY_ROOT"])
    ok = True
    if arg in ("unit", "all"):
        unit_traffic()
        unit_hub(os.environ.get("COLLAB_RELAY_URL"))
        print("UNIT PASS")
    if arg in ("live", "all"):
        ok &= run_live(old_relay=False)
    if arg in ("live-oldrelay", "all") and not os.environ.get("COLLAB_RELAY_URL"):
        ok &= run_live(old_relay=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
