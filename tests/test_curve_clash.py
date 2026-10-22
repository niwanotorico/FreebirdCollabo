# SPDX-License-Identifier: GPL-2.0-or-later
"""
Two-instance test of Freebird pen strokes drawn at the same time (Issue #12, no VR needed).

    python3 tests/test_curve_clash.py direct | relay
    blender --background --factory-startup --python tests/blender_runner.py -- tests/test_curve_clash.py direct

The bug: Freebird names every new pen stroke "pen" / "pen.001" / ... on every Blender. When HOST and GUEST
draw at the same moment, both strokes get the same name, the peer's obj_add "upserted" into the stroke still
being drawn here, cleared its splines, and Freebird's stroke end crashed
(IndexError: bpy_prop_collection[-1]: out of range in draw_stroke.py end()). Checks:
  1  both draw "pen" at once: neither stroke end crashes, and both PCs end up with the same two curves
     ("pen" = smaller uid's stroke, "pen.c01" = the other one) with the right point counts
  3  same, but both strokes were already announced (obj_adds cross in flight): the smaller uid keeps the name,
     the other side renames its own stroke and re-announces it; stale messages about the old name are dropped
  2  host draws into (extends) the shared curve "Line" while the guest edits it: the host's spline stays valid
     during the stroke, and the host's finished stroke wins on both PCs
"""

import os
import subprocess
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_sync import MODE_URLS, PORT, ROOT, STATE, _read_state, _write_state  # noqa: E402

RELAY_URL = os.environ.get("COLLAB_RELAY_URL", "")


def _wait(cond, timeout, s, what):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s.tick()
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(0.02)
    raise AssertionError(f"timeout waiting for {what}")


def _idle(s, secs=1.0):
    t0 = time.time()
    while time.time() - t0 < secs:
        s.tick()
        time.sleep(0.02)


def _barrier(name, timeout=30):
    """Wait for the other process WITHOUT ticking: nothing from the peer is read meanwhile."""
    t0 = time.time()
    while not _read_state().get(name):
        if time.time() - t0 > timeout:
            raise AssertionError(f"timeout waiting for {name}")
        time.sleep(0.02)


def _flag(name, s, timeout=30):
    _wait(lambda: _read_state().get(name), timeout, s, name)


# -- a stand-in for freebird/tools/draw_stroke.py (same module name, same curr_stroke / cu / curve fields) --
_fb = types.ModuleType("freebird.tools.draw_stroke")
_fb.curr_stroke = None
sys.modules["freebird.tools.draw_stroke"] = _fb


class FakeStroke:
    def __init__(self, cu, spline):
        self.cu = cu
        self.curve = spline

    def add_pt(self, x):
        self.curve.points.add(1)
        self.curve.points[-1].co = (x, 0.0, 0.0, 1.0)  # touches [-1]: what crashed in Freebird
        import bpy

        self.cu.update_tag()
        bpy.context.view_layer.update()  # real Blender evaluates the depsgraph on every redraw

    def end(self):
        n = len(self.curve.points)  # Freebird's end() does exactly this first
        self.curve.points[-1].co  # noqa: B018 and reads the last point
        return n


def start_new_stroke(name="pen"):
    import bpy

    cu = bpy.data.curves.new(name, "CURVE")
    cu.dimensions = "3D"
    ob = bpy.data.objects.new(name, cu)
    bpy.context.scene.collection.objects.link(ob)
    sp = cu.splines.new("NURBS")  # one point already
    _fb.curr_stroke = FakeStroke(cu, sp)
    return ob, _fb.curr_stroke


def draw(s, stroke, n_points, secs=1.0):
    for i in range(n_points):
        stroke.add_pt(float(i))
        _idle(s, secs / n_points)


def curves():
    import bpy

    return {ob.name: sum(len(sp.points) for sp in ob.data.splines) for ob in bpy.data.objects if ob.type == "CURVE"}


