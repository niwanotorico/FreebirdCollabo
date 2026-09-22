# SPDX-License-Identifier: GPL-2.0-or-later
"""
Two-instance Armature / Bone structure sync test (no VR needed). Issue #5.

    python3 tests/test_armature.py direct | relay | ws | wss    (runs the unit part first)
    python3 tests/test_armature.py unit
    blender --background --factory-startup --python tests/blender_runner.py -- tests/test_armature.py direct

Covers: an Armature created DURING the session arrives as a real armature (not an Empty placeholder), bone add /
delete / rename, head / tail / roll edits (live, while the sender stays in Edit Mode), parent + connected changes,
a multi-bone chain built by the guest (both directions), a rig the guest creates being posed by the host right after,
local Edit Mode holding a received structure until it ends, guest reconnect (structures match after rejoin), pose
sync still working on the synced rig, transform / mesh / material sync next to it, and an idle session sending nothing.
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


def _r(v, n=3):
    return tuple(round(float(x), n) for x in v)


def _bones(name):
    """{bone: (head, tail, roll, parent, connected)} of an armature object, rounded, from the rest bones."""
    import bpy

    from freebird_collab import object_data as od

    ob = bpy.data.objects.get(name)
    if ob is None or ob.type != "ARMATURE":
        return None
    return {it["n"]: [list(_r(it["h"])), list(_r(it["t"])), round(it["r"], 3), it["p"], it["c"]] for it in od.armature_bones(ob)}  # JSON-safe


def _edit(name):
    """Enter Edit Mode on the armature (like a user pressing Tab) and return its edit_bones."""
    import bpy

    ob = bpy.data.objects[name]
    if bpy.context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    return ob.data.edit_bones


def _object_mode():
    import bpy

    bpy.ops.object.mode_set(mode="OBJECT")


def _pose_mode(name):
    import bpy

    ob = bpy.data.objects[name]
    if bpy.context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    bpy.ops.object.mode_set(mode="POSE")


def _new_armature(name, bones):
    """Create an armature object with bones [(name, head, tail, roll, parent, connected)] the way a user would."""
    import bpy

    arm = bpy.data.armatures.new(name)
    ob = bpy.data.objects.new(name, arm)
    bpy.context.scene.collection.objects.link(ob)
    ebs = _edit(ob.name)
    for n, h, t, r, p, c in bones:
        eb = ebs.new(n)
        eb.head, eb.tail, eb.roll = h, t, r
        if p:
            eb.parent = ebs[p]
            eb.use_connect = c
    _object_mode()
    return ob


def _pb(rig, bone):
    import bpy

    ob = bpy.data.objects.get(rig)
    return ob.pose.bones.get(bone) if ob is not None and ob.type == "ARMATURE" and ob.pose else None


def _counts(s):
    return {k: v[0] for k, v in s.stats["tx_by_type"].items() if k in ("obj_data", "obj_add", "pose")}


def _step(s, key, what=None):
    _wait(lambda: _read_state().get(key), 40, s, what or key)


CHAIN = [("root", (0, 0, 0), (0, 0, 1), 0.0, None, False),
         ("spine", (0, 0, 1), (0, 0, 2), 0.0, "root", True),
         ("arm.L", (0, 0, 2), (1, 0, 2), 0.5, "spine", False),
         ("arm.R", (0, 0, 2), (-1, 0, 2), -0.5, "spine", False)]


# ----------------------------------------------------------------------
def run_host(mode):
    import bpy

    sys.path.insert(0, ROOT)
    from freebird_collab.session import CollabSession
    from mathutils import Vector

    s = CollabSession()
    blend = os.path.join(tempfile.gettempdir(), "collab_armature_master.blend")
    bpy.ops.wm.save_as_mainfile(filepath=blend)
    if mode == "direct":
        assert s.create_room("DIRECT", "Host", (1, 0.5, 0.2), direct_port=PORT)
    else:
        assert s.create_room("RELAY", "Host", (1, 0.5, 0.2), relay_url=RELAY_URL)
    _wait(lambda: s.uid is not None, 5, s, "welcome")
    _write_state(room=s.room)
    _wait(lambda: len(s.peers) == 1, 30, s, "guest join")
    _step(s, "g_loaded", "guest loaded scene")

    # 1. a NEW armature during the session must arrive as a real armature with its bone
    _new_armature("Rig", [("Bone", (0, 0, 0), (0, 0, 1), 0.0, None, False)])
    _step(s, "g1", "guest got the armature")

    # 2. add bones in Edit Mode and stay there: the guest must see them live (like a mesh vertex drag)
    ebs = _edit("Rig")
    for n, h, t, r, p, c in CHAIN[1:]:
        eb = ebs.new(n)
        eb.head, eb.tail, eb.roll = h, t, r
        eb.parent = ebs["Bone"] if p == "root" else ebs[p]
        eb.use_connect = c
    _step(s, "g2", "guest saw new bones while host is still in Edit Mode")
    # 3. head / tail / roll of an existing bone, still in Edit Mode
    ebs["arm.L"].tail = (1.5, 0.5, 2.0)
    ebs["arm.L"].roll = 1.25
    _step(s, "g3", "guest saw head/tail/roll")
    # 4. delete one, rename one (name based: the peer gets delete + create), then leave Edit Mode
    ebs.remove(ebs["arm.R"])
    ebs["Bone"].name = "root"
    _object_mode()
    _step(s, "g4", "guest saw delete + rename")
    assert _bones("Rig") == _read_state()["g4_bones"], (_bones("Rig"), _read_state()["g4_bones"])

    # 5. parent / connected change from the guest, and a whole chain the guest builds -> here
    _step(s, "g5", "guest changed parent/connected")
    _wait(lambda: _bones("Rig") is not None and _bones("Rig")["arm.L"][3:] == ["root", True], 20, s, "parent/connected from guest")
    assert _bones("Rig")["arm.L"][0] == _bones("Rig")["root"][1], "connected bone head must sit on the parent tail"
    _wait(lambda: _bones("GuestRig") is not None and set(_bones("GuestRig")) == {n for n, *_ in CHAIN}, 20, s, "guest rig here")
    assert bpy.data.objects["GuestRig"].type == "ARMATURE"
    assert _bones("GuestRig") == _read_state()["guest_rig"], (_bones("GuestRig"), _read_state()["guest_rig"])

    # 6. pose sync works on the rig the guest created (v0.8 needed the rig in the .blend before the session)
    _pose_mode("GuestRig")
    _pb("GuestRig", "arm.L").rotation_quaternion = (0.7071, 0.7071, 0.0, 0.0)
    _step(s, "g6", "guest saw pose on its rig")
    _wait(lambda: _pb("Rig", "spine") is not None and _r(_pb("Rig", "spine").location) == (0.0, 0.3, 0.0), 20, s, "pose from guest on synced rig")
    _object_mode()

    # 7. a bone added here and posed right away: the structure must reach the guest before the pose
    ebs = _edit("Rig")
    eb = ebs.new("tip")
    eb.head, eb.tail = (1.5, 0.5, 2.0), (2.0, 1.0, 2.5)
    eb.parent = ebs["arm.L"]
    _object_mode()
    _pb("Rig", "tip").location = (0.0, 0.0, 0.4)
    _step(s, "g7", "guest got tip with its pose")

    # 8. the guest is in Edit Mode on Rig while we change it: applied only after the guest leaves Edit Mode
    _step(s, "g8_editing", "guest entered Edit Mode")
    ebs = _edit("Rig")
    ebs["tip"].tail = (2.5, 1.0, 2.5)
    _object_mode()
    _idle(s, 1.0)
    _write_state(h8=True)
    _step(s, "g8", "guest applied after leaving Edit Mode")

    # 9. other sync keeps working next to it
    bpy.data.objects["Cube"].location = Vector((3.0, 0.0, 0.0))
    _step(s, "g9", "guest saw cube move")
    _wait(lambda: "GuestBall" in bpy.data.objects and bpy.data.materials.get("BallMat") is not None, 20, s, "guest object + material")

    # 10. reconnect: guest leaves, host restructures, guest rejoins and must match (bones AND pose)
    _step(s, "g_left", "guest left")
    _idle(s, 0.5)
    ebs = _edit("Rig")
    eb = ebs.new("tail")
    eb.head, eb.tail = (0, 0, 0), (0, -1, 0)
    eb.parent = ebs["root"]
    ebs.remove(ebs["tip"])
    _object_mode()
    _pb("Rig", "arm.L").scale = (1.0, 2.0, 1.0)
    _idle(s, 0.5)
    _write_state(host_rig=_bones("Rig"), host_guest_rig=_bones("GuestRig"), h10=True)
    _wait(lambda: len(s.peers) == 1, 30, s, "guest rejoin")
    _step(s, "g10", "guest matches after rejoin")
    ebs = _edit("Rig")
    ebs["tail"].tail = (0, -2, 0)
    _object_mode()
    _step(s, "g10b", "guest saw edit after rejoin")
    _wait(lambda: _bones("GuestRig") is not None and "extra" in _bones("GuestRig"), 20, s, "guest edit after rejoin")

    # 11. idle: no structure / pose traffic at all
    _idle(s, 1.0)
    before = _counts(s)
    _idle(s, 5.0)
    assert _counts(s) == before, f"idle sent traffic: {before} -> {_counts(s)}"
    _write_state(host_final=[_bones("Rig"), _bones("GuestRig")], host_done=True)
    print("HOST armature tx:", before)
    _idle(s, 0.5)
    s.leave()
    print("HOST ARMATURE PASS")


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

    s = CollabSession()
    _wait_plain(lambda: _read_state().get("room"), 30, None, "room code")
    _join(s, mode)
    _write_state(g_loaded=True)

    # 1
    _wait(lambda: bpy.data.objects.get("Rig") is not None and bpy.data.objects["Rig"].type == "ARMATURE"
          and _bones("Rig") == {"Bone": [[0.0, 0.0, 0.0], [0.0, 0.0, 1.0], 0.0, None, False]}, 20, s, "armature from host")
    _write_state(g1=True)
    # 2
    _wait(lambda: set(_bones("Rig")) == {"Bone", "spine", "arm.L", "arm.R"} and _bones("Rig")["spine"][3:] == ["Bone", True]
          and _bones("Rig")["arm.R"] == [[0.0, 0.0, 2.0], [-1.0, 0.0, 2.0], -0.5, "spine", False], 20, s, "bones from host Edit Mode")
    _write_state(g2=True)
    # 3
    _wait(lambda: _bones("Rig")["arm.L"] == [[0.0, 0.0, 2.0], [1.5, 0.5, 2.0], 1.25, "spine", False], 20, s, "head/tail/roll from host")
    _write_state(g3=True)
    # 4
    _wait(lambda: set(_bones("Rig")) == {"root", "spine", "arm.L"} and _bones("Rig")["spine"][3] == "root", 20, s, "delete + rename from host")
    _write_state(g4_bones=_bones("Rig"), g4=True)
    # 5. parent / connected change here, plus a whole rig built here
    ebs = _edit("Rig")
    ebs["arm.L"].parent = ebs["root"]
    ebs["arm.L"].use_connect = True
    _object_mode()
    _new_armature("GuestRig", CHAIN)
    _idle(s, 0.5)
    _write_state(guest_rig=_bones("GuestRig"), g5=True)
    # 6. pose on the rig we created, from the host
    _wait(lambda: _pb("GuestRig", "arm.L") is not None and _r(_pb("GuestRig", "arm.L").rotation_quaternion) == (0.707, 0.707, 0.0, 0.0),
          20, s, "pose from host on GuestRig")
    _write_state(g6=True)
    _pb("Rig", "spine").location = (0.0, 0.3, 0.0)
    # 7
    _wait(lambda: _pb("Rig", "tip") is not None and _r(_pb("Rig", "tip").location) == (0.0, 0.0, 0.4), 20, s, "tip + its pose")
    assert _bones("Rig")["tip"][3] == "arm.L"
    _write_state(g7=True)
    # 8. hold a received structure while we are in Edit Mode on the same rig
    ebs = _edit("Rig")
    _write_state(g8_editing=True)
    _step(s, "h8")
    assert _r(ebs["tip"].tail) == (2.0, 1.0, 2.5), "received structure must not be applied while we are in Edit Mode"
    assert "Rig" in s._pending_data
    _object_mode()
    _wait(lambda: _bones("Rig")["tip"][1] == [2.5, 1.0, 2.5], 20, s, "pending structure applied after Edit Mode")
    _write_state(g8=True)
    # 9
    _wait(lambda: abs(bpy.data.objects["Cube"].location.x - 3.0) < 1e-3, 20, s, "cube move from host")
    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.5, location=(0, 0, 4))
    ball = bpy.context.active_object
    ball.name = "GuestBall"
    ball.data.materials.append(bpy.data.materials.new("BallMat"))
    _idle(s, 1.0)
    _write_state(g9=True)
    # 10. leave, rejoin from an empty scene: structures and pose come with the snapshot
    _idle(s, 0.5)
    s.leave()
    _write_state(g_left=True)
    bpy.ops.wm.read_homefile(use_empty=True)
    _step(s, "h10")
    s = CollabSession()
    _join(s, mode)
    st = _read_state()
    assert _bones("Rig") == st["host_rig"], (_bones("Rig"), st["host_rig"])
    assert _bones("GuestRig") == st["host_guest_rig"]
    assert _r(_pb("Rig", "arm.L").scale) == (1.0, 2.0, 1.0), "pose after rejoin"
    _write_state(g10=True)
    _wait(lambda: _bones("Rig")["tail"][1] == [0.0, -2.0, 0.0], 20, s, "host edit after rejoin")
    _write_state(g10b=True)
    ebs = _edit("GuestRig")
    eb = ebs.new("extra")
    eb.head, eb.tail = (0, 1, 0), (0, 2, 0)
    _object_mode()
    # 11
    _idle(s, 1.0)
    before = _counts(s)
    _idle(s, 4.0)
    assert _counts(s) == before, f"idle sent traffic: {before} -> {_counts(s)}"
    _step(s, "host_done")
    assert [_bones("Rig"), _bones("GuestRig")] == _read_state()["host_final"], "final structure differs from host"
    print("GUEST armature tx:", before)
    s.leave()
    print("GUEST ARMATURE PASS")


def run_unit():
    """Single process: serialize -> apply onto another armature reproduces the bones (from Object Mode and from
    Edit Mode alike), incremental apply keeps the pose of surviving bones, connected chains stay consistent,
    apply is refused while another object is in Edit Mode, digests match after apply."""
    import bpy

    sys.path.insert(0, ROOT)
    from freebird_collab import object_data as od

    a = _new_armature("A", CHAIN)
    ref = od.armature_bones(a)
    assert [it["n"] for it in ref] == ["root", "spine", "arm.L", "arm.R"], ref  # parents first
    ebs = _edit("A")
    assert od.armature_bones(a) == ref, "edit_bones and Bone must serialize identically"
    ebs["arm.L"].roll = 2.0
    live = od.armature_bones(a)
    assert live != ref and live[2]["r"] == 2.0, live  # live from edit_bones while still in Edit Mode
    _object_mode()
    assert od.armature_bones(a) == live
    b = bpy.data.objects.new("B", bpy.data.armatures.new("B"))
    bpy.context.scene.collection.objects.link(b)
    assert od.apply(b, od.serialize(a)) and od.armature_bones(b) == live, od.armature_bones(b)
    assert od.quick_digest(a) == od.quick_digest(b)
    # incremental: pose of a surviving bone survives, removed bone goes, new bone comes, connected head follows parent tail
    b.pose.bones["spine"].location = (1, 2, 3)
    ebs = _edit("A")
    ebs.remove(ebs["arm.R"])
    eb = ebs.new("head")
    eb.head, eb.tail = (0, 0, 2), (0, 0, 3)
    eb.parent, eb.use_connect = ebs["spine"], True
    ebs["spine"].tail = (0, 0.5, 2.5)  # moves the connected children's heads on the sender
    _object_mode()
    cur = od.armature_bones(a)
    assert {it["n"] for it in cur} == {"root", "spine", "arm.L", "head"}
    assert od.apply(b, od.serialize(a)) and od.armature_bones(b) == cur, (od.armature_bones(b), cur)
    assert _r(b.pose.bones["spine"].location) == (1.0, 2.0, 3.0), "pose lost on incremental apply"
    assert "arm.R" not in b.pose.bones
    assert od.armature_delta_summary(live, cur) == "+head; -arm.R; ~spine", od.armature_delta_summary(live, cur)
    # refused while a mesh is in Edit Mode here, applied afterwards; Pose Mode on the target is restored
    bpy.context.view_layer.objects.active = bpy.data.objects["Cube"]
    bpy.ops.object.mode_set(mode="EDIT")
    assert not od.armature_editable() and not od.apply(b, od.serialize(a))
    _object_mode()
    _pose_mode("B")
    ebs = _edit("A")
    ebs["head"].tail = (0, 0, 4)
    _object_mode()
    _pose_mode("B")
    assert od.apply(b, od.serialize(a)) and od.armature_bones(b) == od.armature_bones(a)
    assert b.mode == "POSE" and bpy.context.mode == "POSE", (b.mode, bpy.context.mode)
    _object_mode()
    assert od.serialize(bpy.data.objects["Cube"])["type"] == "MESH" and od.armature_bones(bpy.data.objects["Cube"]) is None
    print("UNIT ARMATURE PASS")


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
    print(f"\n=== ARMATURE {mode.upper()} MODE: host rc={rc_h} guest rc={rc_g} ===")
    sys.exit(0 if rc_h == 0 and rc_g == 0 else 1)


if __name__ == "__main__":
    main()
