# SPDX-License-Identifier: GPL-2.0-or-later
"""
Object-data wire format: serialize / apply the *contents* of an object's
datablock (geometry, curve splines, text, light, camera, empty display).
Adapted from the Machida patch; changes vs. that patch:
  * apply() updates the existing datablock IN PLACE (materials, modifiers,
    custom props and the datablock name survive) instead of swapping it
  * quick_digest() is a cheap C-speed hash (foreach_get) used to decide
    whether a full serialize is needed at all
  * meshes carry per-face material_index
All functions run on Blender's main thread.
"""

import array
import hashlib
import json
import os
import tempfile

import bpy

MAX_MESH_VERTS = 100_000  # live (Edit Mode) re-sync limit
MAX_MESH_VERTS_ADD = 300_000  # one-shot obj_add limit (GLB imports are often big)
MAX_GP_POINTS = 100_000  # live Grease Pencil re-sync limit
MAX_GP_POINTS_ADD = 300_000  # one-shot obj_add limit
SUPPORTED = {"MESH", "CURVE", "FONT", "LIGHT", "CAMERA", "EMPTY", "GREASEPENCIL"}
AS_EVALUATED_MESH = {"SURFACE", "META"}  # no Python API to rebuild these; ship their evaluated mesh instead

_GP_CURVE_TYPES = {0: "CATMULL_ROM", 1: "POLY", 2: "BEZIER", 3: "NURBS"}


def _vec(v):
    return [round(float(x), 5) for x in v]


def _digest_bytes(*parts):
    h = hashlib.blake2b(digest_size=16)
    for p in parts:
        h.update(p if isinstance(p, (bytes, bytearray, memoryview)) else str(p).encode())
    return h.hexdigest()


# ----------------------------------------------------------------------
# change detection
# ----------------------------------------------------------------------
def quick_digest(ob):
    """Cheap fingerprint of the object's data. None = unsupported."""
    kind = ob.type
    if kind not in SUPPORTED and kind not in AS_EVALUATED_MESH:
        return None
    if kind == "MESH":
        if ob.mode == "EDIT":
            ob.update_from_editmode()  # copy the bmesh edits into the mesh datablock
        me = ob.data
        nv, nl, np = len(me.vertices), len(me.loops), len(me.polygons)
        if nv > MAX_MESH_VERTS:
            return None
        co = array.array("f", bytes(nv * 12))
        me.vertices.foreach_get("co", co)
        li = array.array("i", bytes(nl * 4))
        me.loops.foreach_get("vertex_index", li)
        lt = array.array("i", bytes(np * 4))
        me.polygons.foreach_get("loop_total", lt)
        mi = array.array("i", bytes(np * 4))
        me.polygons.foreach_get("material_index", mi)
        return _digest_bytes(nv, nl, np, co.tobytes(), li.tobytes(), lt.tobytes(), mi.tobytes())
    if kind == "GREASEPENCIL":
        return _grease_pencil_digest(ob.data)
    payload = serialize(ob)  # other types are tiny; hashing the payload is cheap
    return _digest_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":"))) if payload else None


# ----------------------------------------------------------------------
# serialize
# ----------------------------------------------------------------------
def _mesh_data(me, limit=MAX_MESH_VERTS):
    if len(me.vertices) > limit:
        return None
    data = {
        "v": [_vec(v.co) for v in me.vertices],
        "e": [list(e.vertices) for e in me.edges if e.is_loose],
        "f": [list(p.vertices) for p in me.polygons],
        "mi": [p.material_index for p in me.polygons] if any(p.material_index for p in me.polygons) else [],
    }
    uv = me.uv_layers.active
    if uv is not None and len(me.loops) and len(uv.data) == len(me.loops):  # uv.data is empty while in Edit Mode
        buf = array.array("f", bytes(len(me.loops) * 8))
        uv.data.foreach_get("uv", buf)
        data["uv"] = [round(x, 5) for x in buf]  # per loop, same loop order from_pydata recreates
        data["uvn"] = uv.name
    return data


