# SPDX-License-Identifier: GPL-2.0-or-later
"""
Two-instance Pose Mode bone transform sync test (no VR needed).

    python3 tests/test_pose.py direct | relay | ws | wss    (runs the unit part first)
    python3 tests/test_pose.py unit
    blender --background --factory-startup --python tests/blender_runner.py -- tests/test_pose.py direct

Both sides share one rig (the guest gets it with the host's scene snapshot). Covers, in both directions:
bone Location / Rotation (quaternion + euler) / Scale, several bones edited one after another, a bone added on one
side during the session (structure + pose arrive, the rest keeps syncing), guest reconnect (pose matches after rejoin),
object transform / mesh / material sync still working next to it, and that an idle session sends no pose at all.
"""

import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_data import _idle, _wait  # noqa: E402
from test_sync import MODE_URLS, PORT, RELAY_URL, ROOT, STATE, _read_state, _write_state  # noqa: E402
from test_sync import _wait as _wait_plain  # noqa: E402

BONES = ("root", "upper_arm", "lower_arm", "hand")


def _build_rig(name="Rig"):
    """Armature with a 4-bone chain, built through Edit Bones (what a user or an import would produce)."""
    import bpy

    arm = bpy.data.armatures.new(name)
    ob = bpy.data.objects.new(name, arm)
    bpy.context.scene.collection.objects.link(ob)
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    prev = None
    for i, bname in enumerate(BONES):
        eb = arm.edit_bones.new(bname)
        eb.head = (0.0, 0.0, float(i))
        eb.tail = (0.0, 0.0, float(i) + 1.0)
        if prev is not None:
            eb.parent = prev
            eb.use_connect = True
        prev = eb
    bpy.ops.object.mode_set(mode="OBJECT")
    return ob


def _pb(bone, rig="Rig"):
    import bpy

    ob = bpy.data.objects.get(rig)
    return ob.pose.bones.get(bone) if ob and ob.type == "ARMATURE" else None


def _r(v):
    return tuple(round(float(x), 3) for x in v)


def _loc(bone):
    pb = _pb(bone)
    return _r(pb.location) if pb else None


def _quat(bone):
    pb = _pb(bone)
    return _r(pb.rotation_quaternion) if pb else None


def _euler(bone):
    pb = _pb(bone)
    return _r(pb.rotation_euler) if pb else None


def _scale(bone):
    pb = _pb(bone)
    return _r(pb.scale) if pb else None


def _snapshot():
    """Whole pose of Rig, rounded, for equality checks across processes."""
    import bpy

    ob = bpy.data.objects.get("Rig")
    out = {}
    for pb in ob.pose.bones:
        out[pb.name] = [pb.rotation_mode, list(_r(pb.location)), list(_r(pb.rotation_quaternion)),
                        list(_r(pb.rotation_euler)), list(_r(pb.scale))]
    return out


def _counts(s):
    return {k: v[0] for k, v in s.stats["tx_by_type"].items() if k == "pose"}


def _step(s, key, what=None):
    _wait(lambda: _read_state().get(key), 40, s, what or key)


def _pose_mode(rig="Rig"):
    import bpy

    ob = bpy.data.objects[rig]
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    bpy.ops.object.mode_set(mode="POSE")


def _object_mode():
    import bpy

    bpy.ops.object.mode_set(mode="OBJECT")


