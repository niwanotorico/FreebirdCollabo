# SPDX-License-Identifier: GPL-2.0-or-later
"""
Two-instance Vertex Group / Skinning sync test (no VR needed). Issue #6.

    python3 tests/test_skinning.py direct | relay | ws | wss    (runs the unit part first)
    python3 tests/test_skinning.py unit
    blender --background --factory-startup --python tests/blender_runner.py -- tests/test_skinning.py direct

Covers: a mesh skinned with Automatic Weights during the session arrives with the same vertex groups, weights and
Armature modifier (object reference included); weight changes (Weight Paint Mode), vertex group add / rename /
delete, Armature modifier settings, the peer's mesh DEFORMING when a pose bone moves (evaluated vertices match),
a skinned mesh built by the guest whose armature arrives after the mesh (modifier resolved later), local Weight
Paint Mode holding a received payload (and local edits winning), guest reconnect, and an idle session sending nothing.
"""

import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_armature import _bones, _edit, _new_armature, _object_mode, _pb, _pose_mode, _r, _step  # noqa: E402
from test_data import _idle, _wait  # noqa: E402
from test_sync import MODE_URLS, PORT, RELAY_URL, ROOT, STATE, _read_state, _write_state  # noqa: E402
from test_sync import _wait as _wait_plain  # noqa: E402

RIG = [("root", (0, 0, 0), (0, 0, 1), 0.0, None, False),
       ("tip", (0, 0, 1), (0, 0, 2), 0.0, "root", True)]


def _skin(name):
    """JSON-safe skin state of a mesh object: {group name: {vertex index: weight}} + Armature modifiers."""
    import bpy

    from freebird_collab import object_data as od

    ob = bpy.data.objects.get(name)
    if ob is None or ob.type != "MESH":
        return None
    meta = od.skin_meta(ob)
    weights = od._weights(ob, ob.data)
    groups = {}
    for gname, flat in zip(meta["vg"], weights):
        groups[gname] = {str(flat[i]): round(flat[i + 1], 3) for i in range(0, len(flat), 2)}
    mods = [{k: v for k, v in m.items() if k != "i"} for m in meta["mods"]]
    return {"groups": groups, "mods": mods}


def _groups(name):
    import bpy

    ob = bpy.data.objects.get(name)
    return [g.name for g in ob.vertex_groups] if ob is not None else None


def _mod(name):
    import bpy

    ob = bpy.data.objects.get(name)
    if ob is None:
        return None
    return next((m for m in ob.modifiers if m.type == "ARMATURE"), None)


def _eval_top(name):
    """Evaluated (deformed) position of the highest rest vertex of a mesh: the thing skinning is for."""
    import bpy

    ob = bpy.data.objects.get(name)
    if ob is None or ob.type != "MESH" or not len(ob.data.vertices):
        return None
    top = max(range(len(ob.data.vertices)), key=lambda i: (round(ob.data.vertices[i].co.z, 4), -i))
    dg = bpy.context.evaluated_depsgraph_get()
    ev = ob.evaluated_get(dg)
    return list(_r(ev.data.vertices[top].co))


def _deformed(name):
    """How far the top vertex moved from its rest position (0 = the mesh does not follow the armature)."""
    import bpy

    ob = bpy.data.objects[name]
    top = max(range(len(ob.data.vertices)), key=lambda i: (round(ob.data.vertices[i].co.z, 4), -i))
    return (ob.data.vertices[top].co - __import__("mathutils").Vector(_eval_top(name))).length


def _new_skinned(mesh_name, rig_name, bones=RIG):
    """A cylinder parented to a new armature with Automatic Weights (Ctrl+P > With Automatic Weights)."""
    import bpy

    rig = _new_armature(rig_name, bones)
    bpy.ops.mesh.primitive_cylinder_add(vertices=8, depth=2.0, location=(0, 0, 1))
    body = bpy.context.active_object
    body.name = mesh_name
    bpy.ops.object.select_all(action="DESELECT")
    body.select_set(True)
    rig.select_set(True)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.parent_set(type="ARMATURE_AUTO")
    bpy.ops.object.select_all(action="DESELECT")
    assert set(_groups(mesh_name)) == {n for n, *_ in bones}, _groups(mesh_name)
    assert _mod(mesh_name) is not None and _mod(mesh_name).object == rig
    return body, rig


def _weight_paint(name):
    """Enter Weight Paint Mode on the mesh (like a user pressing Ctrl+Tab)."""
    import bpy

    ob = bpy.data.objects[name]
    if bpy.context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    bpy.ops.object.mode_set(mode="WEIGHT_PAINT")
    assert ob.mode == "WEIGHT_PAINT"


