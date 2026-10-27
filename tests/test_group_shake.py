# SPDX-License-Identifier: GPL-2.0-or-later
"""
Two-instance test: shaking a parent (a VR Studio group, e.g. the pudding = Empty + custard + caramel) must not
make its children drift away (no VR needed).

    python3 tests/test_group_shake.py direct | relay

The bug: an xform message carries the parent AND its children (world matrices). The receiver set them in
message order and set a child's world matrix against its parent's matrix_world, which lags until the next
depsgraph evaluation -> the child landed off by the parent's last move, the receiver then saw the child
"moved" and sent that back, and the holder's pudding crept out of the hand while being shaken.
Checks: HOST shakes the group for ~3 s; on both PCs every child keeps its offset to the parent, and the
host's children end exactly where the host put them.
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


def _offsets():
    """Each child's position in its parent's space, from fully evaluated matrices."""
    import bpy

    bpy.context.view_layer.update()
    g = bpy.data.objects["Pudding"]
    return {n: tuple(round(v, 3) for v in (g.matrix_world.inverted() @ bpy.data.objects[n].matrix_world).translation)
            for n in ("Custard", "Caramel")}


def run(role, mode):
    import bpy

    sys.path.insert(0, ROOT)
    from freebird_collab.session import CollabSession

    s = CollabSession()
    other = "guest" if role == "host" else "host"
    if role == "host":
        g = bpy.data.objects.new("Pudding", None)
        g.location = (0, 0, 1)
        bpy.context.scene.collection.objects.link(g)
        for n, z in (("Custard", 0.0), ("Caramel", 0.3)):
            bpy.ops.mesh.primitive_cylinder_add(radius=0.2, depth=0.3, location=(0.5, 0, 1 + z))
            ob = bpy.context.object
            ob.name = n
            bpy.context.view_layer.update()
            ob.parent = g
            ob.matrix_parent_inverse = g.matrix_world.inverted()
        bpy.context.view_layer.update()
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
    start = _offsets()
    _write_state(**{f"{role}_ready": True})
    _wait(lambda: _read_state().get(f"{other}_ready"), 30, s, f"{other} ready")
    _idle(s, 0.5)

    if role == "host":
        g = bpy.data.objects["Pudding"]
        t0 = time.time()
        while time.time() - t0 < 3.0:  # shake it like Freebird's grab does: move + rotate the parent each frame
            t = time.time() - t0
            g.location = (0.15 * math.sin(t * 25), 0.1 * math.cos(t * 19), 1 + 0.1 * math.sin(t * 31))
            g.rotation_euler = (0.3 * math.sin(t * 17), 0.0, 0.4 * math.sin(t * 23))
            bpy.context.view_layer.update()  # one redraw
            s.tick()
            time.sleep(1 / 60)
        _write_state(host_shaken=True)
    else:
        _wait(lambda: _read_state().get("host_shaken"), 30, s, "host shaking")
    _idle(s, 2.0)
    end = _offsets()
    for n in end:
        assert all(abs(a - b) < 1e-3 for a, b in zip(start[n], end[n])), f"{role}: {n} drifted {start[n]} -> {end[n]}"
    print(f"{role.upper()} OK: children kept their place in the group {end}")
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
    print(f"\n=== GROUP SHAKE {mode.upper()}: host rc={rc_h} guest rc={rc_g} ===")
    sys.exit(0 if rc_h == 0 and rc_g == 0 else 1)


if __name__ == "__main__":
    main()
