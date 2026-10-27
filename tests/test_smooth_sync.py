# SPDX-License-Identifier: GPL-2.0-or-later
"""
Two-instance test: Shade Smooth / Flat syncs with the mesh (Issue #14, no VR needed).

    python3 tests/test_smooth_sync.py direct | relay

Before: obj_add / obj_data carried no shading, and from_pydata makes every face flat, so a smooth
object (VR Studio's Smooth button, the pudding) was faceted on everyone else's PC. Checks:
  1  host shades a sphere Smooth -> smooth on the guest
  2  guest moves a vertex (geometry resend) -> stays smooth on both
  3  host shades it Flat -> flat on the guest; mixed per-face shading arrives face by face
  4  a payload from an older add-on (no "sm") keeps the receiver's shading
"""

import math
import os
import subprocess
import sys
import time

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


def _idle(s, secs):
    t0 = time.time()
    while time.time() - t0 < secs:
        s.tick()
        time.sleep(0.02)


def _smooth(name):
    import bpy

    return [p.use_smooth for p in bpy.data.objects[name].data.polygons]


def run(role, mode):
    import bpy

    sys.path.insert(0, ROOT)
    from freebird_collab import object_data
    from freebird_collab.session import CollabSession

    s = CollabSession()
    other = "guest" if role == "host" else "host"
    if role == "host":
        bpy.ops.mesh.primitive_uv_sphere_add(radius=1, location=(0, 0, 0))
        bpy.context.object.name = "Ball"
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
    _write_state(**{f"{role}_ready": True})
    _wait(lambda: _read_state().get(f"{other}_ready"), 30, s, f"{other} ready")
    _idle(s, 0.5)
    me = lambda: bpy.data.objects["Ball"].data  # noqa: E731

    if role == "host":
        me().shade_smooth()
        me().update()
        bpy.context.view_layer.update()
        _wait(lambda: _read_state().get("g1"), 15, s, "guest smooth")
        _wait(lambda: _read_state().get("g2"), 15, s, "guest moved a vertex")
        _wait(lambda: abs(me().vertices[0].co.z - 1.5) < 1e-3, 15, s, "guest's vertex move")
        _idle(s, 1.0)
        assert all(_smooth("Ball")), "host lost Smooth after the guest's geometry edit"
        print("HOST 2 OK: still smooth after the guest's edit")
        me().shade_flat()
        me().polygons[0].use_smooth = True
        me().update()
        bpy.context.view_layer.update()
        _write_state(h3=True)
        _wait(lambda: _read_state().get("g3"), 15, s, "guest flat")
    else:
        _wait(lambda: all(_smooth("Ball")), 15, s, "smooth from host")
        print("GUEST 1 OK: smooth arrived")
        _write_state(g1=True)
        me().vertices[0].co.z = 1.5
        me().update()
        bpy.context.view_layer.update()
        _idle(s, 1.0)
        assert all(_smooth("Ball"))
        _write_state(g2=True)
        _wait(lambda: _read_state().get("h3"), 15, s, "host flat")
        _wait(lambda: _smooth("Ball")[0] and not any(_smooth("Ball")[1:]), 15, s, "mixed shading from host")
        print("GUEST 3 OK: flat + one smooth face arrived")
        # 4: older add-on payload without "sm" keeps ours
        payload = object_data.serialize(bpy.data.objects["Ball"])
        payload["data"].pop("sm")
        before = _smooth("Ball")
        object_data.apply(bpy.data.objects["Ball"], payload)
        assert _smooth("Ball") == before
        print("GUEST 4 OK: old payload kept our shading")
        _write_state(g3=True)
    _write_state(**{f"{role}_done": True})
    _wait(lambda: _read_state().get(f"{other}_done"), 30, s, f"{other} done")
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
    print(f"\n=== SMOOTH SYNC {mode.upper()}: host rc={rc_h} guest rc={rc_g} ===")
    sys.exit(0 if rc_h == 0 and rc_g == 0 else 1)


if __name__ == "__main__":
    main()
