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

The chosen colour is also remembered on the material ("fbvr_color", linear RGB) so the
upcoming Look presets (Clay / Glossy / Emission ...) can re-derive colour-dependent values
without the two ever being multiplied into 32 x 8 materials.

Collaboration: nothing here talks to FreebirdCollabo. Its material sync already notices
new materials, changed Principled values, removed links and slot changes, so a colour
applied in VR reaches the other people in the room through the existing sync.
"""

import warnings

import bpy

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


def apply_color(objects, linear_rgb):
    """Paint every object in `objects` with one colour. Returns a small report dict."""
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
            if set_material_color(mat, linear_rgb):
                report["unplugged"].append(mat.name)
        report["objects"].append(ob.name)
    return report


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
