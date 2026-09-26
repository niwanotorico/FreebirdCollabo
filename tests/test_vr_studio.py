# SPDX-License-Identifier: GPL-2.0-or-later
"""
VR Studio (freebird_plugin/vr_studio) test: one-press colour, no VR needed.

    python3 tests/test_vr_studio.py unit           # colour logic on Blender data (single process)
    python3 tests/test_vr_studio.py ui             # the bl_xr panel: raycast a swatch, press it (needs Freebird's files,
                                                   #   FREEBIRD_XR_DIR=<...>/scripts/addons/freebird_xr; skipped if missing)
    python3 tests/test_vr_studio.py direct | relay # unit, then two instances: a VR colour reaches the other side
                                                   #   through the EXISTING collab material sync (no collab changes)
"""

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "freebird_plugin"))

from test_data import _wait  # noqa: E402
from test_materials import _join, _session, _slots, _step, _val  # noqa: E402
from test_sync import MODE_URLS, PORT, ROOT, STATE, _read_state, _write_state  # noqa: E402


def _lin(hex_str):
    from vr_studio.palette import hex_to_srgb, srgb_to_linear

    return srgb_to_linear(hex_to_srgb(hex_str))


def _r3(v):
    return tuple(round(x, 3) for x in v)


def _base(mat):
    from vr_studio.materials import _principled

    return _r3(_principled(mat).inputs["Base Color"].default_value)


# ----------------------------------------------------------------------
def run_unit():
    import bpy
    from vr_studio import materials, palette

    assert len(palette.PALETTE) == 32 and len(palette.swatches()) == 32
    assert _r3(_lin("#FFFFFF")) == (1.0, 1.0, 1.0) and _r3(_lin("#000000")) == (0.0, 0.0, 0.0)
    assert abs(palette.srgb_to_linear((0.5,))[0] - 0.214) < 0.001, "sRGB -> linear"

    red, blue, green = _lin("#E53935"), _lin("#1E88E5"), _lin("#43A047")
    cube = bpy.data.objects["Cube"]
    cube.data.materials.clear()

    # nothing selected: nothing happens
    for ob in bpy.context.view_layer.objects:
        ob.select_set(False)
    rep = materials.apply_color(materials.selected_targets(), red)
    assert rep["objects"] == [] and materials.describe(rep) == "Select an object first"

    # 1. object without material -> new "VR Cube", Base Color + Solid colour set
    cube.select_set(True)
    targets = materials.selected_targets()
    assert targets == [cube], targets  # camera / light are never targets
    rep = materials.apply_color(targets, red)
    assert rep["created"] == ["VR Cube"], rep
    mat = cube.material_slots[0].material
    assert mat.name == "VR Cube" and _base(mat) == _r3((*red, 1.0)) and _r3(mat.diffuse_color) == _r3((*red, 1.0))
    assert _r3(mat[materials.COLOR_PROP]) == _r3(red)

    # 2. pressing another colour edits the same material (no material spam)
    n = len(bpy.data.materials)
    materials.apply_color([cube], blue)
    assert len(bpy.data.materials) == n and _base(cube.material_slots[0].material) == _r3((*blue, 1.0))

    # 3. material shared with an UNSELECTED object -> copy-on-write, the other keeps its colour
    bpy.ops.mesh.primitive_uv_sphere_add(location=(3, 0, 0))
    sphere = bpy.context.active_object
    sphere.data.materials.append(mat)
    for ob in bpy.context.view_layer.objects:
        ob.select_set(ob == sphere)
    rep = materials.apply_color(materials.selected_targets(), green)
    assert len(rep["copied"]) == 1 and rep["copied"][0] == f"VR {sphere.name}", rep
    assert sphere.material_slots[0].material != mat and _base(sphere.material_slots[0].material) == _r3((*green, 1.0))
    assert _base(mat) == _r3((*blue, 1.0)), "the unselected cube must keep its colour"

    # 4. shared material, ALL its users selected -> edited in place, still shared
    shared = bpy.data.materials.new("Shared")
    for ob in (cube, sphere):
        ob.data.materials.clear()
        ob.data.materials.append(shared)
        ob.select_set(True)
    n = len(bpy.data.materials)
    rep = materials.apply_color(materials.selected_targets(), red)
    assert rep["edited"] == ["Shared"] and len(bpy.data.materials) == n, rep
    assert cube.material_slots[0].material == sphere.material_slots[0].material == shared

    # 5. several slots: every slot painted
    cube.data.materials.clear()
    cube.data.materials.append(bpy.data.materials.new("A"))
    cube.data.materials.append(bpy.data.materials.new("B"))
    materials.apply_color([cube], blue)
    assert [_base(s.material) for s in cube.material_slots] == [_r3((*blue, 1.0))] * 2

    # 6. a texture on Base Color is unplugged (image node kept) so the colour shows
    tex_mat = bpy.data.materials.new("Textured")
    materials._ensure_principled(tex_mat)
    tree = tex_mat.node_tree
    img_node = tree.nodes.new("ShaderNodeTexImage")
    img_node.image = bpy.data.images.new("T", 4, 4)
    tree.links.new(img_node.outputs["Color"], materials._principled(tex_mat).inputs["Base Color"])
    cube.data.materials.clear()
    cube.data.materials.append(tex_mat)
    for ob in bpy.context.view_layer.objects:
        ob.select_set(ob == cube)
    rep = materials.apply_color(materials.selected_targets(), green)
    assert rep["unplugged"] == ["Textured"], rep
    assert not materials._principled(tex_mat).inputs["Base Color"].is_linked and img_node.name in tree.nodes
    assert "texture unplugged" in materials.describe(rep)

    # 7. a material without a Principled BSDF: its shader's Color is set (tree left as it is)
    emit = bpy.data.materials.new("Glow")
    materials._ensure_node_tree(emit)
    t = emit.node_tree
    t.nodes.clear()
    e = t.nodes.new("ShaderNodeEmission")
    o = t.nodes.new("ShaderNodeOutputMaterial")
    t.links.new(e.outputs[0], o.inputs["Surface"])
    materials.set_material_color(emit, red)
    assert _r3(e.inputs["Color"].default_value) == _r3((*red, 1.0)) and len(t.nodes) == 2

    # 8. Grease Pencil material: stroke + fill colour
    gp = bpy.data.materials.new("GP")
    bpy.data.materials.create_gpencil_data(gp)
    materials.set_material_color(gp, blue)
    assert _r3(gp.grease_pencil.color[:3]) == _r3(blue) and _r3(gp.grease_pencil.fill_color[:3]) == _r3(blue)

    # 9. curves (Freebird pen / pipe strokes are curve objects) are paintable
    cu = bpy.data.curves.new("Stroke", "CURVE")
    cob = bpy.data.objects.new("Stroke", cu)
    bpy.context.scene.collection.objects.link(cob)
    rep = materials.apply_color([cob], red)
    assert rep["created"] == ["VR Stroke"] and _base(cob.material_slots[0].material) == _r3((*red, 1.0))

    print("=== VR STUDIO UNIT: PASS ===")


