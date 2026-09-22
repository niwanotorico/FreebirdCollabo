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
MAX_BONES = 4000  # armature structure sync limit (a Rigify rig is ~1000)
SUPPORTED = {"MESH", "CURVE", "FONT", "LIGHT", "CAMERA", "EMPTY", "GREASEPENCIL", "ARMATURE"}
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
        # skinning (Issue #6): vertex group names + armature modifiers (cheap) and the weights (a Python loop over
        # the vertices: ~2 ms per 1000 vertices; only meshes flagged dirty / being painted get here)
        skin = json.dumps(skin_meta(ob), separators=(",", ":")) if (ob.vertex_groups or ob.modifiers) else ""
        weights = json.dumps(_weights(ob, me), separators=(",", ":")) if ob.vertex_groups else ""
        return _digest_bytes(nv, nl, np, co.tobytes(), li.tobytes(), lt.tobytes(), mi.tobytes(), skin, weights)
    if kind == "GREASEPENCIL":
        return _grease_pencil_digest(ob.data)
    if kind == "ARMATURE":
        bones = armature_bones(ob)
        return _digest_bytes(json.dumps(bones, separators=(",", ":"))) if bones is not None else None
    payload = serialize(ob)  # other types are tiny; hashing the payload is cheap
    return _digest_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":"))) if payload else None


# ----------------------------------------------------------------------
# serialize
# ----------------------------------------------------------------------
def _mesh_data(me, limit=MAX_MESH_VERTS, ob=None):
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
    if ob is not None and ob.type == "MESH":  # skinning (Issue #6): vertex groups + weights + armature modifiers
        meta = skin_meta(ob)
        data["vg"] = meta["vg"]
        data["w"] = _weights(ob, me)
        data["mods"] = meta["mods"]
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
        data = _mesh_data(ob.data, limit, ob)
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
    elif kind == "ARMATURE":
        bones = armature_bones(ob)
        if bones is None:
            return None
        data = {"bones": bones}
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
    elif kind == "ARMATURE":
        data = bpy.data.armatures.new(name)
    else:
        raise ValueError(f"unsupported object type: {kind}")
    ob = bpy.data.objects.new(name, data)
    if kind != "ARMATURE":  # bones need Edit Mode, which needs the object in the scene: the caller applies after linking
        apply(ob, payload)
    return ob


def apply(ob, payload):
    """Write payload into the object's existing datablock in place. Returns False if type mismatch."""
    kind, data = payload["type"], payload["data"]
    if ob.type != kind:
        return False
    if kind == "MESH":
        if ob.mode != "OBJECT":
            return False  # caller retries later; can't rewrite a mesh that is being edited / painted / sculpted here
        me = ob.data
        keep_uv = None
        if "uv" not in data and me.uv_layers.active is not None and len(me.uv_layers.active.data) == len(me.loops):
            keep_uv = array.array("f", bytes(len(me.loops) * 8))  # payload came from Edit Mode: keep our UVs
            me.uv_layers.active.data.foreach_get("uv", keep_uv)
            keep_uv = (me.uv_layers.active.name, keep_uv)
        keep_w = None
        if "vg" not in data and ob.vertex_groups:  # pre-0.10 peer: clear_geometry drops the weights, so keep ours
            keep_w = (len(me.vertices), [g.name for g in ob.vertex_groups], _weights(ob, me))
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
        if "vg" in data:
            apply_vertex_groups(ob, data["vg"], data.get("w") or [])
        elif keep_w and keep_w[0] == len(me.vertices):
            apply_vertex_groups(ob, keep_w[1], keep_w[2])
        if "mods" in data:
            apply_armature_modifiers(ob, data["mods"])
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
    elif kind == "ARMATURE":
        if ob.mode == "EDIT" or not armature_editable():
            return False  # caller retries later: the bones are being edited here / another object is in Edit Mode
        apply_armature(ob, data.get("bones") or [])
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
# skinning (Issue #6). Rides inside the MESH payload (obj_add / obj_data), because a geometry rebuild
# (clear_geometry + from_pydata) drops the deform weights anyway:
#   vg    vertex group names in index order            (identity = NAME, like bones: a rename = delete + create)
#   w     one flat list per group: [vertex index, weight, vertex index, weight, ...] (weights rounded to 4 places)
#   mods  Armature modifiers only: {n: name, i: stack index, ob: armature object name | null, vg: vertex_group,
#         uvg / env / pv / inv / mm / sv: use_vertex_groups / use_bone_envelopes / use_deform_preserve_volume /
#         invert_vertex_group / use_multi_modifier / show_viewport}
# Other modifier types are not synced (non-goal); their stack position is respected when we can.
# ----------------------------------------------------------------------
_ARM_MOD_KEYS = (("vg", "vertex_group"), ("uvg", "use_vertex_groups"), ("env", "use_bone_envelopes"),
                 ("pv", "use_deform_preserve_volume"), ("inv", "invert_vertex_group"),
                 ("mm", "use_multi_modifier"), ("sv", "show_viewport"))