def run(role, mode):
    import bpy

    sys.path.insert(0, ROOT)
    from freebird_collab.session import CollabSession

    s = CollabSession()
    if role == "host":
        if mode == "direct":
            assert s.create_room("DIRECT", "Host", (1, 0.5, 0.2), direct_port=PORT)
        else:
            assert s.create_room("RELAY", "Host", (1, 0.5, 0.2), relay_url=RELAY_URL)
        _wait(lambda: s.uid is not None, 5, s, "welcome")
        _write_state(room=s.room)
        _wait(lambda: len(s.peers) == 1, 20, s, "guest join")
    else:
        _wait(lambda: _read_state().get("room"), 20, s, "room code")
        if mode == "direct":
            assert s.join_room("DIRECT", "Guest", (0.2, 0.6, 1), direct_host="127.0.0.1", direct_port=PORT)
        else:
            assert s.join_room("RELAY", "Guest", (0.2, 0.6, 1), code=_read_state()["room"], relay_url=RELAY_URL)
        _wait(lambda: s._scene_loaded_from_host, 20, s, "scene from host")
    _write_state(**{f"{role}_uid": s.uid})
    other = "guest" if role == "host" else "host"
    _flag(f"{other}_uid", s)

    # 1: both start a "pen" stroke in the same instant (no tick in between), draw, end
    _write_state(**{f"{role}_ready1": True})
    _flag(f"{other}_ready1", s)
    ob, stroke = start_new_stroke("pen")
    assert ob.name == "pen", ob.name
    n_mine = 5 if role == "host" else 8
    draw(s, stroke, n_mine - 1, 1.5)
    n = stroke.end()  # crashed before the fix when the peer's "pen" was applied into ours
    _fb.curr_stroke = None
    assert n == n_mine, f"stroke has {n} points, drew {n_mine}"
    print(f"{role.upper()} 1: stroke end OK ({n} points, object now {ob.name})")
    # which stroke keeps "pen" depends on timing (smaller uid, or the one announced first): both PCs must agree
    _wait(lambda: sorted(curves()) == ["pen", "pen.c01"] and sorted(curves().values()) == [5, 8], 15, s,
          f"both curves ({curves()})")
    _idle(s, 1.0)
    mine = curves()
    assert mine[ob.name] == n_mine, (ob.name, mine)
    _write_state(**{f"{role}_curves1": mine})
    _flag(f"{other}_curves1", s)
    assert _read_state()[f"{other}_curves1"] == mine, (mine, _read_state()[f"{other}_curves1"])
    print(f"{role.upper()} 1 OK: {mine} on both PCs")

    # 3: both strokes are ANNOUNCED before either side hears of the other's (the obj_adds cross in flight):
    #    the smaller uid keeps "brush", the other renames its own and re-announces it ("was")
    _idle(s, 0.5)
    _write_state(**{f"{role}_ready3": True})
    _barrier(f"{other}_ready3")
    ob, stroke = start_new_stroke("brush")
    assert ob.name == "brush", ob.name
    s._sync_objects()  # announce now, before reading anything from the peer
    _write_state(**{f"{role}_announced3": True})
    _barrier(f"{other}_announced3")
    n_mine = 4 if role == "host" else 7
    draw(s, stroke, n_mine - 1, 1.0)
    assert stroke.end() == n_mine
    _fb.curr_stroke = None
    i_win = str(s.uid) < str(_read_state()[f"{other}_uid"])
    assert ob.name == ("brush" if i_win else "brush.c01"), (ob.name, i_win)
    want = {"brush": 4, "brush.c01": 7} if (role == "host") == i_win else {"brush": 7, "brush.c01": 4}
    _wait(lambda: {k: v for k, v in curves().items() if k.startswith("brush")} == want, 15, s, f"brush curves ({curves()})")
    _idle(s, 1.0)
    assert {k: v for k, v in curves().items() if k.startswith("brush")} == want, curves()
    print(f"{role.upper()} 3 OK: crossed announcements resolved by uid: {want}")
    _write_state(**{f"{role}_done3": True})
    _flag(f"{other}_done3", s)

    # 2: host extends the shared curve "Line" while the guest edits it
    if role == "host":
        ob, _ = start_new_stroke("Line")
        _fb.curr_stroke = None
        for x in range(3):
            ob.data.splines[0].points.add(1)
            ob.data.splines[0].points[-1].co = (x, 1.0, 0.0, 1.0)
        ob.data.update_tag()
        bpy.context.view_layer.update()
        _wait(lambda: _read_state().get("guest_has_line"), 20, s, "guest has Line")
        stroke = FakeStroke(ob.data, ob.data.splines[0])
        _fb.curr_stroke = stroke
        _write_state(host_drawing=True)
        _flag("guest_edited", s)
        draw(s, stroke, 6, 1.5)  # the guest's edit arrives meanwhile: must be held, not rebuild our spline
        n = stroke.end()
        _fb.curr_stroke = None
        assert n == 10, n
        print("HOST 2: extend stroke end OK")
        _idle(s, 2.0)
        assert curves()["Line"] == 10, curves()
        _write_state(host_line=10)
        _flag("guest_done2", s)
        print("HOST 2 OK: our stroke kept")
    else:
        _wait(lambda: curves().get("Line") == 4, 20, s, "Line from host")
        _write_state(guest_has_line=True)
        _flag("host_drawing", s)
        ln = bpy.data.objects["Line"]
        ln.data.bevel_depth = 0.05
        ln.data.splines[0].points.add(1)
        ln.data.update_tag()
        bpy.context.view_layer.update()
        _idle(s, 0.5)
        _write_state(guest_edited=True)
        _flag("host_line", s)
        _wait(lambda: curves().get("Line") == 10, 15, s, f"host's stroke wins ({curves()})")
        _write_state(guest_done2=True)
        print("GUEST 2 OK: host's finished stroke arrived")
    _idle(s, 0.5)
    s.leave()
    print(f"{role.upper()} PASS")