def _grease_pencil_digest(gp):
    """Fast fingerprint without building the full point-by-point JSON payload."""
    h = hashlib.blake2b(digest_size=16)
    h.update(str(gp.stroke_depth_order).encode())
    total = 0
    for layer in gp.layers:
        h.update(json.dumps(_gp_layer_settings(layer), sort_keys=True, separators=(",", ":")).encode())
        for frame in layer.frames:
            drawing = frame.drawing
            sizes = [len(stroke.points) for stroke in drawing.strokes]
            total += sum(sizes)
            if total > MAX_GP_POINTS:
                return None
            h.update(str((frame.frame_number, frame.keyframe_type, sizes)).encode())
            for stroke in drawing.strokes:
                h.update(json.dumps(_gp_stroke_settings(stroke), sort_keys=True, separators=(",", ":")).encode())
            # Point values dominate payload size. foreach_get keeps this check in C.
            n_points = sum(sizes)
            for name, prop, width, default in (
                ("position", "vector", 3, (0.0, 0.0, 0.0)),
                ("radius", "value", 1, (0.01,)),
                ("opacity", "value", 1, (1.0,)),
                ("rotation", "value", 1, (0.0,)),
                ("vertex_color", "color", 4, (0.0, 0.0, 0.0, 0.0)),
                ("delta_time", "value", 1, (0.0,)),
                ("handle_left", "vector", 3, (0.0, 0.0, 0.0)),
                ("handle_right", "vector", 3, (0.0, 0.0, 0.0)),
            ):
                attr = drawing.attributes.get(name)
                if attr is not None:
                    values = array.array("f", bytes(len(attr.data) * width * 4))
                    attr.data.foreach_get(prop, values)
                else:
                    values = array.array("f", default * n_points)
                h.update(name.encode())
                h.update(values.tobytes())
    return h.hexdigest()


def _gp_layer_settings(layer):
    return {
        "name": layer.name,
        "hide": layer.hide,
        "lock": layer.lock,
        "opacity": round(float(layer.opacity), 5),
        "blend_mode": layer.blend_mode,
        "tint_color": _vec(layer.tint_color),
        "tint_factor": round(float(layer.tint_factor), 5),
        "use_lights": layer.use_lights,
        "use_onion_skinning": layer.use_onion_skinning,
        "translation": _vec(layer.translation),
        "rotation": _vec(layer.rotation),
        "scale": _vec(layer.scale),
    }


def _gp_stroke_settings(stroke):
    return {
        "curve_type": int(stroke.curve_type),
        "cyclic": stroke.cyclic,
        "material_index": stroke.material_index,
        "softness": round(float(stroke.softness), 5),
        "aspect_ratio": round(float(stroke.aspect_ratio), 5),
        "fill_color": [round(float(x), 5) for x in stroke.fill_color],
        "fill_opacity": round(float(stroke.fill_opacity), 5),
        "fill_id": getattr(stroke, "fill_id", 0),
        "hide_stroke": getattr(stroke, "hide_stroke", False),
    }


def _gp_point_data(point):
    item = {
        "co": _vec(point.position),
        "radius": round(float(point.radius), 5),
        "opacity": round(float(point.opacity), 5),
        "rotation": round(float(point.rotation), 5),
        "color": [round(float(x), 5) for x in point.vertex_color],
        "time": round(float(point.delta_time), 5),
    }
    if point.handle_left is not None and point.handle_right is not None:
        item["hl"] = _vec(point.handle_left.position)
        item["hr"] = _vec(point.handle_right.position)
    return item


def _grease_pencil_data(gp, limit=MAX_GP_POINTS):
    layers = []
    total = 0
    for layer in gp.layers:
        layer_item = _gp_layer_settings(layer)
        layer_item["frames"] = []
        for frame in layer.frames:
            strokes = []
            for stroke in frame.drawing.strokes:
                total += len(stroke.points)
                if total > limit:
                    return None
                item = _gp_stroke_settings(stroke)
                item["points"] = [_gp_point_data(point) for point in stroke.points]
                strokes.append(item)
            layer_item["frames"].append({
                "number": frame.frame_number,
                "keyframe_type": frame.keyframe_type,
                "strokes": strokes,
            })
        layers.append(layer_item)
    active = gp.layers.active.name if gp.layers.active else None
    return {"layers": layers, "active": active, "stroke_depth_order": gp.stroke_depth_order}