def skin_meta(ob):
    """Vertex group names + Armature modifier settings of a mesh object (cheap: no weights). None for other types."""
    if ob.type != "MESH":
        return None
    mods = []
    for i, m in enumerate(ob.modifiers):
        if m.type != "ARMATURE":
            continue
        item = {"n": m.name, "i": i, "ob": m.object.name if m.object is not None else None}
        for key, attr in _ARM_MOD_KEYS:
            item[key] = getattr(m, attr)
        mods.append(item)
    return {"vg": [g.name for g in ob.vertex_groups], "mods": mods}


def skin_meta_digest(ob):
    meta = skin_meta(ob)
    return _digest_bytes(json.dumps(meta, separators=(",", ":"))) if meta is not None else None


def _weights(ob, me):
    """Per vertex group (index order): flat [vertex index, weight, ...]."""
    n = len(ob.vertex_groups)
    if not n:
        return []
    out = [[] for _ in range(n)]
    for v in me.vertices:
        vi = v.index
        for g in v.groups:
            gi = g.group
            if 0 <= gi < n:
                out[gi].append(vi)
                out[gi].append(round(g.weight, 4))
    return out


def apply_vertex_groups(ob, names, weights):
    """Make ob.vertex_groups exactly `names` (by name) and assign `weights` (see _weights). The mesh has just been
    rebuilt, so there are no stale assignments; a group that keeps its name keeps its index where Blender allows."""
    vgs = ob.vertex_groups
    wanted = set(names)
    for g in list(vgs):
        if g.name not in wanted:
            vgs.remove(g)
    for name in names:
        if name not in vgs:
            g = vgs.new(name=name)
            if g.name != name:
                g.name = name
    nv = len(ob.data.vertices)
    for name, flat in zip(names, weights):
        vg = vgs.get(name)
        if vg is None or not flat:
            continue
        by = {}  # weight value -> [vertex indices]: VertexGroup.add takes one weight per call
        for i in range(0, len(flat) - 1, 2):
            vi = flat[i]
            if 0 <= vi < nv:
                by.setdefault(flat[i + 1], []).append(vi)
        for w, idx in by.items():
            vg.add(idx, w, "REPLACE")


def apply_armature_modifiers(ob, mods):
    """Make the object's Armature modifiers match `mods` (other modifier types are left alone).
    Returns the names of referenced armature objects that are not here (yet): the caller retries when they arrive."""
    wanted = {it["n"] for it in mods}
    for m in list(ob.modifiers):
        if m.type == "ARMATURE" and m.name not in wanted:
            ob.modifiers.remove(m)
    missing = []
    for it in mods:
        m = ob.modifiers.get(it["n"])
        if m is not None and m.type != "ARMATURE":  # another type squats on the name: remote naming authority
            ob.modifiers.remove(m)
            m = None
        if m is None:
            m = ob.modifiers.new(it["n"], "ARMATURE")
            if m is None:
                continue
            if m.name != it["n"]:
                m.name = it["n"]
        target_name = it.get("ob")
        if target_name:
            target = bpy.data.objects.get(target_name)
            if target is None or target.type != "ARMATURE":
                missing.append(target_name)
            elif m.object != target:
                m.object = target
        elif m.object is not None:
            m.object = None
        for key, attr in _ARM_MOD_KEYS:
            if key in it:
                _set(m, attr, it[key])
        idx = it.get("i")
        if idx is not None:
            try:
                cur = ob.modifiers.find(m.name)
                if 0 <= idx < len(ob.modifiers) and cur != idx:
                    ob.modifiers.move(cur, idx)
            except Exception:
                pass
    return missing


