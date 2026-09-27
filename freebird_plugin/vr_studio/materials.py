# SPDX-License-Identifier: GPL-2.0-or-later
"""What the VR buttons actually do to Blender data. Only bpy, no Freebird: testable headless.

Colour ("select -> press a colour -> done"):
  * every selected object that can hold materials gets the colour on all of its material slots
  * no material yet -> a new "VR <object>" material (Principled BSDF) is created and assigned
  * a material that is also used by an object that is NOT selected is copied first
    (copy-on-write), so painting one chair does not repaint every chair sharing "Wood"
  * Principled "Base Color" is set; a texture feeding Base Color is unplugged (the image node
    stays in the tree, Undo brings the link back); viewport Solid colour (diffuse_color) is
    set too, so the change is visible in Solid shading in VR
  * Grease Pencil materials get stroke + fill colour

Look ("select -> press a Look -> done", see looks.py):
  * same object / material rules as Colour (new material, copy-on-write, all slots)
  * writes every "Look-owned" Principled input + a few material settings, never Base Color
  * Colour and Look are independent: a Look keeps the colour, a colour keeps the Look.
    Nothing is remembered in custom properties: what depends on both (Emission Color,
    the Toon tint) is derived from the material itself, so it also works when the other
    person in a collab room presses the buttons.

Collaboration: nothing here talks to FreebirdCollabo. Its material sync already notices
new materials, changed Principled values, removed links and slot changes, so a colour
applied in VR reaches the other people in the room through the existing sync.
"""

import warnings

import bpy

from .looks import LOOK_NAMES, TOON_PREFIX, TOON_RAMP, look_values

COLOR_PROP = "fbvr_color"
VR_MATERIAL_PREFIX = "VR "


def selected_targets(context=None):
    """Selected objects that can hold materials (plus the active object while in Edit Mode)."""
    context = context or bpy.context
    view_layer = context.view_layer
    out = []
    for ob in view_layer.objects:
        if ob.select_get(view_layer=view_layer) and _can_hold_materials(ob):
            out.append(ob)
    active = view_layer.objects.active
    if active is not None and active not in out and active.mode == "EDIT" and _can_hold_materials(active):
        out.append(active)
    return out


def _can_hold_materials(ob):
    data = getattr(ob, "data", None)
    return data is not None and hasattr(data, "materials") and not getattr(data, "library", None)


# ----------------------------------------------------------------------
# material plumbing
# ----------------------------------------------------------------------
def _principled(mat):
    tree = getattr(mat, "node_tree", None)
    if tree is not None:
        for n in tree.nodes:
            if n.type == "BSDF_PRINCIPLED":
                return n
    return None


def _ensure_node_tree(mat):
    if mat.is_grease_pencil or mat.node_tree is not None:
        return
    with warnings.catch_warnings():  # Material.use_nodes is deprecated in 5.x but still what builds the tree
        warnings.simplefilter("ignore")
        try:
            mat.use_nodes = True
        except Exception:
            pass


def _ensure_principled(mat):
    """Principled BSDF of the material; builds the default Principled -> Output tree if the tree is empty."""
    _ensure_node_tree(mat)
    node = _principled(mat)
    tree = mat.node_tree
    if node is None and tree is not None and not len(tree.nodes):
        node = tree.nodes.new("ShaderNodeBsdfPrincipled")
        out = tree.nodes.new("ShaderNodeOutputMaterial")
        out.location = (300, 0)
        tree.links.new(node.outputs[0], out.inputs[0])
    return node


def _surface_shader(mat):
    """The node plugged into the active Material Output's Surface, for materials without a Principled BSDF."""
    tree = getattr(mat, "node_tree", None)
    if tree is None:
        return None
    outputs = [n for n in tree.nodes if n.type == "OUTPUT_MATERIAL"]
    out = next((n for n in outputs if n.is_active_output), outputs[0] if outputs else None)
    if out is None or not out.inputs["Surface"].is_linked:
        return None
    return out.inputs["Surface"].links[0].from_node


def _by_id(sockets, identifier):
    return next(s for s in sockets if s.identifier == identifier)


def new_material(name):
    mat = bpy.data.materials.new(name)
    _ensure_principled(mat)
    return mat


def _set_socket_color(tree, sock, rgba):
    unplugged = False
    for link in list(sock.links):  # paint over a texture: unplug it, keep the image node
        tree.links.remove(link)
        unplugged = True
    sock.default_value = rgba
    return unplugged