# ----------------------------------------------------------------------
def run_host(mode):
    import bpy

    sys.path.insert(0, ROOT)
    from freebird_collab.session import CollabSession
    from mathutils import Vector

    s = CollabSession()
    _build_rig()
    # the host's rig already has a pose before anyone joins: the guest must start from it (snapshot)
    _pb("root").location = (0.0, 0.5, 0.0)
    blend = os.path.join(tempfile.gettempdir(), "collab_pose_master.blend")
    bpy.ops.wm.save_as_mainfile(filepath=blend)
    if mode == "direct":
        assert s.create_room("DIRECT", "Host", (1, 0.5, 0.2), direct_port=PORT)
    else:
        assert s.create_room("RELAY", "Host", (1, 0.5, 0.2), relay_url=RELAY_URL)
    _wait(lambda: s.uid is not None, 5, s, "welcome")
    _write_state(room=s.room)
    _wait(lambda: len(s.peers) == 1, 30, s, "guest join")
    _step(s, "g_loaded", "guest loaded scene")

    # 1. Rotation host -> guest, edited IN Pose Mode (the interactive path)
    _pose_mode()
    _pb("upper_arm").rotation_mode = "QUATERNION"
    _pb("upper_arm").rotation_quaternion = (0.7071, 0.7071, 0.0, 0.0)
    _step(s, "g1", "guest saw rotation")

    # 2. Location guest -> host
    _wait(lambda: _loc("hand") == (0.0, 0.0, 1.5), 20, s, "hand location from guest")
    _write_state(h2=True)

    # 3. Scale host -> guest (still in Pose Mode)
    _pb("lower_arm").scale = (1.0, 2.0, 1.0)
    _step(s, "g3", "guest saw scale")

    # 4. Euler rotation guest -> host (rotation mode travels with the value)
    _wait(lambda: _pb("root").rotation_mode == "XYZ" and _euler("root") == (0.0, 0.0, 0.5), 20, s, "root euler from guest")
    _write_state(h4=True)
    _object_mode()

    # 5. several bones one after another, host -> guest (Object Mode edits, caught by the depsgraph handler / sweep)
    for i, bname in enumerate(BONES):
        _pb(bname).location = (0.1 * (i + 1), 0.0, 0.0)
        _idle(s, 0.15)
    _step(s, "g5", "guest saw all bones")

    # 6. a bone the guest adds: since v0.9 the structure arrives too (and then its pose)
    _step(s, "g6", "guest posed its new bone")
    _wait(lambda: _pb("GuestOnly") is not None and _loc("GuestOnly") == (0.0, 0.0, 9.0), 20, s, "guest bone + its pose")
    _pb("hand").scale = (0.5, 0.5, 0.5)
    _step(s, "g6b", "guest saw hand scale after new bone")

    # 7. other sync must keep working next to pose sync
    bpy.data.objects["Cube"].location = Vector((3.0, 0.0, 0.0))
    _step(s, "g7", "guest saw cube move")
    _wait(lambda: "GuestBall" in bpy.data.objects, 20, s, "guest object")
    _wait(lambda: bpy.data.materials.get("BallMat") is not None, 20, s, "guest material")

    # 8. reconnect: guest leaves, host poses, guest rejoins and must match exactly
    _step(s, "g_left", "guest left")
    _idle(s, 0.5)
    _pb("upper_arm").rotation_quaternion = (0.9239, 0.0, 0.3827, 0.0)
    _pb("hand").location = (0.0, 0.0, 0.25)
    _idle(s, 0.5)
    _write_state(host_pose=_snapshot(), h8=True)
    _wait(lambda: len(s.peers) == 1, 30, s, "guest rejoin")
    _step(s, "g8", "guest matches after rejoin")
    _pb("root").scale = (1.5, 1.5, 1.5)
    _step(s, "g8b", "guest saw edit after rejoin")
    _wait(lambda: _loc("lower_arm") == (0.0, 0.7, 0.0), 20, s, "guest edit after rejoin")

    # 9. idle: no pose traffic at all (no echo / ping-pong)
    _idle(s, 1.0)
    before = _counts(s)
    _idle(s, 5.0)
    assert _counts(s) == before, f"idle sent pose traffic: {before} -> {_counts(s)}"
    _write_state(host_final=_snapshot(), host_done=True)
    print("HOST pose tx:", before)
    _idle(s, 0.5)
    s.leave()
    print("HOST POSE PASS")