def _args():
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]


def _child_command(mode, role):
    try:
        import bpy

        blender = bpy.app.binary_path
    except ImportError:
        blender = ""
    if blender:
        return [blender, "--background", "--factory-startup", "--python", __file__, "--", mode, role]
    return [sys.executable, __file__, mode, role]


def main():
    args = _args()
    mode = args[0] if args else "direct"
    role = args[1] if len(args) > 1 else None
    if role in ("host", "guest"):
        try:
            run(role, mode)
            code = 0
        except BaseException:
            import traceback

            traceback.print_exc()
            code = 1
        sys.stdout.flush()
        os._exit(code)  # the bpy wheel can hang in its exit handlers

    import glob

    for f in glob.glob(STATE + "*"):
        os.remove(f)
    procs = []
    env = dict(os.environ)
    if mode in MODE_URLS:
        env["COLLAB_RELAY_URL"] = os.environ.get("COLLAB_RELAY_URL") or MODE_URLS[mode]
        if not os.environ.get("COLLAB_RELAY_URL"):
            procs.append(subprocess.Popen([sys.executable, os.path.join(ROOT, "relay", "collab_relay.py"), str(PORT)]))
            time.sleep(1.0)
    run_mode = "direct" if mode == "direct" else "relay"
    host = subprocess.Popen(_child_command(run_mode, "host"), env=env)
    time.sleep(1.0)
    guest = subprocess.Popen(_child_command(run_mode, "guest"), env=env)
    rc_h, rc_g = host.wait(240), guest.wait(240)
    for p in procs:
        p.terminate()
    print(f"\n=== CURVE CLASH {mode.upper()}: host rc={rc_h} guest rc={rc_g} ===")
    sys.exit(0 if rc_h == 0 and rc_g == 0 else 1)


if __name__ == "__main__":
    main()