def _paint(name, group, indices, weight):
    """Set weights the way a Weight Paint stroke does (in whatever mode the object is in)."""
    import bpy

    bpy.data.objects[name].vertex_groups[group].add(list(indices), weight, "REPLACE")


def _counts(s):
    return {k: v[0] for k, v in s.stats["tx_by_type"].items() if k in ("obj_data", "obj_add", "pose")}


# ----------------------------------------------------------------------
def run_host(mode):
    import bpy

    sys.path.insert(0, ROOT)
    from freebird_collab.session import CollabSession
    from mathutils import Vector

    s = CollabSession()
    blend = os.path.join(tempfile.gettempdir(), "collab_skinning_master.blend")
    bpy.ops.wm.save_as_mainfile(filepath=blend)
    if mode == "direct":
        assert s.create_room("DIRECT", "Host", (1, 0.5, 0.2), direct_port=PORT)
    else:
        assert s.create_room("RELAY", "Host", (1, 0.5, 0.2), relay_url=RELAY_URL)
    _wait(lambda: s.uid is not None, 5, s, "welcome")
    _write_state(room=s.room)
    _wait(lambda: len(s.peers) == 1, 30, s, "guest join")
    _step(s, "g_loaded", "guest loaded scene")

    # 1. Armature + mesh with Automatic Weights during the session -> the guest gets groups, weights and the modifier
    _new_skinned("Body", "Rig")
    _idle(s, 0.5)
    _write_state(h1_skin=_skin("Body"), h1=True)
    _step(s, "g1", "guest got the skinned mesh")

    # 2. weights change in Weight Paint Mode (live, while staying in the mode)
    _weight_paint("Body")
    top = [i for i, v in enumerate(bpy.data.objects["Body"].data.vertices) if v.co.z > 0.5]  # local coords: the cylinder is 2 high around its origin
    _paint("Body", "root", top, 0.25)
    _paint("Body", "tip", top, 0.75)
    _idle(s, 0.5)
    _write_state(h2_skin=_skin("Body"), h2=True)
    _step(s, "g2", "guest saw weight paint")
    _object_mode()

    # 3. vertex group add (no geometry change) / rename / delete
    body = bpy.data.objects["Body"]
    body.vertex_groups.new(name="extra")
    _step(s, "g3a", "guest saw group add")
    body.vertex_groups["extra"].name = "extra2"
    _step(s, "g3b", "guest saw group rename")
    body.vertex_groups.remove(body.vertex_groups["extra2"])
    _step(s, "g3c", "guest saw group delete")

    # 4. Armature modifier settings
    m = _mod("Body")
    m.use_deform_preserve_volume = True
    m.vertex_group = "root"
    m.name = "Skin"
    _idle(s, 0.5)
    _write_state(h4_skin=_skin("Body"), h4=True)
    _step(s, "g4", "guest saw modifier settings")

    # 5. THE goal: posing a bone here deforms the guest's mesh the same way
    _pose_mode("Rig")
    _pb("Rig", "tip").rotation_quaternion = (0.7071, 0.7071, 0.0, 0.0)  # 90 deg about X
    _idle(s, 0.5)
    top_here = _eval_top("Body")
    assert top_here is not None and _deformed("Body") > 0.3, f"pose must deform the mesh here first: {top_here}"
    _write_state(h5_top=top_here, h5=True)
    _step(s, "g5", "guest mesh deformed like ours")
    _object_mode()

    # 6. the guest skins its own mesh: mesh obj_add arrives BEFORE its armature (name order) -> modifier resolved later
    _step(s, "g6", "guest built a skinned mesh")
    _wait(lambda: _skin("GuestBody") == _read_state()["g6_skin"] and "GuestBody" not in s._pending_mods, 20, s,
          "guest skinned mesh here (modifier resolved)")
    assert _mod("GuestBody").object == bpy.data.objects["GuestRig"]
    _wait(lambda: _eval_top("GuestBody") == _read_state()["g6_top"], 20, s, "guest pose deforms its mesh here")

    # 7. the guest is in Weight Paint Mode on Body: our weights wait, and the guest's own strokes win
    _step(s, "g7_painting", "guest entered Weight Paint Mode")
    _paint("Body", "tip", top, 0.5)
    _idle(s, 1.0)
    _write_state(h7=True)
    _step(s, "g7", "guest left Weight Paint Mode")
    _wait(lambda: _skin("Body") == _read_state()["g7_skin"], 20, s, "guest's weights (local wins) here")

    # 8. other sync still fine next to it
    bpy.data.objects["Cube"].location = Vector((3.0, 0.0, 0.0))
    _step(s, "g8", "guest saw cube move")

    # 9. reconnect: guest leaves, we paint + change the modifier, guest rejoins and must match
    _step(s, "g_left", "guest left")
    _idle(s, 0.5)
    _paint("Body", "root", top, 0.6)
    _mod("Body").use_bone_envelopes = True
    _pb("Rig", "tip").rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
    _idle(s, 0.5)
    _write_state(host_skin=_skin("Body"), host_guest_skin=_skin("GuestBody"), host_top=_eval_top("Body"), h9=True)
    _wait(lambda: len(s.peers) == 1, 30, s, "guest rejoin")
    _step(s, "g9", "guest matches after rejoin")
    _paint("Body", "tip", top, 0.9)
    _idle(s, 0.5)
    _write_state(h9b_skin=_skin("Body"), h9b=True)
    _step(s, "g9b", "guest saw paint after rejoin")

    # 10. idle: nothing goes out
    _idle(s, 1.0)
    before = _counts(s)
    _idle(s, 5.0)
    assert _counts(s) == before, f"idle sent traffic: {before} -> {_counts(s)}"
    _write_state(host_final=[_skin("Body"), _skin("GuestBody"), _bones("Rig")], host_done=True)
    print("HOST skinning tx:", before)
    _idle(s, 0.5)
    s.leave()
    print("HOST SKINNING PASS")


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
    _step(s, "h1")
    _wait(lambda: _skin("Body") == _read_state()["h1_skin"], 20, s, "skinned mesh from host")
    assert bpy.data.objects["Body"].type == "MESH" and bpy.data.objects["Rig"].type == "ARMATURE"
    assert _mod("Body").object == bpy.data.objects["Rig"] and _mod("Body").use_vertex_groups
    _write_state(g1=True)
    # 2
    _step(s, "h2")
    _wait(lambda: _skin("Body") == _read_state()["h2_skin"], 20, s, "weight paint from host")
    _write_state(g2=True)
    # 3
    _wait(lambda: "extra" in (_groups("Body") or []), 20, s, "group add from host")
    _write_state(g3a=True)
    _wait(lambda: "extra2" in (_groups("Body") or []) and "extra" not in _groups("Body"), 20, s, "group rename from host")
    _write_state(g3b=True)
    _wait(lambda: _groups("Body") == ["root", "tip"], 20, s, "group delete from host")
    _write_state(g3c=True)
    # 4
    _step(s, "h4")
    _wait(lambda: _skin("Body") == _read_state()["h4_skin"], 20, s, "modifier settings from host")
    m = _mod("Body")
    assert m.name == "Skin" and m.use_deform_preserve_volume and m.vertex_group == "root", (m.name, m.vertex_group)
    _write_state(g4=True)
    # 5. deformation
    _step(s, "h5")
    _wait(lambda: _eval_top("Body") == _read_state()["h5_top"], 20, s, "deformed like the host")
    _write_state(g5=True)
    # 6. skin a mesh here (mesh obj_add goes out before the armature's: name order)
    _new_skinned("GuestBody", "GuestRig")
    _pose_mode("GuestRig")
    _pb("GuestRig", "tip").rotation_quaternion = (0.7071, 0.0, 0.7071, 0.0)
    _idle(s, 0.5)
    _object_mode()
    _write_state(g6_skin=_skin("GuestBody"), g6_top=_eval_top("GuestBody"), g6=True)
    # 7. hold while painting here; our strokes win over what arrived meanwhile
    _weight_paint("Body")
    _write_state(g7_painting=True)
    _step(s, "h7")
    assert "Body" in s._pending_data, "received weights must wait while we are in Weight Paint Mode"
    top = [i for i, v in enumerate(bpy.data.objects["Body"].data.vertices) if v.co.z > 0.5]  # local coords: the cylinder is 2 high around its origin
    _paint("Body", "tip", top, 0.33)
    _object_mode()
    _idle(s, 1.0)
    assert "Body" not in s._pending_data
    assert _skin("Body")["groups"]["tip"][str(top[0])] == 0.33, "local Weight Paint edits must win"
    _write_state(g7_skin=_skin("Body"), g7=True)
    # 8
    _wait(lambda: abs(bpy.data.objects["Cube"].location.x - 3.0) < 1e-3, 20, s, "cube move from host")
    _write_state(g8=True)
    # 9. leave, rejoin from an empty scene: weights / modifier / pose come with the snapshot
    _idle(s, 0.5)
    s.leave()
    _write_state(g_left=True)
    bpy.ops.wm.read_homefile(use_empty=True)
    _step(s, "h9")
    s = CollabSession()
    _join(s, mode)
    st = _read_state()
    assert _skin("Body") == st["host_skin"], (_skin("Body"), st["host_skin"])
    assert _skin("GuestBody") == st["host_guest_skin"]
    assert _eval_top("Body") == st["host_top"], "deformation after rejoin"
    _write_state(g9=True)
    _step(s, "h9b")
    _wait(lambda: _skin("Body") == _read_state()["h9b_skin"], 20, s, "paint after rejoin")
    _write_state(g9b=True)
    # 10
    _idle(s, 1.0)
    before = _counts(s)
    _idle(s, 4.0)
    assert _counts(s) == before, f"idle sent traffic: {before} -> {_counts(s)}"
    _step(s, "host_done")
    assert [_skin("Body"), _skin("GuestBody"), _bones("Rig")] == _read_state()["host_final"], "final state differs from host"
    print("GUEST skinning tx:", before)
    s.leave()
    print("GUEST SKINNING PASS")