def _join(s, mode):
    if mode == "direct":
        assert s.join_room("DIRECT", "Guest", (0.2, 0.6, 1), direct_host="127.0.0.1", direct_port=PORT)
    else:
        assert s.join_room("RELAY", "Guest", (0.2, 0.6, 1), code=_read_state()["room"], relay_url=RELAY_URL)
    _wait(lambda: s.uid is not None, 5, s, "welcome")
    _wait(lambda: s._scene_loaded_from_host, 30, s, "scene from host")


def run_guest(mode):
    import bpy

    sys.path.insert(0, ROOT)
    from freebird_collab.session import CollabSession
    from mathutils import Vector

    s = CollabSession()
    _wait_plain(lambda: _read_state().get("room"), 30, None, "room code")
    _join(s, mode)
    rig = bpy.data.objects.get("Rig")
    assert rig is not None and rig.type == "ARMATURE", "rig missing after scene load"
    assert _loc("root") == (0.0, 0.5, 0.0), f"snapshot pose: {_loc('root')}"
    _write_state(g_loaded=True)

    # 1
    _wait(lambda: _quat("upper_arm") == (0.707, 0.707, 0.0, 0.0), 20, s, "rotation from host")
    _write_state(g1=True)
    # 2
    _pb("hand").location = (0.0, 0.0, 1.5)
    _step(s, "h2")
    # 3
    _wait(lambda: _scale("lower_arm") == (1.0, 2.0, 1.0), 20, s, "scale from host")
    _write_state(g3=True)
    # 4
    _pose_mode()
    _pb("root").rotation_mode = "XYZ"
    _pb("root").rotation_euler = (0.0, 0.0, 0.5)
    _step(s, "h4")
    _object_mode()
    # 5
    _wait(lambda: all(_loc(b) == (round(0.1 * (i + 1), 3), 0.0, 0.0) for i, b in enumerate(BONES)), 20, s, "all bones")
    _write_state(g5=True)
    # 6. add a bone here and pose it: the host gets the bone (v0.9 armature sync) and then the pose
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    eb = rig.data.edit_bones.new("GuestOnly")
    eb.head, eb.tail = (1.0, 0.0, 0.0), (1.0, 0.0, 1.0)
    bpy.ops.object.mode_set(mode="OBJECT")
    _idle(s, 0.5)
    _pb("GuestOnly").location = (0.0, 0.0, 9.0)
    _idle(s, 0.5)
    _write_state(g6=True)
    _wait(lambda: _scale("hand") == (0.5, 0.5, 0.5), 20, s, "hand scale from host")
    _write_state(g6b=True)
    # 7
    _wait(lambda: abs(bpy.data.objects["Cube"].location.x - 3.0) < 1e-3, 20, s, "cube move from host")
    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.5, location=(0, 0, 4))
    ball = bpy.context.active_object
    ball.name = "GuestBall"
    mat = bpy.data.materials.new("BallMat")
    ball.data.materials.append(mat)
    _idle(s, 1.0)
    _write_state(g7=True)
    # 8. leave, let the host pose, rejoin from a different scene
    _idle(s, 0.5)
    s.leave()
    _write_state(g_left=True)
    bpy.ops.wm.read_homefile(use_empty=True)
    _step(s, "h8")
    s = CollabSession()
    _join(s, mode)
    want = _read_state()["host_pose"]
    got = _snapshot()
    assert got == want, f"after rejoin: {got} != {want}"
    _write_state(g8=True)
    _wait(lambda: _scale("root") == (1.5, 1.5, 1.5), 20, s, "host edit after rejoin")
    _write_state(g8b=True)
    _pb("lower_arm").location = (0.0, 0.7, 0.0)
    # 9
    _idle(s, 1.0)
    before = _counts(s)
    _idle(s, 4.0)
    assert _counts(s) == before, f"idle sent pose traffic: {before} -> {_counts(s)}"
    _step(s, "host_done")
    assert _snapshot() == _read_state()["host_final"], "final pose differs from host"
    print("GUEST pose tx:", before)
    s.leave()
    print("GUEST POSE PASS")