def missing_modifier_objects(mods):
    """Armature object names referenced by `mods` that are not in this file."""
    out = []
    for it in mods or []:
        name = it.get("ob")
        if name:
            target = bpy.data.objects.get(name)
            if target is None or target.type != "ARMATURE":
                out.append(name)
    return out


def skin_summary(meta):
    """Short human summary of a skin meta dict for the log: '3 groups (Bone, spine, arm.L); Armature -> Rig'."""
    if not meta:
        return "no skin"
    names = meta.get("vg") or []
    parts = [f"{len(names)} group{'s' if len(names) != 1 else ''}" + (f" ({', '.join(names[:6])}{'...' if len(names) > 6 else ''})" if names else "")]
    for it in meta.get("mods") or []:
        parts.append(f"{it['n']} -> {it.get('ob') or 'none'}")
    return "; ".join(parts)


# ----------------------------------------------------------------------
# materials
#   A material travels as one small "state" dict (serialize_material). The same dict is used
#   inline in obj_add["mats"] and standalone in the live "mat" message (Issue #2):
#     n      name                      c    viewport colour (= Base Color value when unlinked)
#     vm/vr  viewport metallic/roughness    rm / bc  surface_render_method / backface culling
#     p      {socket identifier: value}  unlinked Principled BSDF inputs (float / colour / vector)
#     tex    Base Color image texture    tx   {socket identifier: texture} for the other inputs
#     l      inputs driven by something we do not sync (procedural nodes...) -> receiver leaves them alone
#     gp     Grease Pencil style
#   Arbitrary node graphs are NOT synced (non-goal); images themselves travel separately (img / img_need).
# ----------------------------------------------------------------------
MAX_IMAGE_BYTES = 24 * 1024 * 1024
TEX_INPUTS = ("Base Color", "Metallic", "Roughness", "Alpha", "Normal", "Emission Color")
_GP_STYLE_KEYS = ("color", "fill_color", "mode", "stroke_style", "fill_style", "alignment_mode",
                  "use_overlap_strokes", "use_stroke_holdout", "use_fill_holdout")
_MAT_SETTINGS = (("rm", "surface_render_method"), ("bc", "use_backface_culling"))


def _principled(mat):
    tree = getattr(mat, "node_tree", None)  # (Material.use_nodes is deprecated in 5.x; node_tree is None when off)
    if tree is not None:
        for n in tree.nodes:
            if n.type == "BSDF_PRINCIPLED":
                return n
    return None


def _ensure_principled(mat):
    """Principled BSDF of a material we are about to write to; builds the default tree if there is none."""
    node = _principled(mat)
    if node is not None or mat.is_grease_pencil:
        return node
    if mat.node_tree is None:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                mat.use_nodes = True
            except Exception:
                return None
        node = _principled(mat)
    if node is None and mat.node_tree is not None and not len(mat.node_tree.nodes):
        tree = mat.node_tree
        node = tree.nodes.new("ShaderNodeBsdfPrincipled")
        out = tree.nodes.new("ShaderNodeOutputMaterial")
        out.location = (300, 0)
        tree.links.new(node.outputs[0], out.inputs[0])
    return node  # a material built around another shader stays as it is


def _base_color(mat):
    try:
        n = _principled(mat)
        if n:
            return [round(float(c), 4) for c in n.inputs["Base Color"].default_value]
    except Exception:
        pass
    return [round(float(c), 4) for c in mat.diffuse_color]


def _image_node(sock):
    """Image Texture node feeding a socket, searching breadth-first through intermediate nodes
    (Mix with a vertex colour, gamma, normal map, separate/combine, node groups...), or None."""
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
            return node
        if len(seen) > 12:
            break
        for inp in node.inputs:
            for link in inp.links:
                queue.append(link.from_node)
    return None


def _base_color_image(mat):
    n = _principled(mat)
    node = _image_node(n.inputs["Base Color"]) if n is not None else None
    return node.image if node is not None else None


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


_tex_cache = {}  # image name -> (signature, tex dict | None): materials are re-digested often, images are big


def _image_signature(img):
    try:
        if img.packed_file is not None:
            return ("packed", img.packed_file.size)
        path = bpy.path.abspath(img.filepath_raw or img.filepath)
        st = os.stat(path)
        return (path, st.st_mtime_ns, st.st_size)
    except Exception:
        return None


