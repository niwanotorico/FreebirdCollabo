# SPDX-License-Identifier: GPL-2.0-or-later
"""
Two-instance test of the per-user Undo / Redo in a room (no VR needed).

    python3 tests/test_undo.py unit | direct | relay
    blender --background --factory-startup --python tests/blender_runner.py -- tests/test_undo.py direct

"unit" checks only how history.py groups changes into undo steps (no bpy); direct / relay run it first.

The bug it guards against: Blender's own Undo rewinds the whole scene, so undoing your own move also wiped
the other user's work (locally, and then on their side through the sync). Checks, with HOST and GUEST:
  1  host undoes its Cube move while the guest moved its own Sphere meanwhile: Cube back, Sphere stays (both PCs)
  2  host redo: Cube moves again on both
  3  conflict: guest moves the Cube after the host did -> host undo keeps the guest's position
  4  host adds a Box -> undo removes it on both, redo brings it back; host deletes it -> undo brings it back
  5  material Base Color change -> undo restores the old colour on both
  6  pose bone rotation -> undo restores the rest pose on both
  7  the guest's own undo only reverts the guest's last change (host's work untouched)
  8  host creates two boxes ~0.1 s apart -> one undo removes only the second one, on both
"""

import os
import subprocess
import sys
import tempfile
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


def _idle(s, secs=1.0):
    """Tick long enough that the open undo step closes (history.STEP_IDLE)."""
    t0 = time.time()
    while time.time() - t0 < secs:
        s.tick()
        time.sleep(0.02)


def _near(a, b, eps=1e-3):
    return all(abs(x - y) < eps for x, y in zip(a, b))


def _loc(name):
    import bpy

    ob = bpy.data.objects.get(name)
    return tuple(ob.location) if ob else None


def _base(mat="Material"):
    import bpy

    m = bpy.data.materials.get(mat)
    return tuple(m.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value) if m else None


def _rig_rot():
    import bpy

    ob = bpy.data.objects.get("Rig")
    return tuple(ob.pose.bones["b"].rotation_quaternion) if ob else None


def _flag(name, s, timeout=30):
    _wait(lambda: _read_state().get(name), timeout, s, name)


def _setup_scene():
    import bpy

    bpy.data.objects["Cube"].location = (1.0, 2.0, 3.0)
    arm = bpy.data.armatures.new("Rig")
    ob = bpy.data.objects.new("Rig", arm)
    bpy.context.scene.collection.objects.link(ob)
    bpy.context.view_layer.objects.active = ob
    bpy.ops.object.mode_set(mode="EDIT")
    eb = arm.edit_bones.new("b")
    eb.head, eb.tail = (0, 0, 0), (0, 0, 1)
    bpy.ops.object.mode_set(mode="OBJECT")
    ob.location = (-5.0, 0.0, 0.0)
    mat = bpy.data.materials.get("Material")
    mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (1.0, 0.0, 0.0, 1.0)