def serialize(ob, limit=MAX_MESH_VERTS):
    """Return {"type", "data"} or None when unsupported / too large."""
    kind = ob.type
    if kind == "MESH":
        if ob.mode == "EDIT":
            ob.update_from_editmode()
        data = _mesh_data(ob.data, limit)
        if data is None:
            return None
    elif kind in AS_EVALUATED_MESH:
        dg = bpy.context.evaluated_depsgraph_get()
        ev = ob.evaluated_get(dg)
        me = ev.to_mesh()
        try:
            data = _mesh_data(me, limit) if me else None
        finally:
            ev.to_mesh_clear()
        if data is None:
            return None
        return {"type": "MESH", "data": data, "src": kind}  # receiver gets a plain mesh copy
    elif kind == "CURVE":
        cu = ob.data
        splines = []
        for sp in cu.splines:
            item = {"type": sp.type, "cyclic": sp.use_cyclic_u, "order_u": sp.order_u, "resolution_u": sp.resolution_u}
            if sp.type == "BEZIER":
                item["points"] = [
                    {"co": _vec(p.co), "l": _vec(p.handle_left), "r": _vec(p.handle_right),
                     "lt": p.handle_left_type, "rt": p.handle_right_type}
                    for p in sp.bezier_points
                ]
            else:
                item["points"] = [{"co": _vec(p.co), "w": round(p.weight, 5)} for p in sp.points]
            splines.append(item)
        data = {"dimensions": cu.dimensions, "bevel_depth": cu.bevel_depth, "extrude": cu.extrude, "splines": splines}
    elif kind == "FONT":
        cu = ob.data
        data = {"body": cu.body, "size": cu.size, "extrude": cu.extrude, "bevel_depth": cu.bevel_depth, "align_x": cu.align_x}
    elif kind == "LIGHT":
        lamp = ob.data
        data = {"type": lamp.type, "energy": lamp.energy, "color": _vec(lamp.color)}
        if lamp.type in ("POINT", "SPOT", "AREA"):
            data["shadow_soft_size"] = lamp.shadow_soft_size
        if lamp.type == "SPOT":
            data["spot_size"] = lamp.spot_size
            data["spot_blend"] = lamp.spot_blend
    elif kind == "CAMERA":
        cam = ob.data
        data = {"type": cam.type, "lens": cam.lens, "ortho_scale": cam.ortho_scale, "clip_start": cam.clip_start, "clip_end": cam.clip_end}
    elif kind == "EMPTY":
        data = {"display_type": ob.empty_display_type, "display_size": ob.empty_display_size}
    elif kind == "GREASEPENCIL":
        gp_limit = MAX_GP_POINTS_ADD if limit == MAX_MESH_VERTS_ADD else MAX_GP_POINTS
        data = _grease_pencil_data(ob.data, gp_limit)
        if data is None:
            return None
    else:
        return None
    return {"type": kind, "data": data}


# ----------------------------------------------------------------------
# apply
# ----------------------------------------------------------------------
def new_object(name, payload):
    """Create a new object (with a fresh datablock) for an obj_add payload."""
    kind = payload["type"]
    if kind == "MESH":
        data = bpy.data.meshes.new(name)
    elif kind == "CURVE":
        data = bpy.data.curves.new(name, type="CURVE")
    elif kind == "FONT":
        data = bpy.data.curves.new(name, type="FONT")
    elif kind == "LIGHT":
        data = bpy.data.lights.new(name, type=payload["data"]["type"])
    elif kind == "CAMERA":
        data = bpy.data.cameras.new(name)
    elif kind == "EMPTY":
        data = None
    elif kind == "GREASEPENCIL":
        data = bpy.data.grease_pencils.new(name)
    else:
        raise ValueError(f"unsupported object type: {kind}")
    ob = bpy.data.objects.new(name, data)
    apply(ob, payload)
    return ob


