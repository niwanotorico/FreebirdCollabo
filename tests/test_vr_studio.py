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

    run_unit_looks()
    print("=== VR STUDIO UNIT: PASS ===")


def _inp(mat, name):
    from vr_studio.materials import _principled

    v = _principled(mat).inputs[name].default_value
    return round(v, 3) if isinstance(v, float) else _r3(v)


def run_unit_looks():
    import bpy
    from vr_studio import looks, materials

    blue, red = _lin("#1E88E5"), _lin("#E53935")
    bpy.ops.mesh.primitive_cube_add(location=(0, 5, 0))
    ob = bpy.context.active_object
    ob.name = "LookBox"
    ob.data.materials.clear()

    # a Look on an object without material creates one; every Look is recognised again
    rep = materials.apply_look([ob], "Matte")
    mat = ob.material_slots[0].material
    assert rep["created"] == ["VR LookBox"] and materials.detect_look(mat) == "Matte"
    for name in looks.LOOK_NAMES:
        materials.apply_look([ob], name)
        assert materials.detect_look(mat) == name, (name, materials.detect_look(mat))

    # Color and Look are independent, in both orders
    materials.apply_color([ob], blue)
    for name in looks.LOOK_NAMES:
        materials.apply_look([ob], name)
        assert _base(mat) == _r3((*blue, 1.0)), name  # the colour survives every Look
        assert _r3(mat.diffuse_color[:3]) == _r3(blue)
    materials.apply_look([ob], "Glass")
    materials.apply_color([ob], red)
    assert materials.detect_look(mat) == "Glass" and _inp(mat, "Transmission Weight") == 1.0
    assert round(mat.diffuse_color[3], 2) == 0.35 and mat.use_raytrace_refraction  # see-through in Solid mode
    materials.apply_look([ob], "Matte")  # nothing of Glass is left behind
    assert _inp(mat, "Transmission Weight") == 0.0 and mat.diffuse_color[3] == 1.0 and not mat.use_raytrace_refraction
    assert _base(mat) == _r3((*red, 1.0))

    # Emission glows in the current colour, and follows a later colour change
    materials.apply_look([ob], "Emission")
    assert _inp(mat, "Emission Strength") == looks.EMISSION_STRENGTH and _inp(mat, "Emission Color") == _r3((*red, 1.0))
    materials.apply_color([ob], blue)
    assert _inp(mat, "Emission Color") == _r3((*blue, 1.0))
    materials.apply_look([ob], "Plastic")
    assert _inp(mat, "Emission Strength") == 0.0

    # Toon: its own node chain feeds the output, Principled kept (unplugged) as the colour holder
    materials.apply_look([ob], "Toon")
    tree = mat.node_tree
    out = next(n for n in tree.nodes if n.type == "OUTPUT_MATERIAL" and n.is_active_output)
    assert out.inputs["Surface"].links[0].from_node.name == looks.TOON_PREFIX + "Emit"
    tint = tree.nodes[looks.TOON_PREFIX + "Tint"]
    b_col = next(s for s in tint.inputs if s.identifier == "B_Color")
    assert _r3(b_col.default_value) == _r3((*blue, 1.0))
    materials.apply_color([ob], red)  # a colour change reaches the toon tint
    assert _r3(b_col.default_value) == _r3((*red, 1.0)) and materials.detect_look(mat) == "Toon"
    ramp = tree.nodes[looks.TOON_PREFIX + "Bands"].color_ramp
    assert ramp.interpolation == "CONSTANT" and len(ramp.elements) == 3
    materials.apply_look([ob], "Clay")  # leaving Toon removes its nodes and plugs the Principled back in
    assert not any(n.name.startswith(looks.TOON_PREFIX) for n in tree.nodes)
    assert out.inputs["Surface"].links[0].from_node.type == "BSDF_PRINCIPLED" and _base(mat) == _r3((*red, 1.0))

    # a texture driving Roughness wins over the Look (only unplugged sockets are written)
    img = tree.nodes.new("ShaderNodeTexImage")
    tree.links.new(img.outputs["Color"], materials._principled(mat).inputs["Roughness"])
    materials.apply_look([ob], "Glossy")
    assert materials._principled(mat).inputs["Roughness"].is_linked and _inp(mat, "Coat Weight") == 1.0

    # a material built around another shader becomes a Principled material with that colour
    odd = bpy.data.materials.new("OnlyEmission")
    materials._ensure_node_tree(odd)
    t = odd.node_tree
    t.nodes.clear()
    e = t.nodes.new("ShaderNodeEmission")
    e.inputs["Color"].default_value = (*blue, 1.0)
    o = t.nodes.new("ShaderNodeOutputMaterial")
    t.links.new(e.outputs[0], o.inputs["Surface"])
    materials.set_material_look(odd, "Metallic")
    assert materials.detect_look(odd) == "Metallic" and _base(odd) == _r3((*blue, 1.0))
    assert o.inputs["Surface"].links[0].from_node.type == "BSDF_PRINCIPLED"

    # copy-on-write applies to Looks too
    bpy.ops.mesh.primitive_cube_add(location=(0, 8, 0))
    other = bpy.context.active_object
    other.data.materials.append(mat)
    rep = materials.apply_look([other], "Metallic")
    assert len(rep["copied"]) == 1 and materials.detect_look(mat) == "Glossy"
    # Grease Pencil materials keep their colour-only behaviour
    gp = bpy.data.materials.new("GPL")
    bpy.data.materials.create_gpencil_data(gp)
    assert materials.set_material_look(gp, "Metallic") is False


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
    """Build the real Studio area with Freebird's own UI code (bl_xr + Freebird's main menu),
    check where it sits next to the main menu, raycast tiles like the laser does, press them."""
    fb = _freebird_dir()
    if fb is None:
        print("=== VR STUDIO UI: SKIPPED (set FREEBIRD_XR_DIR to Freebird's freebird_xr folder) ===")
        return
    sys.path.insert(0, fb)
    import bpy
    from mathutils import Vector

    import bl_xr
    from bl_xr import Image
    from bl_xr.utils import raycast

    Image.base_dir = fb
    import freebird  # noqa: F401  (registers Freebird's settings the main menu reads)
    from freebird.ui import main_menu as mm

    from vr_studio import materials
    from vr_studio.area import SIDE_GAP, StudioArea
    from vr_studio.color_section import SWATCH, ColorSection
    from vr_studio.look_section import TILE, LookSection

    for ob in bpy.context.view_layer.objects:
        ob.select_set(ob.name == "Cube")
    bpy.context.view_layer.objects.active = bpy.data.objects["Cube"]
    bpy.data.objects["Cube"].data.materials.clear()

    area = StudioArea()
    color = area.add(ColorSection())
    look = area.add(LookSection())
    area.attach()
    assert area.node.parent is bl_xr.root and not color.node.style["visible"]
    area.toggle(color)
    area.toggle(look)
    assert color.visible and look.visible and len(color.tiles) == 32 and len(look.tiles) == 8
    # stacking: Color at the bottom, Look right above it
    assert color.node.position.y == 0 and abs(look.node.position.y - (color.height + 0.006)) < 1e-6

    # placement: RIGHT of Freebird's main menu (right-handed), bottom-aligned, slides past an open sub-menu
    for sub in list(mm.submenus.values()) + [mm.submenu_custom, mm.mirror_panel]:
        sub.style["visible"] = False
    left, right, bottom, others = area._menu_space()
    assert (round(left, 3), round(right, 3), round(bottom, 3)) == (0.0, 0.08, 0.051), (left, right, bottom)
    x = area._target_x(left, right, bottom, others)
    assert abs(x - (0.08 + SIDE_GAP)) < 1e-6, x
    mm.submenus["PEN"].style["visible"] = True  # Freebird's pen options open on the right
    x2 = area._target_x(*area._menu_space())
    assert abs(x2 - (0.2 + SIDE_GAP)) < 1e-4, x2
    mm.submenus["PEN"].style["visible"] = False
    mm.submenus["SELECT"].style["visible"] = True  # select options open on the LEFT: no need to move
    assert abs(area._target_x(*area._menu_space()) - x) < 1e-6
    mm.submenus["SELECT"].style["visible"] = False
    bl_xr.main_hand = "left"  # left-handed: mirrored, the area goes to the left of the menu
    assert area._target_x(*area._menu_space()) < 0
    bl_xr.main_hand = "right"

    def hit(tile, size):
        c = tile.local_to_world_point(Vector((size / 2, size / 2, 0)))
        node, _, _ = raycast(c + Vector((0, 0, 0.5)), Vector((0, 0, -1)), object_raycast=False, ui_raycast=True)
        return node

    yellow = color.tiles[18]
    assert yellow.label == "Yellow" and hit(yellow, SWATCH) is yellow
    metal = look.tiles[4]
    assert metal.look == "Metallic" and hit(metal, TILE) is metal, hit(metal, TILE)

    # Color, then Look, then Color: each keeps the other
    yellow.dispatch_event("pointer_main_enter", None)
    assert color.status.text == "Yellow" and "border" in yellow.style
    yellow.dispatch_event("pointer_main_press_end", None)
    yellow.dispatch_event("pointer_main_leave", None)
    mat = bpy.data.objects["Cube"].material_slots[0].material
    assert mat.name == "VR Cube" and _base(mat) == _r3((*yellow.linear, 1.0))
    assert color.last_message == "1 object, 1 new", color.last_message
    metal.dispatch_event("pointer_main_press_end", None)
    assert materials.detect_look(mat) == "Metallic" and _base(mat) == _r3((*yellow.linear, 1.0))
    area._refresh()
    assert metal.current and yellow.current and not look.tiles[0].current
    blue = color.tiles[21]
    blue.dispatch_event("pointer_main_press_end", None)
    assert materials.detect_look(mat) == "Metallic" and _base(mat) == _r3((*blue.linear, 1.0))
    area._refresh()
    assert blue.current and not yellow.current and metal.current

    # nothing selected -> friendly message, nothing created
    for ob in bpy.context.view_layer.objects:
        ob.select_set(False)
    n = len(bpy.data.materials)
    look.tiles[0].dispatch_event("pointer_main_press_end", None)
    assert look.last_message == "Select an object first" and len(bpy.data.materials) == n

    # the plugin entry builds the same area with both sections
    import vr_studio

    vr_studio._on_xr_start(None, "fb.xr_start", None)
    assert set(vr_studio._sections) == {"color", "look"} and vr_studio._area.node.parent is bl_xr.root
    vr_studio._on_xr_end(None, "fb.xr_end", None)
    assert vr_studio._area.node.parent is None
    area.detach()
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

    # 3. host: Toon on the Cube (node chain), Emission on the Ball -> guest sees both Looks, colour kept
    blue = _r3((*_lin("#1E88E5"), 1.0))
    materials.apply_look([cube], "Toon")
    materials.apply_look([bpy.data.objects["Ball"]], "Emission")
    _step(s, "g3", "guest saw Toon + Emission")
    # 4. guest: Glass on the Cube, then a new colour -> host: Glass, toon nodes gone, green
    green = _r3((*_lin("#43A047"), 1.0))
    _wait(lambda: materials.detect_look(bpy.data.materials["Material"]) == "Glass"
          and _val("Material", "Base Color") == green, 30, s, "guest's green glass")
    assert not any(n.name.startswith("VR Toon") for n in bpy.data.materials["Material"].node_tree.nodes)
    assert _val("Material", "Base Color") == green and materials.detect_look(bpy.data.materials["VR Ball"]) == "Emission"
    assert _val("VR Ball", "Base Color") == blue
    _write_state(h4=True)
    _step(s, "g5", "guest done")
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

    blue = _r3((*_lin("#1E88E5"), 1.0))
    _wait(lambda: materials.detect_look(bpy.data.materials["Material"]) == "Toon"
          and materials.detect_look(bpy.data.materials["VR Ball"]) == "Emission", 30, s, "host's Toon + Emission")
    assert _val("Material", "Base Color") == blue and _val("VR Ball", "Base Color") == blue
    assert _val("VR Ball", "Emission Color") == blue
    out = next(n for n in bpy.data.materials["Material"].node_tree.nodes if n.type == "OUTPUT_MATERIAL" and n.is_active_output)
    assert out.inputs["Surface"].links[0].from_node.name == "VR Toon Emit"
    _write_state(g3=True)
    materials.apply_look([cube], "Glass")
    materials.apply_color([cube], _lin("#43A047"))
    _step(s, "h4", "host saw green glass")
    _write_state(g5=True)
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