def _tex_ref(img):
    """{"id", "name", "ext", "cs"} for an image whose bytes we can ship, else None."""
    sig = _image_signature(img)
    if sig is None:
        return None
    hit = _tex_cache.get(img.name)
    if hit is None or hit[0] != sig:
        got = image_bytes(img)
        hit = (sig, {"id": image_id(got[0]), "ext": got[1]} if got else None)
        _tex_cache[img.name] = hit
        if len(_tex_cache) > 512:
            _tex_cache.pop(next(iter(_tex_cache)))
    if hit[1] is None:
        return None
    ref = dict(hit[1], name=img.name)
    try:
        ref["cs"] = img.colorspace_settings.name
    except Exception:
        pass
    return ref


def _socket_value(sock):
    if sock.type == "VALUE":
        return round(float(sock.default_value), 5)
    if sock.type in ("RGBA", "VECTOR"):
        return [round(float(x), 5) for x in sock.default_value]
    return None


def serialize_material(mat):
    """Wire state of one material (see the section header)."""
    item = {"n": mat.name, "c": _base_color(mat)}
    try:
        item["vm"] = round(float(mat.metallic), 5)
        item["vr"] = round(float(mat.roughness), 5)
        for key, attr in _MAT_SETTINGS:
            if hasattr(mat, attr):
                item[key] = getattr(mat, attr)
    except Exception:
        pass
    if mat.is_grease_pencil and mat.grease_pencil is not None:
        gp = mat.grease_pencil
        item["gp"] = {k: ([round(float(x), 5) for x in getattr(gp, k)] if k.endswith("color") else getattr(gp, k))
                      for k in _GP_STYLE_KEYS if hasattr(gp, k)}
    node = _principled(mat)
    if node is None:
        return item
    values, textures, foreign = {}, {}, []
    for sock in node.inputs:
        if sock.is_linked:
            ref = None
            if sock.identifier in TEX_INPUTS:
                texnode = _image_node(sock)
                ref = _tex_ref(texnode.image) if texnode is not None else None
                if ref is not None:
                    link = sock.links[0]
                    if link.from_node == texnode:
                        ref["out"] = link.from_socket.identifier  # Color or Alpha
                    textures[sock.identifier] = ref
            if ref is None:
                foreign.append(sock.identifier)
        elif not sock.hide_value and hasattr(sock, "default_value"):
            v = _socket_value(sock)
            if v is not None:
                values[sock.identifier] = v
    item["p"] = values
    base = textures.pop("Base Color", None)
    if base is not None:
        item["tex"] = base
    if textures:
        item["tx"] = textures
    if foreign:
        item["l"] = sorted(foreign)
    return item


def state_digest(item):
    return _digest_bytes(json.dumps(item, sort_keys=True, separators=(",", ":")))


def material_digest(mat):
    return state_digest(serialize_material(mat))


def material_delta(prev, item):
    """What changed from prev to item, in the same wire format (apply_material only touches keys it is given).
    A removed texture shows up as a new plain value in "p", which is what makes the receiver unlink it."""
    out = {"n": item["n"]}
    for key, value in item.items():
        if key in ("p", "tx"):
            old = prev.get(key) or {}
            sub = {k: v for k, v in value.items() if old.get(k) != v}
            if sub:
                out[key] = sub
        elif key != "l" and prev.get(key) != value:
            out[key] = value
    return out


def material_uid(mat):
    """Identity that survives a rename (and undo), so a rename is not mistaken for delete + create."""
    return getattr(mat, "session_uid", None) or mat.as_pointer()


def material_textures(item):
    """All texture refs of one serialized material."""
    if not item:
        return []
    out = [item["tex"]] if item.get("tex") else []
    out.extend((item.get("tx") or {}).values())
    return out


def serialize_materials(ob):
    """Per slot: serialize_material() or None for an empty slot."""
    if ob.data is None or not hasattr(ob.data, "materials"):
        return []
    return [serialize_material(slot.material) if slot.material is not None else None for slot in ob.material_slots]


def serialize_slots(ob):
    """[[material name | None, "DATA" | "OBJECT"], ...] or None when the object cannot have materials."""
    if ob.data is None or not hasattr(ob.data, "materials"):
        return None
    return [[slot.material.name if slot.material is not None else None, slot.link] for slot in ob.material_slots]