def _load_history():
    """history.py without the freebird_collab package (its __init__ needs bpy)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("collab_history", os.path.join(ROOT, "freebird_collab", "history.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_unit():
    hist = _load_history()
    M0, M1, M2 = [0.0], [1.0], [2.0]  # stand-ins for world matrices
    INFO = {"data": "d"}  # stand-in for _object_info()

    def steps(records):
        """records: (kind, name, before, after, now). Returns the closed steps as lists of keys."""
        h = hist.LocalHistory()
        for r in records:
            h.record(*r)
        h.tick(1e9)
        return [list(st.items) for st in h.undo_stack]

    def check(name, got, want):
        assert got == want, f"{name}: got {got}, want {want}"
        print(f"UNIT OK: {name}")

    # creates
    check("two creates 0.1 s apart -> two steps",
          steps([("o", "A", None, INFO, 0.0), ("o", "B", None, INFO, 0.1)]),
          [[("o", "A")], [("o", "B")]])
    check("one duplicate of several objects (same detection pass) -> one step",
          steps([("o", "A", None, INFO, 0.0), ("o", "B", None, INFO, 0.0), ("o", "C", None, INFO, 0.0)]),
          [[("o", "A"), ("o", "B"), ("o", "C")]])
    check("drag right after duplicate -> same step as the add",
          steps([("o", "A", None, INFO, 0.0), ("x", "A", M0, M1, 0.3), ("x", "A", M1, M2, 0.6)]),
          [[("o", "A"), ("x", "A")]])
    check("create right after a move -> new step",
          steps([("x", "C", M0, M1, 0.0), ("o", "A", None, INFO, 0.05)]),
          [[("x", "C")], [("o", "A")]])
    check("create after idle -> new step, move after idle -> new step",
          steps([("o", "A", None, INFO, 0.0), ("x", "A", M0, M1, 1.0)]),
          [[("o", "A")], [("x", "A")]])
    # unchanged: moves and deletes keep the time-based grouping
    check("moves of two objects within 0.2 s -> one step",
          steps([("x", "C", M0, M1, 0.0), ("x", "D", M0, M1, 0.1)]),
          [[("x", "C"), ("x", "D")]])
    check("move of another object 0.2 s+ later -> new step",
          steps([("x", "C", M0, M1, 0.0), ("x", "D", M0, M1, 0.3)]),
          [[("x", "C")], [("x", "D")]])
    check("deletes within 0.2 s -> one step",
          steps([("o", "A", INFO, None, 0.0), ("o", "B", INFO, None, 0.1)]),
          [[("o", "A"), ("o", "B")]])
    check("deletes 0.2 s+ apart -> two steps",
          steps([("o", "A", INFO, None, 0.0), ("o", "B", INFO, None, 0.3)]),
          [[("o", "A")], [("o", "B")]])
    print("UNIT PASS")


def run_host(mode):
    import bpy

    sys.path.insert(0, ROOT)
    from freebird_collab.session import CollabSession

    s = CollabSession()
    _setup_scene()
    if mode == "direct":
        assert s.create_room("DIRECT", "Host", (1, 0.5, 0.2), direct_port=PORT)
    else:
        assert s.create_room("RELAY", "Host", (1, 0.5, 0.2), relay_url=RELAY_URL)
    _wait(lambda: s.uid is not None, 5, s, "welcome")
    _write_state(room=s.room)
    _wait(lambda: len(s.peers) == 1, 20, s, "guest join")
    _flag("guest_ready", s)

    # 1: host moves Cube, guest then moves its Sphere, host undoes
    bpy.data.objects["Cube"].location.x = 3.0
    _idle(s)
    _write_state(host_moved=True)
    _flag("guest_moved_sphere", s)
    _wait(lambda: _near(_loc("GSphere"), (0, 0, 8)), 10, s, "guest sphere move")
    assert s.undo_local(), s.history.last_msg
    assert _near(_loc("Cube"), (1, 2, 3)), _loc("Cube")
    _idle(s, 0.5)
    assert _near(_loc("GSphere"), (0, 0, 8)), f"host undo touched the guest's sphere: {_loc('GSphere')}"
    print("HOST 1 OK: undo moved Cube back, guest Sphere untouched")
    _write_state(h1=True)
    _flag("g1", s)

    # 2: redo
    assert s.redo_local(), s.history.last_msg
    assert _near(_loc("Cube"), (3, 2, 3))
    _write_state(h2=True)
    _flag("g2", s)
    print("HOST 2 OK: redo")

    # 3: conflict: host moves Cube, guest moves it after -> host undo must keep the guest's position
    bpy.data.objects["Cube"].location.x = 6.0
    _idle(s)
    _write_state(h3_moved=True)
    _wait(lambda: _near(_loc("Cube"), (6, 5, 3)), 10, s, "guest's cube move")
    s.undo_local()
    _idle(s, 0.3)
    assert _near(_loc("Cube"), (6, 5, 3)), f"host undo overwrote the guest's later move: {_loc('Cube')}"
    assert "someone else" in s.history.last_msg, s.history.last_msg
    _write_state(h3=True)
    _flag("g3", s)
    print("HOST 3 OK: conflict kept the guest's edit:", s.history.last_msg)

    # 4: add / undo / redo / delete / undo
    bpy.ops.mesh.primitive_cube_add(size=0.5, location=(0, 4, 0))
    bpy.context.active_object.name = "HBox"
    _idle(s)
    _write_state(h4_added=True)
    _flag("g4_saw_box", s)
    assert s.undo_local(), s.history.last_msg
    assert "HBox" not in bpy.data.objects
    _write_state(h4_undo=True)
    _flag("g4_box_gone", s)
    assert s.redo_local(), s.history.last_msg
    assert "HBox" in bpy.data.objects and _near(_loc("HBox"), (0, 4, 0))
    _write_state(h4_redo=True)
    _flag("g4_box_back", s)
    _idle(s)
    bpy.data.objects.remove(bpy.data.objects["HBox"])
    _idle(s)
    _write_state(h4_deleted=True)
    _flag("g4_box_deleted", s)
    assert s.undo_local(), s.history.last_msg
    assert "HBox" in bpy.data.objects and len(bpy.data.objects["HBox"].data.vertices) == 8
    _write_state(h4_restored=True)
    _flag("g4", s)
    print("HOST 4 OK: add / delete undo + redo")

    # 5: material
    _idle(s)
    bpy.data.materials["Material"].node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.0, 1.0, 0.0, 1.0)
    _idle(s)
    _write_state(h5_green=True)
    _flag("g5_green", s)
    assert s.undo_local(), s.history.last_msg
    assert _near(_base(), (1, 0, 0, 1)), _base()
    _write_state(h5=True)
    _flag("g5", s)
    print("HOST 5 OK: material undo")

    # 6: pose
    rig = bpy.data.objects["Rig"]
    rig.pose.bones["b"].rotation_quaternion = (0.7071, 0.7071, 0.0, 0.0)
    _idle(s)
    _write_state(h6_posed=True)
    _flag("g6_posed", s)
    assert s.undo_local(), s.history.last_msg
    assert _near(_rig_rot(), (1, 0, 0, 0)), _rig_rot()
    _write_state(h6=True)
    _flag("g6", s)
    print("HOST 6 OK: pose undo")

    # 7: guest undoes its own last two changes (Cube y, Sphere move): host sees both, its own work stays
    _wait(lambda: _near(_loc("GSphere"), (0, 0, 4)), 15, s, "guest undo of its sphere move")
    _idle(s, 0.5)
    assert _near(_loc("Cube"), (6, 2, 3)) and "HBox" in bpy.data.objects and _near(_base(), (1, 0, 0, 1))
    print("HOST 7 OK: guest undo did not touch host's work")
    _flag("g7", s)

    # 8: two creates ~0.1 s apart are two undo steps: one undo removes only the second box, on both PCs
    _idle(s)
    bpy.ops.mesh.primitive_cube_add(size=0.5, location=(3, -4, 0))
    bpy.context.active_object.name = "HC1"
    _idle(s, 0.1)
    bpy.ops.mesh.primitive_cube_add(size=0.5, location=(4, -4, 0))
    bpy.context.active_object.name = "HC2"
    _idle(s)
    _write_state(h8_added=True)
    _flag("g8_saw_both", s)
    assert s.undo_local(), s.history.last_msg
    assert "HC2" not in bpy.data.objects, "undo did not remove the second box"
    assert "HC1" in bpy.data.objects, "one undo also removed the first box (both creates were one step)"
    _write_state(h8=True)
    _flag("g8", s)
    _idle(s, 0.5)
    assert "HC1" in bpy.data.objects and "HC2" not in bpy.data.objects
    print("HOST 8 OK: quick creates are separate undo steps")
    _write_state(host_done=True)
    _idle(s, 0.5)
    s.leave()
    print("HOST PASS")


def run_guest(mode):
    import bpy

    sys.path.insert(0, ROOT)
    from freebird_collab.session import CollabSession

    s = CollabSession()
    _wait(lambda: _read_state().get("room"), 20, s, "room code")
    code = _read_state()["room"]
    if mode == "direct":
        assert s.join_room("DIRECT", "Guest", (0.2, 0.6, 1), direct_host="127.0.0.1", direct_port=PORT)
    else:
        assert s.join_room("RELAY", "Guest", (0.2, 0.6, 1), code=code, relay_url=RELAY_URL)
    _wait(lambda: s._scene_loaded_from_host, 20, s, "scene from host")
    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.5, location=(0, 0, 4))
    bpy.context.active_object.name = "GSphere"
    _idle(s)
    _write_state(guest_ready=True)

    _flag("host_moved", s)
    _wait(lambda: _near(_loc("Cube"), (3, 2, 3)), 10, s, "host cube move")
    bpy.data.objects["GSphere"].location.z = 8.0
    _idle(s)
    _write_state(guest_moved_sphere=True)
    _flag("h1", s)
    _wait(lambda: _near(_loc("Cube"), (1, 2, 3)), 10, s, "host undo of the cube")
    _idle(s, 0.5)
    assert _near(_loc("GSphere"), (0, 0, 8)), f"guest sphere rewound by the host's undo: {_loc('GSphere')}"
    print("GUEST 1 OK")
    _write_state(g1=True)

    _flag("h2", s)
    _wait(lambda: _near(_loc("Cube"), (3, 2, 3)), 10, s, "host redo")
    _write_state(g2=True)

    _flag("h3_moved", s)
    _wait(lambda: _near(_loc("Cube"), (6, 2, 3)), 10, s, "host move to x=6")
    _idle(s, 0.3)
    bpy.data.objects["Cube"].location.y = 5.0
    _flag("h3", s)
    _idle(s, 0.5)
    assert _near(_loc("Cube"), (6, 5, 3)), _loc("Cube")
    _write_state(g3=True)

    _flag("h4_added", s)
    _wait(lambda: "HBox" in bpy.data.objects, 10, s, "HBox")
    _write_state(g4_saw_box=True)
    _flag("h4_undo", s)
    _wait(lambda: "HBox" not in bpy.data.objects, 10, s, "HBox removed by host undo")
    _write_state(g4_box_gone=True)
    _flag("h4_redo", s)
    _wait(lambda: "HBox" in bpy.data.objects and _near(_loc("HBox"), (0, 4, 0)), 10, s, "HBox back (redo)")
    _write_state(g4_box_back=True)
    _flag("h4_deleted", s)
    _wait(lambda: "HBox" not in bpy.data.objects, 10, s, "HBox deleted by host")
    _write_state(g4_box_deleted=True)
    _flag("h4_restored", s)
    _wait(lambda: "HBox" in bpy.data.objects and len(bpy.data.objects["HBox"].data.vertices) == 8, 10, s, "HBox restored")
    _write_state(g4=True)
    print("GUEST 4 OK")

    _flag("h5_green", s)
    _wait(lambda: _near(_base(), (0, 1, 0, 1)), 10, s, "green")
    _write_state(g5_green=True)
    _flag("h5", s)
    _wait(lambda: _near(_base(), (1, 0, 0, 1)), 10, s, "red again after host undo")
    _write_state(g5=True)
    print("GUEST 5 OK")

    _flag("h6_posed", s)
    _wait(lambda: _near(_rig_rot(), (0.7071, 0.7071, 0, 0)), 10, s, "pose")
    _write_state(g6_posed=True)
    _flag("h6", s)
    _wait(lambda: _near(_rig_rot(), (1, 0, 0, 0)), 10, s, "rest pose after host undo")
    _write_state(g6=True)
    print("GUEST 6 OK")

    # 7: the guest's own history: last step = Cube y move (conflict-free for the guest), then the sphere move
    assert s.undo_local(), s.history.last_msg  # Cube y 5 -> 2? only if nobody touched it since: host did not
    print("GUEST undo 1:", s.history.last_msg)
    _idle(s, 0.3)
    assert s.undo_local(), s.history.last_msg  # GSphere z 8 -> 4
    print("GUEST undo 2:", s.history.last_msg)
    assert _near(_loc("GSphere"), (0, 0, 4)), _loc("GSphere")
    assert "HBox" in bpy.data.objects and _near(_base(), (1, 0, 0, 1)), "guest undo touched the host's work"
    _write_state(g7=True)

    _flag("h8_added", s)
    _wait(lambda: "HC1" in bpy.data.objects and "HC2" in bpy.data.objects, 10, s, "both host boxes")
    _write_state(g8_saw_both=True)
    _flag("h8", s)
    _wait(lambda: "HC2" not in bpy.data.objects, 10, s, "HC2 removed by host undo")
    _idle(s, 0.5)
    assert "HC1" in bpy.data.objects, "host's one undo also removed HC1 here"
    _write_state(g8=True)
    print("GUEST 8 OK")
    _flag("host_done", s)
    s.leave()
    print("GUEST PASS")


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
    if mode == "unit":
        run_unit()
        return
    if role in ("host", "guest"):
        try:
            (run_host if role == "host" else run_guest)(mode)
            code = 0
        except BaseException:
            import traceback

            traceback.print_exc()
            code = 1
        sys.stdout.flush()
        os._exit(code)  # the bpy wheel can hang in its exit handlers

    run_unit()
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
    print(f"\n=== UNDO {mode.upper()}: host rc={rc_h} guest rc={rc_g} ===")
    sys.exit(0 if rc_h == 0 and rc_g == 0 else 1)


if __name__ == "__main__":
    main()