def apply(ob, payload):
    """Write payload into the object's existing datablock in place. Returns False if type mismatch."""
    kind, data = payload["type"], payload["data"]
    if ob.type != kind:
        return False
    if kind == "MESH":
        if ob.mode == "EDIT":
            return False  # caller retries later; can't rewrite a mesh that is being edited here
        me = ob.data
        keep_uv = None
        if "uv" not in data and me.uv_layers.active is not None and len(me.uv_layers.active.data) == len(me.loops):
            keep_uv = array.array("f", bytes(len(me.loops) * 8))  # payload came from Edit Mode: keep our UVs
            me.uv_layers.active.data.foreach_get("uv", keep_uv)
            keep_uv = (me.uv_layers.active.name, keep_uv)
        me.clear_geometry()
        me.from_pydata(data["v"], data.get("e", []), data["f"])
        if keep_uv and len(keep_uv[1]) == len(me.loops) * 2:
            layer = me.uv_layers.new(name=keep_uv[0])
            layer.data.foreach_set("uv", keep_uv[1])
            me.uv_layers.active = layer
        mi = data.get("mi")
        if mi and len(mi) == len(me.polygons):
            me.polygons.foreach_set("material_index", mi)
        uv = data.get("uv")
        if uv and len(uv) == len(me.loops) * 2:
            layer = me.uv_layers.get(data.get("uvn") or "UVMap") or me.uv_layers.new(name=data.get("uvn") or "UVMap")
            layer.data.foreach_set("uv", uv)
            me.uv_layers.active = layer
        me.update()
    elif kind == "CURVE":
        cu = ob.data
        cu.splines.clear()
        cu.dimensions = data["dimensions"]
        cu.bevel_depth = data["bevel_depth"]
        cu.extrude = data["extrude"]
        for item in data["splines"]:
            sp = cu.splines.new(item["type"])
            pts = item["points"]
            if item["type"] == "BEZIER":
                if len(pts) > 1:
                    sp.bezier_points.add(len(pts) - 1)
                for p, v in zip(sp.bezier_points, pts):
                    p.handle_left_type, p.handle_right_type = v["lt"], v["rt"]
                    p.co, p.handle_left, p.handle_right = v["co"], v["l"], v["r"]
            else:
                if len(pts) > 1:
                    sp.points.add(len(pts) - 1)
                for p, v in zip(sp.points, pts):
                    p.co, p.weight = v["co"], v["w"]
                sp.order_u = min(item["order_u"], max(len(pts), 1))
            sp.use_cyclic_u = item["cyclic"]
            sp.resolution_u = item["resolution_u"]
    elif kind == "FONT":
        for k, v in data.items():
            setattr(ob.data, k, v)
    elif kind == "LIGHT":
        lamp = ob.data
        if lamp.type != data["type"]:
            lamp.type = data["type"]
        for k, v in data.items():
            if k != "type":
                setattr(lamp, k, v)
    elif kind == "CAMERA":
        for k, v in data.items():
            setattr(ob.data, k, v)
    elif kind == "EMPTY":
        ob.empty_display_type = data["display_type"]
        ob.empty_display_size = data["display_size"]
    elif kind == "GREASEPENCIL":
        _apply_grease_pencil(ob.data, data)
    return True