def set_material_color(mat, linear_rgb):
    """Write one colour into a material. Returns True if a texture link was unplugged."""
    r, g, b = linear_rgb
    unplugged = False
    if mat.is_grease_pencil and mat.grease_pencil is not None:
        gp = mat.grease_pencil
        gp.color = (r, g, b, gp.color[3])
        gp.fill_color = (r, g, b, gp.fill_color[3])
    else:
        node = _ensure_principled(mat)
        if node is None:
            node = _surface_shader(mat)  # e.g. an Emission- or Diffuse-only material: colour its shader
            sock = None
            if node is not None:
                sock = next((s for s in node.inputs if s.name in ("Color", "Base Color") and s.type == "RGBA"), None)
        else:
            sock = node.inputs["Base Color"]
            if node.inputs["Emission Strength"].default_value > 0 and not node.inputs["Emission Color"].is_linked:
                node.inputs["Emission Color"].default_value = (r, g, b, 1.0)  # Emission Look glows in the colour
            tint = mat.node_tree.nodes.get(TOON_PREFIX + "Tint")
            if tint is not None:
                _by_id(tint.inputs, "B_Color").default_value = (r, g, b, 1.0)
        if sock is not None:
            unplugged = _set_socket_color(mat.node_tree, sock, (r, g, b, 1.0))
    mat.diffuse_color = (r, g, b, mat.diffuse_color[3])  # Solid-mode viewport colour
    mat[COLOR_PROP] = [r, g, b]
    return unplugged


# ----------------------------------------------------------------------
# the one-press action
# ----------------------------------------------------------------------
def _users(mat):
    """Objects whose material slots show this material."""
    return {ob for ob in bpy.data.objects for slot in ob.material_slots if slot.material == mat}


def _vr_name(ob, index, count):
    return f"{VR_MATERIAL_PREFIX}{ob.name}" if count <= 1 else f"{VR_MATERIAL_PREFIX}{ob.name} {index + 1}"


def _material_for_slot(ob, index, count, selection, copies):
    """The material this object's slot should be painted on (created or copied as needed)."""
    slot = ob.material_slots[index] if index < len(ob.material_slots) else None
    mat = slot.material if slot is not None else None
    if mat is None:
        return new_material(_vr_name(ob, index, count)), "created"
    if mat.library is not None or not _users(mat) <= selection:
        key = mat.name  # one copy per press, shared by the selected objects that shared the original
        if key not in copies:
            copy = mat.copy()
            copy.name = _vr_name(ob, index, count)
            copies[key] = copy
        return copies[key], "copied"
    return mat, "edited"


def _assign(ob, index, mat):
    if index < len(ob.material_slots):
        if ob.material_slots[index].material != mat:
            ob.material_slots[index].material = mat  # respects the slot's Data / Object link
    else:
        ob.data.materials.append(mat)


def _paint(objects, fn):
    """Run fn(material) once per material of the selected objects (created / copied as needed).
    fn returns True when it had to unplug a texture."""
    report = {"objects": [], "created": [], "copied": [], "edited": [], "unplugged": []}
    selection = set(objects)
    done = set()  # materials already painted in this press (shared by several selected objects)
    copies = {}
    for ob in objects:
        if not _can_hold_materials(ob):
            continue
        count = max(1, len(ob.material_slots))
        for index in range(count):
            mat, how = _material_for_slot(ob, index, count, selection, copies)
            _assign(ob, index, mat)
            if mat.name in done:
                continue
            done.add(mat.name)
            report[how].append(mat.name)
            if fn(mat):
                report["unplugged"].append(mat.name)
        report["objects"].append(ob.name)
    return report


def apply_color(objects, linear_rgb):
    """Paint every object in `objects` with one colour (keeps each material's Look)."""
    return _paint(objects, lambda mat: set_material_color(mat, linear_rgb))


def apply_look(objects, look_name):
    """Give every object in `objects` one Look (keeps each material's colour)."""
    return _paint(objects, lambda mat: set_material_look(mat, look_name))


# ----------------------------------------------------------------------
# Look
# ----------------------------------------------------------------------
def _output_node(tree):
    outputs = [n for n in tree.nodes if n.type == "OUTPUT_MATERIAL"]
    out = next((n for n in outputs if n.is_active_output), outputs[0] if outputs else None)
    if out is None:
        out = tree.nodes.new("ShaderNodeOutputMaterial")
        out.location = (300, 0)
    return out


def _plug_surface(tree, from_socket):
    out = _output_node(tree)
    surface = out.inputs["Surface"]
    if surface.is_linked and surface.links[0].from_socket == from_socket:
        return
    for link in list(surface.links):
        tree.links.remove(link)
    tree.links.new(from_socket, surface)


def _principled_for_look(mat):
    """Principled BSDF to put a Look on. A material built around another shader gets one
    (Base Color taken from that shader's colour) and it becomes the surface: picking a Look
    means "make it this kind of material"; the old nodes stay in the tree (Undo restores)."""
    node = _ensure_principled(mat)
    if node is not None:
        return node
    tree = mat.node_tree
    old = _surface_shader(mat)
    node = tree.nodes.new("ShaderNodeBsdfPrincipled")
    node.location = (old.location.x, old.location.y - 400) if old is not None else (0, 0)
    col = next((s for s in old.inputs if s.name in ("Color", "Base Color") and s.type == "RGBA"), None) if old else None
    if col is not None and not col.is_linked:
        node.inputs["Base Color"].default_value = col.default_value
    return node


def _remove_toon(tree):
    for n in [n for n in tree.nodes if n.name.startswith(TOON_PREFIX)]:
        tree.nodes.remove(n)