def images_for_materials(mats):
    """Yield (id, data, name, ext) for every texture referenced by a serialized material list."""
    seen = set()
    for item in mats or []:
        for tex in material_textures(item):
            if tex["id"] in seen:
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


def _differs(a, b):
    try:
        if isinstance(b, (list, tuple)):
            return len(a) != len(b) or any(abs(float(x) - float(y)) > 1e-6 for x, y in zip(a, b))
        if isinstance(b, float):
            return abs(float(a) - b) > 1e-6
    except TypeError:
        return True
    return a != b


def _set(owner, attr, value):
    """setattr only on a real change: every write tags the depsgraph and would wake the change detector."""
    try:
        if _differs(getattr(owner, attr), value):
            setattr(owner, attr, value)
    except Exception:
        pass


def _apply_texture(mat, node, sock, tex):
    """Make `sock` read image `tex`. Returns False when the image is not here yet."""
    img = find_image(tex["id"])
    if img is None:
        return False
    cs = tex.get("cs")
    if cs:
        try:
            if img.colorspace_settings.name != cs:
                img.colorspace_settings.name = cs
        except Exception:
            pass
    current = _image_node(sock)
    if current is not None:  # keep the local graph (mapping, mix, normal map...) and swap the image only
        if current.image != img:
            current.image = img
        return True
    tree = mat.node_tree
    texnode = next((n for n in tree.nodes if n.type == "TEX_IMAGE" and n.image == img), None)
    if texnode is None:
        texnode = tree.nodes.new("ShaderNodeTexImage")
        texnode.image = img
        row = TEX_INPUTS.index(sock.identifier) if sock.identifier in TEX_INPUTS else 0
        texnode.location = (node.location.x - 600, node.location.y - 280 * row)
    for l in list(sock.links):
        tree.links.remove(l)
    out = texnode.outputs.get(tex.get("out") or "Color") or texnode.outputs["Color"]
    if sock.identifier == "Normal":
        nm = tree.nodes.new("ShaderNodeNormalMap")
        nm.location = (node.location.x - 250, texnode.location.y)
        tree.links.new(out, nm.inputs["Color"])
        tree.links.new(nm.outputs["Normal"], sock)
    else:
        tree.links.new(out, sock)
    return True


def apply_material(item, textures_only=False):
    """Create / update the material named item["n"]. Returns (material, [missing texture ids])."""
    missing = []
    mat = bpy.data.materials.get(item["n"])
    if mat is None:
        mat = bpy.data.materials.new(item["n"])
        if mat.name != item["n"]:
            mat.name = item["n"]
    gp_data = item.get("gp")
    if gp_data and not mat.is_grease_pencil:
        bpy.data.materials.create_gpencil_data(mat)
    node = _principled(mat) if gp_data else _ensure_principled(mat)
    if not textures_only:
        if gp_data and mat.grease_pencil is not None:
            for key, value in gp_data.items():
                if hasattr(mat.grease_pencil, key):
                    _set(mat.grease_pencil, key, value)
        col = item.get("c")
        if col and len(col) == 4:
            _set(mat, "diffuse_color", col)
            if node is not None and "p" not in item and not node.inputs["Base Color"].is_linked:
                _set(node.inputs["Base Color"], "default_value", col)  # pre-0.7 peers send the colour only
        if "vm" in item:
            _set(mat, "metallic", item["vm"])
        if "vr" in item:
            _set(mat, "roughness", item["vr"])
        for key, attr in _MAT_SETTINGS:
            if key in item and hasattr(mat, attr):
                _set(mat, attr, item[key])
    if node is None:
        return mat, missing
    sockets = {s.identifier: s for s in node.inputs}
    if not textures_only:
        for ident, value in (item.get("p") or {}).items():
            sock = sockets.get(ident)
            if sock is None or not hasattr(sock, "default_value"):
                continue
            for l in list(sock.links):  # the sender has a plain value here (e.g. texture removed)
                mat.node_tree.links.remove(l)
            _set(sock, "default_value", value)
    textures = dict(item.get("tx") or {})
    if item.get("tex"):
        textures["Base Color"] = item["tex"]
    for ident, tex in textures.items():
        sock = sockets.get(ident)
        if sock is not None and not _apply_texture(mat, node, sock, tex):
            missing.append(tex["id"])
    return mat, missing