def _apply_grease_pencil(gp, data):
    """Rebuild drawings in the existing datablock so slots and object settings survive."""
    for layer in list(gp.layers):
        gp.layers.remove(layer)
    created = {}
    for layer_item in data.get("layers", []):
        layer = gp.layers.new(layer_item["name"], set_active=False)
        created[layer.name] = layer
        for key in ("hide", "lock", "opacity", "blend_mode", "tint_color", "tint_factor",
                    "use_lights", "use_onion_skinning", "translation", "rotation", "scale"):
            if key in layer_item:
                setattr(layer, key, layer_item[key])
        for frame_item in layer_item.get("frames", []):
            frame = layer.frames.new(frame_item["number"])
            frame.keyframe_type = frame_item.get("keyframe_type", "KEYFRAME")
            drawing = frame.drawing
            strokes = frame_item.get("strokes", [])
            if strokes:
                drawing.add_strokes([len(item.get("points", [])) for item in strokes])
            for index, item in enumerate(strokes):
                curve_type = _GP_CURVE_TYPES.get(int(item.get("curve_type", 0)), "POLY")
                if curve_type != "POLY":
                    drawing.set_types(type=curve_type, indices=[index])
                stroke = drawing.strokes[index]
                for key in ("cyclic", "material_index", "softness", "aspect_ratio", "fill_color",
                            "fill_opacity", "fill_id", "hide_stroke"):
                    if key in item and hasattr(stroke, key):
                        setattr(stroke, key, item[key])
                for point, point_item in zip(stroke.points, item.get("points", [])):
                    point.position = point_item["co"]
                    point.radius = point_item.get("radius", 0.01)
                    point.opacity = point_item.get("opacity", 1.0)
                    point.rotation = point_item.get("rotation", 0.0)
                    point.vertex_color = point_item.get("color", (0.0, 0.0, 0.0, 0.0))
                    point.delta_time = point_item.get("time", 0.0)
                    if point.handle_left is not None and "hl" in point_item:
                        point.handle_left.position = point_item["hl"]
                    if point.handle_right is not None and "hr" in point_item:
                        point.handle_right.position = point_item["hr"]
    active = created.get(data.get("active"))
    if active is not None:
        gp.layers.active = active
    if "stroke_depth_order" in data:
        gp.stroke_depth_order = data["stroke_depth_order"]
    gp.update_tag()


def payload_bytes(payload):
    return len(json.dumps(payload, separators=(",", ":")))


# ----------------------------------------------------------------------
# materials (MVP: slot name + Base Color value + Base Color image texture; no other PBR inputs)
# ----------------------------------------------------------------------
MAX_IMAGE_BYTES = 24 * 1024 * 1024


def _principled(mat):
    if mat.use_nodes and mat.node_tree:
        for n in mat.node_tree.nodes:
            if n.type == "BSDF_PRINCIPLED":
                return n
    return None


def _base_color(mat):
    try:
        n = _principled(mat)
        if n:
            return [round(float(c), 4) for c in n.inputs["Base Color"].default_value]
    except Exception:
        pass
    return [round(float(c), 4) for c in mat.diffuse_color]


def _base_color_image(mat):
    """Image node feeding Base Color, searching breadth-first through intermediate nodes
    (Mix with a vertex colour, gamma, separate/combine, node groups...), or None."""
    n = _principled(mat)
    if n is None:
        return None
    sock = n.inputs["Base Color"]
    if not sock.is_linked:
        return None
    queue = [sock.links[0].from_node]
    seen = set()
    while queue:
        node = queue.pop(0)
        if node.name in seen:
            continue
        seen.add(node.name)
        if node.type == "TEX_IMAGE" and node.image is not None:
            return node.image
        if len(seen) > 12:
            break
        for inp in node.inputs:
            for link in inp.links:
                queue.append(link.from_node)
    return None


def image_bytes(img):
    """(raw file bytes, ext) of an image: packed data or the file on disk. None when unavailable."""
    if img is None:
        return None
    try:
        if img.packed_file is not None:
            data = bytes(img.packed_file.data)
        else:
            path = bpy.path.abspath(img.filepath_raw or img.filepath)
            if not path or not os.path.isfile(path):
                return None
            with open(path, "rb") as f:
                data = f.read()
    except Exception:
        return None
    if not data or len(data) > MAX_IMAGE_BYTES:
        return None
    ext = (os.path.splitext(img.filepath_raw or img.name)[1] or "").lower().lstrip(".")
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        ext = "png"
    elif data[:3] == b"\xff\xd8\xff":
        ext = "jpg"
    return data, (ext or "png")


def image_id(data):
    return hashlib.sha1(data).hexdigest()


