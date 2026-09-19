# SPDX-License-Identifier: GPL-2.0-or-later
"""
GLB import during a live session (no reconnect), both directions.

    python3 tests/test_glb.py direct | relay | ws | wss

1. HOST imports a GLB while connected  -> appears on GUEST
2. GUEST imports a GLB while connected -> appears on HOST
3. no reconnect
4. GLB with several meshes            5. parent/child hierarchy kept
6. transform edit after import        7. Edit-Mode mesh edit after import
Also: material slot colour arrives, orphan name squatting does not block re-import.
"""

import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_data import _edit_mesh_move_vertex, _idle, _wait  # noqa: E402
from test_sync import MODE_URLS, PORT, RELAY_URL, ROOT, STATE, _read_state, _write_state  # noqa: E402

GLB_A = os.path.join(tempfile.gettempdir(), "collab_test_a.glb")
GLB_B = os.path.join(tempfile.gettempdir(), "collab_test_b.glb")
GLB_C = os.path.join(tempfile.gettempdir(), "collab_test_c.glb")  # textured cube


def build_glbs():
    """Two small hierarchical GLBs: Root(empty) > BodyA(mesh, red), BodyB(mesh) > Tip(mesh)."""
    import bpy

    for prefix, path in (("A", GLB_A), ("B", GLB_B)):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.object.empty_add(location=(0, 0, 0))
        root = bpy.context.active_object
        root.name = f"{prefix}_Root"
        bpy.ops.mesh.primitive_cube_add(size=1, location=(1, 0, 0))
        a = bpy.context.active_object
        a.name = f"{prefix}_BodyA"
        a.parent = root
        bpy.ops.mesh.primitive_uv_sphere_add(radius=0.5, location=(-1, 0, 0))
        b = bpy.context.active_object
        b.name = f"{prefix}_BodyB"
        b.parent = root
        bpy.ops.mesh.primitive_cone_add(radius1=0.3, location=(-1, 0, 1))
        c = bpy.context.active_object
        c.name = f"{prefix}_Tip"
        c.parent = b
        c.matrix_parent_inverse = b.matrix_world.inverted()
        mat = bpy.data.materials.new(f"{prefix}_Red")
        mat.use_nodes = True
        mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (1, 0, 0, 1)
        a.data.materials.append(mat)
        bpy.ops.export_scene.gltf(filepath=path, export_format="GLB")
    # C: one cube with a Base Color image texture (8x8 orange/blue checker), embedded in the GLB
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 4, 0))
    cube = bpy.context.active_object
    cube.name = "C_TexCube"
    img = bpy.data.images.new("C_Checker", 8, 8)
    px = []
    for y in range(8):
        for x in range(8):
            px += [1.0, 0.5, 0.0, 1.0] if (x + y) % 2 == 0 else [0.0, 0.3, 1.0, 1.0]
    img.pixels = px
    img.filepath_raw = os.path.join(tempfile.gettempdir(), "collab_checker.png")
    img.file_format = "PNG"
    img.save()
    mat = bpy.data.materials.new("C_TexMat")
    mat.use_nodes = True
    tex = mat.node_tree.nodes.new("ShaderNodeTexImage")
    tex.image = img
    mat.node_tree.links.new(tex.outputs["Color"], mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"])
    cube.data.materials.append(mat)
    bpy.ops.export_scene.gltf(filepath=GLB_C, export_format="GLB")


def _check_hierarchy(prefix):
    import bpy

    o = bpy.data.objects
    names = [f"{prefix}_Root", f"{prefix}_BodyA", f"{prefix}_BodyB", f"{prefix}_Tip"]
    if any(n not in bpy.context.scene.objects for n in names):
        return False
    if o[f"{prefix}_BodyA"].parent is not o[f"{prefix}_Root"] or o[f"{prefix}_Tip"].parent is not o[f"{prefix}_BodyB"]:
        return False
    if o[f"{prefix}_Tip"].type != "MESH" or len(o[f"{prefix}_BodyA"].data.vertices) != 24:
        return False
    return abs(o[f"{prefix}_Tip"].matrix_world.translation.z - 1.0) < 1e-3  # world position survives re-parenting


def _import(path):
    import bpy

    bpy.ops.object.select_all(action="DESELECT")
    bpy.ops.import_scene.gltf(filepath=path)
    bpy.context.view_layer.update()


def run_host(mode):
    import bpy
    from mathutils import Vector

    sys.path.insert(0, ROOT)
    from freebird_collab.session import CollabSession

    s = CollabSession()
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(tempfile.gettempdir(), "collab_glb_master.blend"))
    if mode == "direct":
        assert s.create_room("DIRECT", "Host", (1, 0.5, 0.2), direct_port=PORT)
    else:
        assert s.create_room("RELAY", "Host", (1, 0.5, 0.2), relay_url=RELAY_URL)
    _wait(lambda: s.uid is not None, 5, s, "welcome")
    _write_state(room=s.room)
    _wait(lambda: _read_state().get("guest_ready"), 30, s, "guest ready")
    _idle(s, 0.5)

    # 1/4/5: HOST imports while connected
    _import(GLB_A)
    assert _check_hierarchy("A"), "host-side hierarchy broken after import"
    _wait(lambda: _read_state().get("guest_saw_A"), 40, s, "guest saw GLB A")

    # 6: transform edit after import (move the whole hierarchy root)
    bpy.data.objects["A_Root"].location = Vector((0, 0, 7))
    bpy.context.view_layer.update()
    _wait(lambda: _read_state().get("guest_saw_A_move"), 30, s, "guest saw A_Root move")

    # 7: Edit-Mode edit of an imported mesh
    _edit_mesh_move_vertex(bpy.data.objects["A_BodyA"], 0, Vector((0, 0, 3.0)))
    _wait(lambda: _read_state().get("guest_saw_A_edit"), 30, s, "guest saw A_BodyA vertex edit")
    bpy.ops.object.mode_set(mode="OBJECT")

    # 2: GUEST imports while connected -> must appear here, with hierarchy.
    # An orphan datablock squatting on one of the names (typical after a local delete) must not block it.
    bpy.data.objects.new("B_Tip", None)
    _wait(lambda: _check_hierarchy("B"), 40, s, "GLB B from guest")
    assert "B_Tip.orphan" in bpy.data.objects, "orphan should have been moved aside"
    print("HOST: GLB B hierarchy from guest OK; B_BodyA material:", bpy.data.objects["B_BodyA"].material_slots[0].material.name)
    col = tuple(round(c, 2) for c in bpy.data.objects["B_BodyA"].material_slots[0].material.diffuse_color)
    assert col[:3] == (1.0, 0.0, 0.0), f"material colour not carried: {col}"
    _wait(lambda: bpy.data.objects["B_Tip"].matrix_world.translation.x > 4.5, 30, s, "guest moved B_Tip")

    _write_state(host_saw_B=True)

    # textured GLB from guest: image + UV must arrive and be wired into Base Color
    def _textured_ok(name):
        ob = bpy.data.objects.get(name)
        if ob is None or name not in bpy.context.scene.objects or not ob.material_slots:
            return False
        mat = ob.material_slots[0].material
        if mat is None:
            return False
        from freebird_collab import object_data
        img = object_data._base_color_image(mat)
        return img is not None and tuple(img.size) == (8, 8) and ob.data.uv_layers.active is not None

    _wait(lambda: _textured_ok("C_TexCube"), 40, s, "textured cube from guest")
    ob = bpy.data.objects["C_TexCube"]
    uvs = [round(l.uv.x, 2) for l in ob.data.uv_layers.active.data]
    assert max(uvs) > 0.5, f"UVs empty: {uvs[:8]}"
    img = bpy.data.objects["C_TexCube"].material_slots[0].material.node_tree.nodes
    print("HOST: textured cube OK, image", [n.image.name for n in img if n.type == "TEX_IMAGE"], "packed:", bpy.data.images.get("C_Checker") is not None)
    # the same GLB imported again on the guest must NOT push the image a second time
    _wait(lambda: "C_TexCube.001" in bpy.context.scene.objects, 40, s, "second import of the textured GLB")
    _idle(s, 1.0)
    n_img_dup = len([i for i in bpy.data.images if i.get("collab_id")])
    assert n_img_dup == 1, f"image datablocks with collab_id: {n_img_dup}"
    assert _textured_ok("C_TexCube.001")
    _write_state(host_saw_C=True)
    before = dict(s.stats["tx_by_type"])
    _idle(s, 2.0)
    after = dict(s.stats["tx_by_type"])
    assert before.get("xform") == after.get("xform") and before.get("obj_data") == after.get("obj_data"), f"idle chatter {before}->{after}"
    _write_state(host_done=True)
    _idle(s, 0.5)
    s.leave()
    print("HOST PASS")


