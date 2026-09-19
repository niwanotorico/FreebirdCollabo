# SPDX-License-Identifier: GPL-2.0-or-later
"""
Two-instance object-DATA sync test (no VR needed).

    python3 tests/test_data.py direct | relay | ws | wss

Covers: Mesh add, Mesh Edit-Mode vertex edit (both directions), Curve, Surface,
Text, Light, Camera, Empty, property updates, delete, transform, and that an
idle session sends no obj_data at all (bandwidth check).
"""

import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_sync import MODE_URLS, PORT, RELAY_URL, ROOT, _read_state, _write_state  # noqa: E402


def _wait(cond, timeout, s, what):
    import bpy

    t0 = time.time()
    while time.time() - t0 < timeout:
        bpy.context.view_layer.update()  # what the UI does every redraw -> fires depsgraph handler
        s.tick()
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError(f"timeout waiting for {what}")


def _idle(s, seconds):
    import bpy

    t0 = time.time()
    while time.time() - t0 < seconds:
        bpy.context.view_layer.update()
        s.tick()
        time.sleep(0.02)


def _edit_mesh_move_vertex(ob, index, delta):
    """Enter Edit Mode, move one vertex with bmesh, stay in Edit Mode (like a user dragging)."""
    import bmesh
    import bpy

    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    bm = bmesh.from_edit_mesh(ob.data)
    bm.verts.ensure_lookup_table()
    bm.verts[index].co += delta
    bmesh.update_edit_mesh(ob.data)


def _obj_type(name):
    import bpy

    ob = bpy.data.objects.get(name)
    return ob.type if ob else None