def _build_toon(tree, principled):
    """Diffuse -> Shader to RGB -> ColorRamp (constant bands) -> Mix Multiply (colour) -> Emission -> Output."""
    _remove_toon(tree)
    x, y = principled.location.x, principled.location.y + 420

    def add(kind, name, dx):
        n = tree.nodes.new(kind)
        n.name = n.label = TOON_PREFIX + name
        n.location = (x + dx, y)
        return n

    diffuse = add("ShaderNodeBsdfDiffuse", "Shade", 0)
    diffuse.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
    to_rgb = add("ShaderNodeShaderToRGB", "ToRGB", 200)
    ramp = add("ShaderNodeValToRGB", "Bands", 380)
    ramp.color_ramp.interpolation = "CONSTANT"
    els = ramp.color_ramp.elements
    while len(els) > 1:
        els.remove(els[-1])
    els[0].position, els[0].color = TOON_RAMP[0]
    for pos, color in TOON_RAMP[1:]:
        e = els.new(pos)
        e.color = color
    tint = add("ShaderNodeMix", "Tint", 660)
    tint.data_type = "RGBA"
    tint.blend_type = "MULTIPLY"
    _by_id(tint.inputs, "Factor_Float").default_value = 1.0
    base = principled.inputs["Base Color"].default_value
    _by_id(tint.inputs, "B_Color").default_value = (base[0], base[1], base[2], 1.0)  # B = the colour
    emit = add("ShaderNodeEmission", "Emit", 860)
    emit.inputs["Strength"].default_value = 1.0
    tree.links.new(diffuse.outputs[0], to_rgb.inputs[0])
    tree.links.new(to_rgb.outputs["Color"], ramp.inputs["Fac"])
    tree.links.new(ramp.outputs["Color"], _by_id(tint.inputs, "A_Color"))
    tree.links.new(_by_id(tint.outputs, "Result_Color"), emit.inputs["Color"])
    _plug_surface(tree, emit.outputs[0])


def set_material_look(mat, look_name):
    """Write one Look into a material. Base Color (the colour) is left alone."""
    values, settings, toon = look_values(look_name)
    if mat.is_grease_pencil:
        return False  # Grease Pencil strokes have no Principled look: colour only
    node = _principled_for_look(mat)
    tree = mat.node_tree
    for ident, v in values.items():
        sock = node.inputs.get(ident)
        if sock is None or sock.is_linked:
            continue  # a texture / node driving it wins (e.g. a roughness map)
        sock.default_value = v
    if values["Emission Strength"] > 0 and not node.inputs["Emission Color"].is_linked:
        base = node.inputs["Base Color"].default_value
        node.inputs["Emission Color"].default_value = (base[0], base[1], base[2], 1.0)
    if toon:
        _build_toon(tree, node)
    else:
        _remove_toon(tree)
        _plug_surface(tree, node.outputs[0])
    for attr in ("surface_render_method", "use_raytrace_refraction"):
        if hasattr(mat, attr):
            setattr(mat, attr, settings[attr])
    c = mat.diffuse_color
    mat.diffuse_color = (c[0], c[1], c[2], settings["viewport_alpha"])
    mat.metallic = values["Metallic"]  # Solid-mode viewport display
    mat.roughness = values["Roughness"]
    return False


def detect_look(mat, tol=0.015):
    """Which Look a material currently has (None if it matches none). Read from the material
    itself, so Looks set by the other person in a collab room are recognised too."""
    if mat is None or mat.is_grease_pencil:
        return None
    node = _principled(mat)
    if node is None:
        return None
    has_toon = mat.node_tree.nodes.get(TOON_PREFIX + "Tint") is not None
    for name in LOOK_NAMES:
        values, _settings, toon = look_values(name)
        if toon != has_toon:
            continue
        ok = True
        for ident, v in values.items():
            sock = node.inputs.get(ident)
            if sock is None or sock.is_linked:
                continue
            cur = sock.default_value
            if isinstance(v, tuple):
                if any(abs(a - b) > tol for a, b in zip(cur, v)):
                    ok = False
                    break
            elif abs(cur - v) > tol:
                ok = False
                break
        if ok:
            return name
    return None


def material_color(mat):
    """Linear RGB the material shows as its colour (Principled Base Color, else viewport colour)."""
    if mat is None:
        return None
    if mat.is_grease_pencil and mat.grease_pencil is not None:
        return tuple(mat.grease_pencil.color[:3])
    node = _principled(mat)
    if node is not None and not node.inputs["Base Color"].is_linked:
        return tuple(node.inputs["Base Color"].default_value[:3])
    return tuple(mat.diffuse_color[:3])


def describe(report):
    n = len(report["objects"])
    if not n:
        return "Select an object first"
    parts = [f"{n} object{'s' if n > 1 else ''}"]
    if report["created"]:
        parts.append(f"{len(report['created'])} new")
    if report["copied"]:
        parts.append(f"{len(report['copied'])} copied")
    if report["unplugged"]:
        parts.append("texture unplugged")
    return ", ".join(parts)