def run_unit():
    """Single process: serialize -> apply reproduces groups / weights / modifier on another object; group rename
    and delete; modifier object missing at apply time (resolved later); a payload without skin data (older peer)
    keeps the local weights; apply refused in Weight Paint Mode; digest stable across serialize / apply."""
    import bpy

    sys.path.insert(0, ROOT)
    from freebird_collab import object_data as od

    body, rig = _new_skinned("A", "RigA")
    top = [i for i, v in enumerate(body.data.vertices) if v.co.z > 0.5]  # local coords: the cylinder is 2 high around its origin
    _paint("A", "tip", top, 0.8)
    payload = od.serialize(body)
    d = payload["data"]
    assert d["vg"] == ["root", "tip"] and len(d["w"]) == 2 and d["mods"][0]["ob"] == "RigA" and d["mods"][0]["uvg"], d["mods"]
    b = bpy.data.objects.new("B", bpy.data.meshes.new("B"))
    bpy.context.scene.collection.objects.link(b)
    b.matrix_world = body.matrix_world.copy()  # what xform sync does (the modifier deforms in armature space)
    assert od.apply(b, payload)
    assert _skin("B") == _skin("A"), (_skin("B"), _skin("A"))
    a = body
    assert od.quick_digest(b) is not None and _mod("B").object == rig
    # deformation matches after a pose
    _pose_mode("RigA")
    _pb("RigA", "tip").rotation_quaternion = (0.7071, 0.7071, 0.0, 0.0)
    _object_mode()
    assert _eval_top("A") == _eval_top("B") and _deformed("A") > 0.3, (_eval_top("A"), _eval_top("B"))
    # rename + delete + modifier tweak travel
    a.vertex_groups["tip"].name = "tip2"
    a.vertex_groups.new(name="tmp")
    a.vertex_groups.remove(a.vertex_groups["root"])
    _mod("A").use_deform_preserve_volume = True
    assert od.apply(b, od.serialize(a)) and _skin("B") == _skin("A") and _groups("B") == ["tip2", "tmp"], _groups("B")
    assert _mod("B").use_deform_preserve_volume
    # meta digest: cheap gate changes on group / modifier edits, not on weights
    m0 = od.skin_meta_digest(a)
    _paint("A", "tip2", top, 0.1)
    assert od.skin_meta_digest(a) == m0
    _mod("A").vertex_group = "tmp"
    assert od.skin_meta_digest(a) != m0
    # modifier pointing at an armature that is not here: created without object, reported as missing
    d = od.serialize(a)["data"]
    d["mods"][0]["ob"] = "Later"
    assert od.missing_modifier_objects(d["mods"]) == ["Later"]
    assert od.apply_armature_modifiers(b, d["mods"]) == ["Later"] and _mod("B").object == rig  # keeps the old one
    later = _new_armature("Later", RIG)
    assert od.apply_armature_modifiers(b, d["mods"]) == [] and _mod("B").object == later
    # older peer: payload without skin keys must not wipe our weights
    old = od.serialize(b)
    for key in ("vg", "w", "mods"):
        old["data"].pop(key)
    before = _skin("B")
    assert od.apply(b, old) and _skin("B") == before, (_skin("B"), before)
    # refused while painted here
    _weight_paint("B")
    assert not od.apply(b, od.serialize(a))
    _object_mode()
    assert od.apply(b, od.serialize(a))
    print("UNIT SKINNING PASS")


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
    print(f"\n=== SKINNING {mode.upper()} MODE: host rc={rc_h} guest rc={rc_g} ===")
    sys.exit(0 if rc_h == 0 and rc_g == 0 else 1)


if __name__ == "__main__":
    main()