def apply_slots(ob, slots):
    """Make the object's material slots exactly `slots` ([[name | None, link], ...])."""
    if slots is None or ob.data is None or not hasattr(ob.data, "materials"):
        return
    mats = ob.data.materials
    while len(mats) < len(slots):
        mats.append(None)
    while len(mats) > len(slots):
        mats.pop(index=len(mats) - 1)
    for i, (name, link) in enumerate(slots):
        mat = None
        if name is not None:
            mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)  # its state follows in a "mat" message
        slot = ob.material_slots[i]
        if link in ("DATA", "OBJECT") and slot.link != link:
            slot.link = link
        if slot.material != mat:
            slot.material = mat


def apply_materials(ob, mats):
    """obj_add: upsert every material and fill the slots in order.
    Returns the list of texture ids that are referenced but not available here (yet)."""
    missing = []
    if not mats or ob.data is None or not hasattr(ob.data, "materials"):
        return missing
    me = ob.data
    while len(me.materials) < len(mats):
        me.materials.append(None)
    for i, item in enumerate(mats):
        if item is None:
            continue
        mat, miss = apply_material(item)
        missing.extend(miss)
        if me.materials[i] != mat:
            me.materials[i] = mat
    return missing


# ----------------------------------------------------------------------
# armature structure (Issue #5). One bone = {"n": name, "h": head, "t": tail, "r": roll,
# "p": parent name | None, "c": connected}, in parents-first order, all in armature space (rest pose).
# Bones are matched by NAME on both sides (a rename therefore travels as delete + create).
# Read from edit_bones while the armature is in Edit Mode here (live), from Bone otherwise.
# Not synced: bone collections, deform / inherit flags, custom shapes, constraints, IK, drivers, weights.
# ----------------------------------------------------------------------
_EDIT_OK_MODES = ("OBJECT", "POSE")  # context modes from which we may borrow the view layer for an Edit Mode pass


def _roll_from_bone(bone):
    axis = bone.tail_local - bone.head_local
    if axis.length < 1e-8:
        return 0.0
    try:
        _axis, roll = bpy.types.Bone.AxisRollFromMatrix(bone.matrix_local.to_3x3(), axis=axis.normalized())
        return float(roll)
    except Exception:
        return 0.0


def _bone_item(name, head, tail, roll, parent, connected):
    # "+ 0.0" folds -0.0 into 0.0: edit_bones and Bone disagree on the sign of zero, and the digest is text
    return {"n": name, "h": [x + 0.0 for x in _vec(head)], "t": [x + 0.0 for x in _vec(tail)],
            "r": round(float(roll), 4) + 0.0, "p": parent, "c": bool(connected)}


def _parents_first(items):
    """Deterministic (by name) parents-first order so a receiver can parent bones as it creates them
    and so edit_bones and Bone (which list bones in different orders) produce the same digest."""
    items = sorted(items, key=lambda it: it["n"])
    by_name = {it["n"]: it for it in items}
    out, seen = [], set()

    def visit(it, depth=0):
        if it["n"] in seen or depth > 256:
            return
        parent = by_name.get(it["p"]) if it["p"] else None
        if parent is not None:
            visit(parent, depth + 1)
        seen.add(it["n"])
        out.append(it)

    for it in items:
        visit(it)
    return out


def armature_bones(ob):
    """Bone list of an armature object (see the section header), or None for other objects / over the limit."""
    if ob.type != "ARMATURE" or ob.data is None:
        return None
    arm = ob.data
    items = []
    if ob.mode == "EDIT":
        bones = arm.edit_bones
        if len(bones) > MAX_BONES:
            return None
        for eb in bones:
            items.append(_bone_item(eb.name, eb.head, eb.tail, eb.roll, eb.parent.name if eb.parent else None, eb.use_connect))
    else:
        bones = arm.bones
        if len(bones) > MAX_BONES:
            return None
        for b in bones:
            items.append(_bone_item(b.name, b.head_local, b.tail_local, _roll_from_bone(b),
                                    b.parent.name if b.parent else None, b.use_connect))
    return _parents_first(items)


