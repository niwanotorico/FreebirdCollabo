# SPDX-License-Identifier: GPL-2.0-or-later
"""
Two-instance Grease Pencil sync test (no VR needed).

With a bpy Python package:
    python3 tests/test_grease_pencil.py direct

With Blender itself:
    blender --background --factory-startup --python tests/test_grease_pencil.py -- direct

Covers creation, stroke addition, point edits, material style, transform,
bidirectional updates, and idle bandwidth.
"""

import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_data import _idle, _obj_type, _wait  # noqa: E402
from test_sync import PORT, ROOT, STATE, _read_state, _write_state  # noqa: E402


def _new_gp(name, points, color):
    import bpy

    gp = bpy.data.grease_pencils.new(name)
    ob = bpy.data.objects.new(name, gp)
    bpy.context.scene.collection.objects.link(ob)
    layer = gp.layers.new("Lines", set_active=True)
    frame = layer.frames.new(1)
    frame.drawing.add_strokes([len(points)])
    stroke = frame.drawing.strokes[0]
    for point, co in zip(stroke.points, points):
        point.position = co
        point.radius = 0.02
    mat = bpy.data.materials.new(name + "Ink")
    bpy.data.materials.create_gpencil_data(mat)
    mat.grease_pencil.color = color
    gp.materials.append(mat)
    gp.update_tag()
    return ob


def _drawing(name):
    import bpy

    return bpy.data.objects[name].data.layers[0].frames[0].drawing


def run_host():
    import bpy
    from mathutils import Vector

    sys.path.insert(0, ROOT)
    from freebird_collab.session import CollabSession

    s = CollabSession()
    blend = os.path.join(tempfile.gettempdir(), "collab_gp_master.blend")
    bpy.ops.wm.save_as_mainfile(filepath=blend)
    assert s.create_room("DIRECT", "Host", (1, 0.5, 0.2), direct_port=PORT)
    _wait(lambda: s.uid is not None, 5, s, "welcome")
    _write_state(room=s.room)
    _wait(lambda: _read_state().get("guest_ready"), 30, s, "guest ready")

    ob = _new_gp("HostGP", [(0, 0, 0), (1, 0, 0), (2, 1, 0)], (0.2, 0.4, 0.8, 1.0))
    ob.location = (3, 4, 5)
    _wait(lambda: _read_state().get("guest_saw_add"), 30, s, "guest saw Grease Pencil add")

    drawing = _drawing("HostGP")
    drawing.add_strokes([2])
    drawing.strokes[1].points[0].position = (4, 0, 0)
    drawing.strokes[1].points[1].position = (5, 1, 0)
    drawing.strokes[0].points[1].position += Vector((0, 0, 2))
    ob.data.update_tag()
    _wait(lambda: _read_state().get("guest_saw_edit"), 30, s, "guest saw Grease Pencil edit")

    _wait(lambda: _obj_type("GuestGP") == "GREASEPENCIL", 30, s, "guest Grease Pencil")
    _wait(lambda: len(_drawing("GuestGP").strokes) == 1 and
          _drawing("GuestGP").strokes[0].points[1].position.y < -1.5,
          30, s, "guest Grease Pencil edit")

    before = dict(s.stats["tx_by_type"])
    _idle(s, 3.0)
    after = dict(s.stats["tx_by_type"])
    assert before.get("obj_data") == after.get("obj_data"), f"idle sent obj_data: {before} -> {after}"
    assert before.get("xform") == after.get("xform"), f"idle sent xform: {before} -> {after}"
    _write_state(host_done=True)
    _idle(s, 0.5)
    s.leave()
    print("HOST GP PASS")


def run_guest():
    import bpy

    sys.path.insert(0, ROOT)
    from freebird_collab.session import CollabSession

    s = CollabSession()
    t0 = time.time()
    while not _read_state().get("room") and time.time() - t0 < 20:
        time.sleep(0.05)
    assert s.join_room("DIRECT", "Guest", (0.2, 0.6, 1), direct_host="127.0.0.1", direct_port=PORT)
    _wait(lambda: s._scene_loaded_from_host, 20, s, "scene from host")
    _write_state(guest_ready=True)

    _wait(lambda: _obj_type("HostGP") == "GREASEPENCIL", 30, s, "HostGP")
    drawing = _drawing("HostGP")
    assert len(drawing.strokes) == 1 and len(drawing.strokes[0].points) == 3
    mat = bpy.data.objects["HostGP"].data.materials[0]
    assert mat.is_grease_pencil
    assert tuple(round(x, 2) for x in mat.grease_pencil.color) == (0.2, 0.4, 0.8, 1.0)
    assert tuple(round(x, 2) for x in bpy.data.objects["HostGP"].location) == (3.0, 4.0, 5.0)
    _write_state(guest_saw_add=True)

    _wait(lambda: len(_drawing("HostGP").strokes) == 2 and
          _drawing("HostGP").strokes[0].points[1].position.z > 1.5,
          30, s, "HostGP stroke and point edit")
    _write_state(guest_saw_edit=True)

    guest = _new_gp("GuestGP", [(0, 0, 0), (0, -1, 0), (1, -1, 0)], (0.8, 0.3, 0.1, 1.0))
    _idle(s, 0.8)
    _drawing("GuestGP").strokes[0].points[1].position.y = -2.0
    guest.data.update_tag()
    _idle(s, 0.8)

    _wait(lambda: _read_state().get("host_done"), 60, s, "host done")
    s.leave()
    print("GUEST GP PASS")


def _args():
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]


def _child_command(role):
    args = ["direct", role]
    try:
        import bpy
        blender = bpy.app.binary_path
    except ImportError:
        blender = ""
    if blender:
        return [blender, "--background", "--factory-startup", "--python", __file__, "--", *args]
    return [sys.executable, __file__, *args]


def main():
    args = _args()
    role = args[1] if len(args) > 1 else None
    if role == "host":
        return run_host()
    if role == "guest":
        return run_guest()

    import glob

    for path in glob.glob(STATE + "*"):
        os.remove(path)
    host = subprocess.Popen(_child_command("host"))
    time.sleep(1.0)
    guest = subprocess.Popen(_child_command("guest"))
    rc_h, rc_g = host.wait(180), guest.wait(180)
    print(f"\n=== GREASE PENCIL DIRECT: host rc={rc_h} guest rc={rc_g} ===")
    sys.exit(0 if rc_h == 0 and rc_g == 0 else 1)


if __name__ == "__main__":
    main()