def serialize_materials(ob):
    """Per slot: {"n": name, "c": [rgba], "tex": {"id", "name", "ext"}|absent}. Images themselves travel separately."""
    if ob.data is None or not hasattr(ob.data, "materials"):
        return []
    out = []
    for slot in ob.material_slots:
        m = slot.material
        if m is None:
            out.append(None)
            continue
        item = {"n": m.name, "c": _base_color(m)}
        if m.is_grease_pencil and m.grease_pencil is not None:
            gp = m.grease_pencil
            item["gp"] = {
                "color": [round(float(x), 5) for x in gp.color],
                "fill_color": [round(float(x), 5) for x in gp.fill_color],
                "mode": gp.mode,
                "stroke_style": gp.stroke_style,
                "fill_style": gp.fill_style,
                "alignment_mode": gp.alignment_mode,
                "use_overlap_strokes": gp.use_overlap_strokes,
                "use_stroke_holdout": gp.use_stroke_holdout,
                "use_fill_holdout": gp.use_fill_holdout,
            }
        img = _base_color_image(m)
        if img is not None:
            got = image_bytes(img)
            if got:
                data, ext = got
                item["tex"] = {"id": image_id(data), "name": img.name, "ext": ext}
        out.append(item)
    return out


def images_for_materials(mats):
    """Yield (id, data, name, ext) for every texture referenced by a serialized material list."""
    seen = set()
    for item in mats or []:
        tex = (item or {}).get("tex")
        if not tex or tex["id"] in seen:
            continue
        seen.add(tex["id"])
        img = bpy.data.images.get(tex["name"])
        got = image_bytes(img) if img else None
        if got and image_id(got[0]) == tex["id"]:
            yield tex["id"], got[0], tex["name"], got[1]


_hash_index = {"n": -1, "map": {}}


def find_image(img_id):
    """Image already here? Tagged ones first, then (lazily) by content hash of packed images
    (covers images that arrived inside the .blend snapshot or a local import of the same asset)."""
    for img in bpy.data.images:
        if img.get("collab_id") == img_id:
            return img
    if _hash_index["n"] != len(bpy.data.images):
        _hash_index["n"] = len(bpy.data.images)
        _hash_index["map"] = {}
        for img in bpy.data.images:
            if img.packed_file is not None and "collab_id" not in img:
                try:
                    _hash_index["map"][image_id(bytes(img.packed_file.data))] = img.name
                except Exception:
                    pass
    name = _hash_index["map"].get(img_id)
    img = bpy.data.images.get(name) if name else None
    if img is not None:
        img["collab_id"] = img_id
    return img


def store_image(img_id, data, name, ext):
    """Create (or reuse) a packed image datablock for received bytes. Returns the image."""
    img = find_image(img_id)
    if img is not None:
        return img
    path = os.path.join(tempfile.gettempdir(), f"collab_img_{img_id[:12]}.{ext}")
    with open(path, "wb") as f:
        f.write(data)
    img = bpy.data.images.load(path, check_existing=False)
    img.name = name
    try:
        img.pack()
    except Exception:
        pass
    img["collab_id"] = img_id
    return img


def apply_materials(ob, mats):
    """Returns the list of texture ids that are referenced but not available here (yet)."""
    missing = []
    if not mats or ob.data is None or not hasattr(ob.data, "materials"):
        return missing
    me = ob.data
    while len(me.materials) < len(mats):
        me.materials.append(None)
    for i, item in enumerate(mats):
        if item is None:
            continue
        mat = bpy.data.materials.get(item["n"])
        if mat is None:
            mat = bpy.data.materials.new(item["n"])
        gp_data = item.get("gp")
        if gp_data and not mat.is_grease_pencil:
            bpy.data.materials.create_gpencil_data(mat)
        if gp_data and mat.grease_pencil is not None:
            style = mat.grease_pencil
            for key, value in gp_data.items():
                if hasattr(style, key):
                    setattr(style, key, value)
        col = item.get("c")
        node = _principled(mat)
        if col and len(col) == 4:
            mat.diffuse_color = col
            if node:
                node.inputs["Base Color"].default_value = col
        tex = item.get("tex")
        if tex and node is not None:
            img = find_image(tex["id"])
            if img is None:
                missing.append(tex["id"])
            elif _base_color_image(mat) is not img:
                tree = mat.node_tree
                texnode = tree.nodes.new("ShaderNodeTexImage")
                texnode.image = img
                texnode.location = (node.location.x - 300, node.location.y)
                for l in list(node.inputs["Base Color"].links):
                    tree.links.remove(l)
                tree.links.new(texnode.outputs["Color"], node.inputs["Base Color"])
        me.materials[i] = mat
    return missing