# ----------------------------------------------------------------------
def run_host(mode):
    import bpy
    from mathutils import Vector

    sys.path.insert(0, ROOT)
    from freebird_collab.session import CollabSession

    s = CollabSession()
    blend = os.path.join(tempfile.gettempdir(), "collab_data_master.blend")
    bpy.ops.wm.save_as_mainfile(filepath=blend)
    if mode == "direct":
        assert s.create_room("DIRECT", "Host", (1, 0.5, 0.2), direct_port=PORT)
    else:
        assert s.create_room("RELAY", "Host", (1, 0.5, 0.2), relay_url=RELAY_URL)
    _wait(lambda: s.uid is not None, 5, s, "welcome")
    _write_state(room=s.room)
    _wait(lambda: _read_state().get("guest_ready"), 30, s, "guest ready")
    _idle(s, 0.5)
    base_tx = dict(s.stats["tx_by_type"])

    # --- add one object of every supported type -----------------------------
    bpy.ops.mesh.primitive_ico_sphere_add(radius=1, location=(3, 0, 0))
    bpy.context.active_object.name = "HostMesh"
    bpy.ops.curve.primitive_bezier_circle_add(radius=1, location=(0, 3, 0))
    bpy.context.active_object.name = "HostCurve"
    bpy.ops.surface.primitive_nurbs_surface_sphere_add(radius=1, location=(0, -3, 0))
    bpy.context.active_object.name = "HostSurface"
    bpy.ops.object.text_add(location=(0, 0, 3))
    bpy.context.active_object.name = "HostText"
    bpy.context.active_object.data.body = "hello machida"
    bpy.ops.object.light_add(type="SPOT", location=(5, 5, 5))
    bpy.context.active_object.name = "HostLight"
    bpy.context.active_object.data.energy = 777.0
    bpy.ops.object.camera_add(location=(-5, -5, 5))
    bpy.context.active_object.name = "HostCamera"
    bpy.context.active_object.data.lens = 85.0
    bpy.ops.object.empty_add(type="ARROWS", location=(0, 0, -3))
    bpy.context.active_object.name = "HostEmpty"
    bpy.context.active_object.empty_display_size = 2.5
    _wait(lambda: _read_state().get("guest_saw_adds"), 30, s, "guest saw all adds")

    # --- Edit Mode vertex edit on the existing Cube (stay in edit mode while it syncs) --
    cube = bpy.data.objects["Cube"]
    _edit_mesh_move_vertex(cube, 0, Vector((0, 0, 4.0)))
    _wait(lambda: _read_state().get("guest_saw_vertex"), 30, s, "guest saw vertex edit (host still in EDIT mode)")
    bpy.ops.object.mode_set(mode="OBJECT")

    # --- property updates on existing datablocks --------------------------------
    bpy.data.objects["HostLight"].data.energy = 123.0
    bpy.data.objects["HostCamera"].data.lens = 24.0
    bpy.data.objects["HostText"].data.body = "edited"
    bpy.data.objects["HostEmpty"].empty_display_size = 0.5
    bpy.data.objects["HostCurve"].data.bevel_depth = 0.1
    bpy.data.objects["HostMesh"].location = Vector((3, 0, 9))
    _wait(lambda: _read_state().get("guest_saw_updates"), 30, s, "guest saw property updates")

    # --- delete ---------------------------------------------------------------
    bpy.data.objects.remove(bpy.data.objects["HostSurface"], do_unlink=True)
    _wait(lambda: _read_state().get("guest_saw_delete"), 30, s, "guest saw delete")

    # --- guest edits come back to us ------------------------------------------
    _wait(lambda: bpy.data.objects.get("GuestMesh") is not None and len(bpy.data.objects["GuestMesh"].data.vertices) == 8, 30, s, "guest mesh")
    _wait(lambda: bpy.data.objects["GuestMesh"].data.vertices[0].co.z < -2.0, 30, s, "guest vertex edit")  # local -0.5 -> -2.5
    print("HOST: GuestMesh vertex 0 =", tuple(round(c, 2) for c in bpy.data.objects["GuestMesh"].data.vertices[0].co))

    # --- bandwidth: idle must send nothing ------------------------------------
    before = dict(s.stats["tx_by_type"])
    _idle(s, 3.0)
    after = dict(s.stats["tx_by_type"])
    assert before.get("obj_data") == after.get("obj_data"), f"idle sent obj_data: {before} -> {after}"
    assert before.get("xform") == after.get("xform"), f"idle sent xform: {before} -> {after}"
    sent = {k: (v[0] - base_tx.get(k, [0, 0])[0], v[1] - base_tx.get(k, [0, 0])[1]) for k, v in after.items()}
    print("HOST tx since guest ready (count, bytes):", sent)
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

    def d(name):
        return bpy.data.objects[name].data

    _wait(lambda: _obj_type("HostMesh") == "MESH" and len(d("HostMesh").vertices) == 42, 30, s, "HostMesh")
    _wait(lambda: _obj_type("HostCurve") == "CURVE" and len(d("HostCurve").splines[0].bezier_points) == 4, 30, s, "HostCurve")
    _wait(lambda: _obj_type("HostSurface") == "MESH" and len(d("HostSurface").polygons) > 10 and bpy.data.objects["HostSurface"].name in bpy.context.scene.objects, 30, s, "HostSurface (arrives as evaluated mesh)")
    _wait(lambda: _obj_type("HostText") == "FONT" and d("HostText").body == "hello machida", 30, s, "HostText")
    _wait(lambda: _obj_type("HostLight") == "LIGHT" and d("HostLight").type == "SPOT" and abs(d("HostLight").energy - 777) < 1e-3, 30, s, "HostLight")
    _wait(lambda: _obj_type("HostCamera") == "CAMERA" and abs(d("HostCamera").lens - 85) < 1e-3, 30, s, "HostCamera")
    _wait(lambda: _obj_type("HostEmpty") == "EMPTY" and bpy.data.objects["HostEmpty"].empty_display_type == "ARROWS" and abs(bpy.data.objects["HostEmpty"].empty_display_size - 2.5) < 1e-3, 30, s, "HostEmpty")
    print("GUEST: all 7 object types arrived")
    _write_state(guest_saw_adds=True)

    _wait(lambda: d("Cube").vertices[0].co.z > 3.5, 30, s, "Cube vertex edit from host")
    print("GUEST: Cube vertex 0 =", tuple(round(c, 2) for c in d("Cube").vertices[0].co), "material slots kept:", len(d("Cube").materials))
    _write_state(guest_saw_vertex=True)

    _wait(lambda: abs(d("HostLight").energy - 123) < 1e-3, 30, s, "light energy update")
    _wait(lambda: abs(d("HostCamera").lens - 24) < 1e-3, 30, s, "camera lens update")
    _wait(lambda: d("HostText").body == "edited", 30, s, "text body update")
    _wait(lambda: abs(bpy.data.objects["HostEmpty"].empty_display_size - 0.5) < 1e-3, 30, s, "empty size update")
    _wait(lambda: abs(d("HostCurve").bevel_depth - 0.1) < 1e-4, 30, s, "curve bevel update")
    _wait(lambda: abs(bpy.data.objects["HostMesh"].location.z - 9) < 1e-3, 30, s, "mesh transform")
    print("GUEST: property updates arrived")
    _write_state(guest_saw_updates=True)

    _wait(lambda: "HostSurface" not in bpy.data.objects, 30, s, "delete")
    _write_state(guest_saw_delete=True)

    # guest side: add a mesh and edit it in Edit Mode
    bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, -6))
    gm = bpy.context.active_object
    gm.name = "GuestMesh"
    _idle(s, 0.6)
    _edit_mesh_move_vertex(gm, 0, Vector((0, 0, -2.0)))
    _idle(s, 1.0)
    bpy.ops.object.mode_set(mode="OBJECT")

    _wait(lambda: _read_state().get("host_done"), 60, s, "host done")
    print("GUEST rx:", s.stats["rx"])
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

    from test_sync import STATE

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
    rc_h, rc_g = host.wait(180), guest.wait(180)
    for p in procs:
        p.terminate()
    print(f"\n=== DATA {mode.upper()}: host rc={rc_h} guest rc={rc_g} ===")
    sys.exit(0 if rc_h == 0 and rc_g == 0 else 1)


if __name__ == "__main__":
    main()