def local_mode():
    """Mode of the active object of the view layer ("OBJECT" when there is none). Read from the view layer, not
    bpy.context.mode: inside a bpy.app.timers callback (where the add-on runs) the context has no window and
    context.mode is not reliable, while the view layer is."""
    try:
        act = bpy.context.view_layer.objects.active
        return act.mode if act is not None else "OBJECT"
    except Exception:
        try:
            return bpy.context.mode
        except Exception:
            return "OBJECT"


def armature_editable():
    """True when this Blender is in a mode from which we can enter Edit Mode on an armature and come back
    without destroying local work (Object / Pose Mode). While a mesh / curve / armature is being edited here,
    the structure is applied once that Edit Mode ends."""
    return local_mode() in _EDIT_OK_MODES


def _ui_override():
    """Window / 3D View area / region for operators that need a real UI context (the add-on ticks from a timer,
    whose context has no window). None in headless Blender."""
    try:
        for win in bpy.context.window_manager.windows:
            for area in win.screen.areas:
                if area.type == "VIEW_3D":
                    region = next((r for r in area.regions if r.type == "WINDOW"), None)
                    return {"window": win, "screen": win.screen, "area": area, "region": region}
        wins = list(bpy.context.window_manager.windows)
        if wins:
            return {"window": wins[0], "screen": wins[0].screen}
    except Exception:
        pass
    return None


def _mode_set(mode, ob=None):
    """bpy.ops.object.mode_set that also works from a timer: first with a UI context override (real Blender),
    then plain (headless / bpy module). Raises RuntimeError with both errors if neither works."""
    override = _ui_override()
    errors = []
    if override is not None:
        kw = dict(override)
        if ob is not None:
            kw.update(active_object=ob, object=ob, selected_objects=[ob], selected_editable_objects=[ob])
        try:
            with bpy.context.temp_override(**kw):
                bpy.ops.object.mode_set(mode=mode)
            return
        except Exception as e:
            errors.append(f"with UI override: {e}")
    try:
        bpy.ops.object.mode_set(mode=mode)
        return
    except Exception as e:
        errors.append(f"plain: {e}")
    raise RuntimeError(f"mode_set({mode}) failed ({'; '.join(errors)})")


def _edit_armature(ob, fn):
    """Run fn(armature) with `ob` in Edit Mode, then put the view layer back the way it was
    (active object, selection, and the previous object's mode - e.g. Pose Mode on the same rig)."""
    vl = bpy.context.view_layer
    prev_active = vl.objects.active
    prev_mode = prev_active.mode if prev_active is not None else "OBJECT"
    prev_sel = [o for o in vl.objects if o.select_get(view_layer=vl)]
    try:
        if prev_mode != "OBJECT":
            _mode_set("OBJECT", prev_active)
        for o in prev_sel:  # multi-object Edit Mode would pull other selected armatures in with us
            if o != ob:
                o.select_set(False, view_layer=vl)
        vl.objects.active = ob
        ob.select_set(True, view_layer=vl)
        if ob.hide_viewport or not ob.visible_get(view_layer=vl):
            ob.hide_set(False, view_layer=vl)
        _mode_set("EDIT", ob)
        if ob.mode != "EDIT":
            raise RuntimeError(f"{ob.name} did not enter Edit Mode (mode={ob.mode}, active={vl.objects.active})")
        try:
            fn(ob.data)
        finally:
            _mode_set("OBJECT", ob)
    finally:
        try:
            ob.select_set(ob in prev_sel, view_layer=vl)
            for o in prev_sel:
                if o != ob:
                    o.select_set(True, view_layer=vl)
            if prev_active is not None and prev_active.name in bpy.data.objects:
                vl.objects.active = prev_active
                if prev_mode != "OBJECT":
                    _mode_set(prev_mode, prev_active)
        except Exception:
            pass