def run_unit():
    """Single process: serialize -> delta -> apply onto a second copy of the rig reproduces the pose,
    unknown bones are reported, non-armatures are refused."""
    import bpy

    sys.path.insert(0, ROOT)
    from freebird_collab import object_data as od

    a = _build_rig("A")
    b = _build_rig("B")
    base = od.serialize_pose(a)
    assert set(base) == set(BONES) and od.pose_delta(base, od.serialize_pose(a)) == {}
    a.pose.bones["upper_arm"].rotation_quaternion = (0.7071, 0.7071, 0.0, 0.0)
    a.pose.bones["hand"].location = (1.0, 2.0, 3.0)
    a.pose.bones["hand"].scale = (2.0, 2.0, 2.0)
    a.pose.bones["root"].rotation_mode = "XYZ"
    a.pose.bones["root"].rotation_euler = (0.1, 0.2, 0.3)
    cur = od.serialize_pose(a)
    delta = od.pose_delta(base, cur)
    assert set(delta) == {"upper_arm", "hand", "root"}, delta
    delta["Nope"] = {"rm": "QUATERNION", "l": [0, 0, 0], "r": [1, 0, 0, 0], "s": [1, 1, 1]}
    applied, skipped = od.apply_pose(b, delta)
    assert sorted(applied) == ["hand", "root", "upper_arm"] and skipped == ["Nope"], (applied, skipped)
    assert od.serialize_pose(b) == cur, (od.serialize_pose(b), cur)
    assert od.pose_delta(od.serialize_pose(b), cur) == {}  # re-applying the same state changes nothing
    assert od.serialize_pose(bpy.data.objects["Cube"]) is None
    assert od.apply_pose(bpy.data.objects["Cube"], delta) == ([], sorted(delta, key=list(delta).index))
    print("UNIT POSE PASS")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "direct"
    role = sys.argv[2] if len(sys.argv) > 2 else None
    if role == "host":
        return run_host(mode)
    if role == "guest":
        return run_guest(mode)
    if mode == "unit":
        return run_unit()

    import glob

    unit = subprocess.run([sys.executable, __file__, "unit"])
    if unit.returncode != 0:
        sys.exit(unit.returncode)
    for f in glob.glob(STATE + "*"):
        os.remove(f)
    procs = []
    env = dict(os.environ)
    if mode in MODE_URLS:
        env["COLLAB_RELAY_URL"] = os.environ.get("COLLAB_RELAY_URL") or MODE_URLS[mode]
        if not os.environ.get("COLLAB_RELAY_URL"):
            procs.append(subprocess.Popen([sys.executable, os.path.join(ROOT, "relay", "collab_relay.py"), str(PORT)]))
            if mode == "wss":
                procs.append(subprocess.Popen([sys.executable, os.path.join(ROOT, "tests", "tls_proxy.py"), str(PORT + 1), str(PORT)]))
                env["SSL_CERT_FILE"] = os.path.join(tempfile.gettempdir(), "collab_test_cert.pem")
            time.sleep(1.0)
    run_mode = "direct" if mode == "direct" else "relay"
    host = subprocess.Popen([sys.executable, __file__, run_mode, "host"], env=env)
    time.sleep(1.0)
    guest = subprocess.Popen([sys.executable, __file__, run_mode, "guest"], env=env)
    rc_h, rc_g = host.wait(240), guest.wait(240)
    for p in procs:
        p.terminate()
    print(f"\n=== POSE {mode.upper()} MODE: host rc={rc_h} guest rc={rc_g} ===")
    sys.exit(0 if rc_h == 0 and rc_g == 0 else 1)


if __name__ == "__main__":
    main()