def run_guest(mode):
    import bpy
    from mathutils import Vector

    sys.path.insert(0, ROOT)
    from freebird_collab.session import CollabSession

    s = CollabSession()
    t0 = time.time()
    while not _read_state().get("room") and time.time() - t0 < 20:
        time.sleep(0.05)
    code = _read_state()["room"]
    if mode == "direct":
        assert s.join_room("DIRECT", "Guest", (0.2, 0.6, 1), direct_host="127.0.0.1", direct_port=PORT)
    else:
        assert s.join_room("RELAY", "Guest", (0.2, 0.6, 1), code=code, relay_url=RELAY_URL)
    _wait(lambda: s._scene_loaded_from_host, 20, s, "scene from host")
    _write_state(guest_ready=True)

    _wait(lambda: _check_hierarchy("A"), 40, s, "GLB A from host")
    print("GUEST: GLB A hierarchy OK, A_BodyA mat:", bpy.data.objects["A_BodyA"].material_slots[0].material.name if bpy.data.objects["A_BodyA"].material_slots else None)
    _write_state(guest_saw_A=True)
    _wait(lambda: abs(bpy.data.objects["A_Tip"].matrix_world.translation.z - 8.0) < 1e-2, 30, s, "A_Root move propagates to A_Tip world pos")
    _write_state(guest_saw_A_move=True)
    _wait(lambda: bpy.data.objects["A_BodyA"].data.vertices[0].co.z > 2.0, 30, s, "A_BodyA vertex edit")
    _write_state(guest_saw_A_edit=True)

    # 2: guest imports its own GLB while connected
    _import(GLB_B)
    assert _check_hierarchy("B")
    _idle(s, 1.0)
    bpy.data.objects["B_Tip"].matrix_world.translation = Vector((5, 0, 1))  # transform edit on an imported child
    bpy.data.objects["B_Tip"].location.x = 6.0
    bpy.context.view_layer.update()
    _wait(lambda: _read_state().get("host_saw_B"), 40, s, "host saw B")
    _import(GLB_C)
    _idle(s, 1.5)
    img_msgs = s.stats["tx_by_type"].get("img", [0, 0])[0]
    assert img_msgs == 1, f"img messages after first textured import: {img_msgs}"
    _import(GLB_C)  # again: objects become .001, image content identical -> no re-send
    _idle(s, 1.5)
    img_msgs = s.stats["tx_by_type"].get("img", [0, 0])[0]
    assert img_msgs == 1, f"image was re-sent: {img_msgs}"
    print("GUEST: textured GLB imported twice, image sent once; tx:", {k: tuple(v) for k, v in s.stats["tx_by_type"].items()})
    _wait(lambda: _read_state().get("host_saw_C"), 40, s, "host saw C")
    _wait(lambda: _read_state().get("host_done"), 60, s, "host done")
    s.leave()
    print("GUEST PASS")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "direct"
    role = sys.argv[2] if len(sys.argv) > 2 else None
    if role == "host":
        return run_host(mode)
    if role == "guest":
        return run_guest(mode)
    import glob

    for f in glob.glob(STATE + "*"):
        os.remove(f)
    build_glbs()
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
    rc_h, rc_g = host.wait(180), guest.wait(180)
    for p in procs:
        p.terminate()
    print(f"\n=== GLB {mode.upper()}: host rc={rc_h} guest rc={rc_g} ===")
    sys.exit(0 if rc_h == 0 and rc_g == 0 else 1)


if __name__ == "__main__":
    main()