# ----------------------------------------------------------------------
def _freebird_dir():
    d = os.environ.get("FREEBIRD_XR_DIR")
    if d:
        return d
    d = os.path.expanduser("~/AppData/Roaming/Blender Foundation/Blender/5.2/scripts/addons/freebird_xr")
    if os.path.isdir(os.path.join(d, "bl_xr")):
        return d
    return None


def run_ui():
    """Build the real panel with Freebird's bl_xr, raycast a swatch like the laser does, press it."""
    fb = _freebird_dir()
    if fb is None:
        print("=== VR STUDIO UI: SKIPPED (set FREEBIRD_XR_DIR to Freebird's freebird_xr folder) ===")
        return
    sys.path.insert(0, fb)
    import bpy
    from mathutils import Vector

    import bl_xr
    from bl_xr.utils import raycast
    from bl_xr.utils.event_utils import has_pointer_event_listeners

    from vr_studio.panel import ColorPanel, PAD, CELL, SWATCH
    from vr_studio import materials

    for ob in bpy.context.view_layer.objects:
        ob.select_set(ob.name == "Cube")
    bpy.data.objects["Cube"].data.materials.clear()

    panel = ColorPanel()
    panel.attach()
    assert panel.node.parent is bl_xr.root and not panel.node.style["visible"]
    panel.toggle()
    assert panel.node.style["visible"]
    assert len(panel.grid.child_nodes) == 32

    # swatch index 18 = row 2 (vivid), col 2 = "Yellow"
    idx = 18
    sw = panel.grid.child_nodes[idx]
    assert sw.color_name == "Yellow", sw.color_name
    centre = sw.local_to_world_point(Vector((SWATCH / 2, SWATCH / 2, 0)))
    node, point, _ = raycast(centre + Vector((0, 0, 0.5)), Vector((0, 0, -1)), object_raycast=False, ui_raycast=True)
    assert node is sw, f"laser hit {node} instead of the Yellow swatch"
    assert has_pointer_event_listeners(node)

    # between two swatches the laser lands on the panel background (still a UI hit: laser stays visible)
    gap_pt = panel.grid.local_to_world_point(Vector((CELL - (CELL - SWATCH) / 2, SWATCH / 2, 0)))
    node2, _, _ = raycast(gap_pt + Vector((0, 0, 0.5)), Vector((0, 0, -1)), object_raycast=False, ui_raycast=True)
    assert node2 is panel.background, node2

    # hover + press, dispatched the way bl_xr dispatches pointer events
    sw.dispatch_event("pointer_main_enter", None)
    assert panel.status.text == "Yellow" and "border" in sw.style
    sw.dispatch_event("pointer_main_press_end", None)
    mat = bpy.data.objects["Cube"].material_slots[0].material
    assert mat.name == "VR Cube" and _base(mat) == _r3((*sw.linear, 1.0)), mat
    assert _r3(bpy.data.objects["Cube"].color[:3]) == _r3(sw.linear)
    assert panel.current is sw and panel.last_message == "1 object, 1 new", panel.last_message
    sw.dispatch_event("pointer_main_leave", None)
    assert panel.status.text == "1 object, 1 new" and sw.style["border"][1][2] == 1.0  # keeps the "current" ring

    # nothing selected -> friendly message, nothing created
    for ob in bpy.context.view_layer.objects:
        ob.select_set(False)
    n = len(bpy.data.materials)
    panel.grid.child_nodes[0].dispatch_event("pointer_main_press_end", None)
    assert panel.last_message == "Select an object first" and len(bpy.data.materials) == n

    panel.detach()
    assert panel.node.parent is None
    print("=== VR STUDIO UI: PASS ===")