def _apply_bones(arm, items):
    """Make arm.edit_bones match `items` in place: extra bones are removed, missing ones created,
    head / tail / roll / parent / connected written only where they differ (pose channels of bones
    that keep their name survive the edit session, so the peer's pose is untouched)."""
    ebs = arm.edit_bones
    wanted = {it["n"]: it for it in items}
    for eb in list(ebs):
        if eb.name not in wanted:
            ebs.remove(eb)
    for it in items:
        if it["n"] not in ebs:
            eb = ebs.new(it["n"])
            if eb.name != it["n"]:  # (cannot happen after the removal above; keep remote naming authority anyway)
                eb.name = it["n"]
            eb.head, eb.tail = it["h"], it["t"]
    # disconnect / reparent first: a connected child follows its parent's tail, so parents must be right
    # before heads and tails are written (parents-first order from the sender)
    for it in items:
        eb = ebs[it["n"]]
        parent = ebs.get(it["p"]) if it["p"] else None
        if parent is not None and parent == eb:
            parent = None
        if eb.parent != parent or (eb.use_connect and not it["c"]):
            if eb.use_connect:
                eb.use_connect = False
            if eb.parent != parent:
                eb.parent = parent
    for it in items:
        eb = ebs[it["n"]]
        _set(eb, "head", it["h"])
        _set(eb, "tail", it["t"])
        if abs(float(eb.roll) - float(it["r"])) > 1e-4:
            eb.roll = it["r"]
    for it in items:
        eb = ebs[it["n"]]
        if it["c"] and eb.parent is not None and not eb.use_connect:
            eb.use_connect = True  # snaps head to the parent's tail (already equal: the sender's rig is consistent)


def apply_armature(ob, items):
    """Write a bone list onto an armature object (must be linked in the scene, caller checked armature_editable())."""
    _edit_armature(ob, lambda arm: _apply_bones(arm, items))


def armature_delta_summary(prev, cur):
    """Short human summary of what changed between two bone lists, for the log."""
    if prev is None or cur is None:
        return f"{len(cur or [])} bones"
    a, b = {it["n"]: it for it in prev}, {it["n"]: it for it in cur}
    added, removed = sorted(set(b) - set(a)), sorted(set(a) - set(b))
    changed = sorted(n for n in set(a) & set(b) if a[n] != b[n])
    parts = []
    if added:
        parts.append(f"+{', '.join(added[:5])}{'...' if len(added) > 5 else ''}")
    if removed:
        parts.append(f"-{', '.join(removed[:5])}{'...' if len(removed) > 5 else ''}")
    if changed:
        parts.append(f"~{', '.join(changed[:5])}{'...' if len(changed) > 5 else ''}")
    return "; ".join(parts) or "no change"


# ----------------------------------------------------------------------
# pose bones (Issue: Pose Mode transform sync). Bones are matched by NAME;
# constraints, drivers and keyframes are not synced (the structure is: see above).
# ----------------------------------------------------------------------
_ROT_ATTR = {"QUATERNION": "rotation_quaternion", "AXIS_ANGLE": "rotation_axis_angle"}  # anything else: rotation_euler


def _rot_attr(mode):
    return _ROT_ATTR.get(mode, "rotation_euler")


def _bone_state(pb):
    """One pose bone's transform: rotation mode + the values of THAT mode (no conversion on either side)."""
    mode = pb.rotation_mode
    return {"rm": mode, "l": _vec(pb.location), "r": _vec(getattr(pb, _rot_attr(mode))), "s": _vec(pb.scale)}


def serialize_pose(ob):
    """{bone name: state} for every pose bone of an armature object, or None for other objects."""
    if ob.type != "ARMATURE" or ob.pose is None:
        return None
    return {pb.name: _bone_state(pb) for pb in ob.pose.bones}


def pose_delta(prev, cur):
    """Bones whose state differs from prev (None = everything)."""
    if not prev:
        return dict(cur)
    return {name: st for name, st in cur.items() if prev.get(name) != st}


def apply_pose(ob, bones):
    """Write bone states onto ob.pose.bones by name. Unknown bones are skipped.
    Returns (applied names, skipped names)."""
    applied, skipped = [], []
    if ob.type != "ARMATURE" or ob.pose is None:
        return applied, list(bones)
    pbones = ob.pose.bones
    for name, st in bones.items():
        pb = pbones.get(name)
        if pb is None or not isinstance(st, dict):
            skipped.append(name)
            continue
        mode = st.get("rm")
        if mode and pb.rotation_mode != mode:
            try:
                pb.rotation_mode = mode
            except TypeError:
                skipped.append(name)
                continue
        if "l" in st:
            _set(pb, "location", st["l"])
        if "r" in st:
            _set(pb, _rot_attr(pb.rotation_mode), st["r"])
        if "s" in st:
            _set(pb, "scale", st["s"])
        applied.append(name)
    return applied, skipped