def _check_fb_info():
    """Freebird reads fb_info with ast.literal_eval before importing: it must be a plain dict literal."""
    import ast

    path = os.path.join(os.path.dirname(HERE), "freebird_plugin", "vr_studio", "__init__.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    for st in tree.body:
        if isinstance(st, ast.Assign) and any(getattr(t, "id", None) == "fb_info" for t in st.targets):
            info = ast.literal_eval(st.value)
            assert isinstance(info, dict) and info.get("name"), info
            return info
    raise AssertionError("fb_info missing")


# ----------------------------------------------------------------------
def run_host(mode):
    import bpy
    from vr_studio import materials

    s = _session(mode, "host")
    _wait(lambda: s.uid is not None, 10, s, "welcome")
    _write_state(room=s.room)
    _step(s, "guest_ready")
    cube = bpy.data.objects["Cube"]

    # 1. host paints the Cube (factory "Material", used only by the Cube -> edited in place),
    #    and a new object without material (-> new "VR Ball") -> guest gets both
    bpy.ops.mesh.primitive_uv_sphere_add(location=(3, 0, 0))
    bpy.context.active_object.name = "Ball"
    t = time.time()
    while time.time() - t < 1.5:  # let obj_add for Ball go out first (as a user would)
        bpy.context.view_layer.update()
        s.tick()
        time.sleep(0.02)
    for ob in bpy.context.view_layer.objects:
        ob.select_set(ob.name in ("Cube", "Ball"))
    rep = materials.apply_color(materials.selected_targets(), _lin("#E53935"))
    assert rep["edited"] == ["Material"] and rep["created"] == ["VR Ball"], rep
    _step(s, "g1", "guest saw the red Cube and VR Ball")

    # 2. guest repaints it -> host sees the new colour, same material
    _wait(lambda: _slots("Cube") == ["Material"] and _val("Material", "Base Color") == _r3((*_lin("#1E88E5"), 1.0))
          and _val("VR Ball", "Base Color") == _r3((*_lin("#1E88E5"), 1.0)), 30, s, "guest's blue")
    assert len([m for m in bpy.data.materials if m.name.startswith("VR ")]) == 1
    _write_state(h2=True)
    _step(s, "g3", "guest done")
    print("=== VR STUDIO SYNC host: PASS ===")


def run_guest(mode):
    import bpy
    from vr_studio import materials

    s = _session(mode, "guest")
    t0 = time.time()
    while not _read_state().get("room") and time.time() - t0 < 20:
        time.sleep(0.05)
    _join(s, mode)
    _write_state(guest_ready=True)

    red = _r3((*_lin("#E53935"), 1.0))
    _wait(lambda: _slots("Cube") == ["Material"] and _val("Material", "Base Color") == red
          and _slots("Ball") == ["VR Ball"] and _val("VR Ball", "Base Color") == red, 30, s, "host's red Cube + VR Ball")
    assert _r3(bpy.data.materials["VR Ball"].diffuse_color[:3]) == _r3(_lin("#E53935"))
    _write_state(g1=True)

    cube = bpy.data.objects["Cube"]
    for ob in bpy.context.view_layer.objects:
        ob.select_set(ob == cube or ob.name == "Ball")
    materials.apply_color(materials.selected_targets(), _lin("#1E88E5"))
    _step(s, "h2", "host saw blue")
    _write_state(g3=True)
    t = time.time()
    while time.time() - t < 1.0:
        s.tick()
        time.sleep(0.02)
    print("=== VR STUDIO SYNC guest: PASS ===")


# ----------------------------------------------------------------------
def _args():
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]


def _child(mode, role):
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
    mode = args[0] if args else "unit"
    role = args[1] if len(args) > 1 else None
    if role == "host":
        return run_host(mode)
    if role == "guest":
        return run_guest(mode)
    if role == "-":
        return run_unit() if mode == "unit" else run_ui()
    print("fb_info:", _check_fb_info())
    for part in ("unit", "ui"):
        rc = subprocess.call(_child(part, "-"))
        if rc:
            sys.exit(rc)
        if part == mode:
            sys.exit(0)
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
    host = subprocess.Popen(_child(run_mode, "host"), env=env)
    time.sleep(1.0)
    guest = subprocess.Popen(_child(run_mode, "guest"), env=env)
    rc_h, rc_g = host.wait(300), guest.wait(300)
    for p in procs:
        p.terminate()
    print(f"\n=== VR STUDIO {mode.upper()}: host rc={rc_h} guest rc={rc_g} ===")
    sys.exit(0 if rc_h == 0 and rc_g == 0 else 1)


if __name__ == "__main__":
    main()
