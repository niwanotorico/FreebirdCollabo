# SPDX-License-Identifier: GPL-2.0-or-later
"""
Draws remote users (head, hands, pointer rays, selection boxes, name/tool
labels) with the gpu module, in both the desktop 3D viewport (WINDOW) and
the VR view (XR region). Independent from Freebird's bl_xr renderer so it
also works when Freebird is not installed or not running.
"""

import time

import bpy
from mathutils import Matrix, Quaternion, Vector

try:
    import blf
    import gpu
    from gpu_extras.batch import batch_for_shader
except Exception:  # headless / bpy module without GPU
    gpu = None

STALE_SECONDS = 3.0
HEAD_SIZE = 0.16
HAND_SIZE = 0.05
RAY_LENGTH = 2.0
FORWARD = Vector((0.0, 0.0, -1.0))  # OpenXR pose convention (as exposed by bpy XrSessionState)

_handles = []
_shader = None


def _cube_lines(size):
    h = size * 0.5
    c = [Vector((x, y, z)) for x in (-h, h) for y in (-h, h) for z in (-h, h)]
    idx = [(0, 1), (0, 2), (1, 3), (2, 3), (4, 5), (4, 6), (5, 7), (6, 7), (0, 4), (1, 5), (2, 6), (3, 7)]
    return [c[i] for pair in idx for i in pair]


def _pyramid_lines(size):
    tip = Vector((0, 0, -size * 1.6))
    b = [Vector((x, y, 0)) for x, y in ((-size, -size), (size, -size), (size, size), (-size, size))]
    pts = []
    for i in range(4):
        pts += [b[i], b[(i + 1) % 4], b[i], tip]
    return pts


HEAD_PTS = _cube_lines(HEAD_SIZE)
HAND_PTS = _pyramid_lines(HAND_SIZE * 0.5)


def _pose_matrix(p):
    return Matrix.LocRotScale(Vector(p[0:3]), Quaternion(p[3:7]), Vector((1, 1, 1)))


def _draw_lines(points, color, matrix=None, width=2.0):
    shader = _get_shader()
    if matrix is not None:
        points = [matrix @ p for p in points]
    batch = batch_for_shader(shader, "LINES", {"pos": points})
    gpu.state.line_width_set(width)
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)


def _get_shader():
    global _shader
    if _shader is None:
        _shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    return _shader


def _viewer_rotation():
    """Rotation to billboard labels toward the local viewer (desktop or VR)."""
    try:
        rd = bpy.context.region_data
        if rd is not None and getattr(bpy.context.region, "type", "") == "WINDOW":
            return rd.view_rotation.copy()
    except Exception:
        pass
    try:
        st = bpy.context.window_manager.xr_session_state
        if st and st.is_running(bpy.context):
            return Quaternion(st.viewer_pose_rotation)
    except Exception:
        pass
    return Quaternion()


def _draw_label(text, pos, color, view_rot, scale=0.0025, font_size=40):
    font_id = 0
    blf.size(font_id, font_size)
    lines = text.split("\n")
    widths = [blf.dimensions(font_id, ln)[0] for ln in lines]
    w = max(widths) if widths else 0
    line_h = font_size * 1.25
    mat = Matrix.LocRotScale(pos, view_rot, Vector((scale, scale, scale)))
    with gpu.matrix.push_pop():
        gpu.matrix.multiply_matrix(mat)
        gpu.state.depth_test_set("NONE")
        blf.color(font_id, *color[:3], 1.0)
        for i, ln in enumerate(lines):
            blf.position(font_id, -widths[i] * 0.5, (len(lines) - 1 - i) * line_h, 0)
            blf.draw(font_id, ln)
        gpu.state.depth_test_set("LESS_EQUAL")
    return w * scale


def _selection_box(ob):
    m = ob.matrix_world
    c = [m @ Vector(v) for v in ob.bound_box]
    idx = [(0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4), (0, 4), (1, 5), (2, 6), (3, 7)]
    return [c[i] for pair in idx for i in pair]


def _tool_label(tool):
    if not tool:
        return ""
    if tool.startswith("fb:"):
        return tool[3:].replace(".", " ").replace("_", " ").title()
    return tool.replace("_", " ").title()


def draw_peers(session):
    if gpu is None or session is None or not session.peers:
        return
    now = time.time()
    view_rot = _viewer_rotation()
    gpu.state.blend_set("ALPHA")
    gpu.state.depth_test_set("LESS_EQUAL")
    for peer in list(session.peers.values()):
        pres = peer.presence
        if not pres or now - peer.last_seen > STALE_SECONDS:
            continue
        col = (*peer.color[:3], 1.0)
        col_soft = (*peer.color[:3], 0.55)
        label_pos = None

        head = pres.get("head")
        if head:
            hm = _pose_matrix(head)
            _draw_lines(HEAD_PTS, col, hm, 2.0)
            _draw_lines([Vector((0, 0, 0)), FORWARD * HEAD_SIZE], col, hm, 3.0)  # gaze direction
            label_pos = hm @ Vector((0, 0, 0)) + Vector((0, 0, HEAD_SIZE * 0.9))

        hands = pres.get("hands") or {}
        for key in ("L", "R"):
            hp = hands.get(key)
            if not hp:
                continue
            m = _pose_matrix(hp)
            _draw_lines(HAND_PTS, col, m, 2.0)
            _draw_lines([Vector((0, 0, 0)), FORWARD * RAY_LENGTH], col_soft, m, 1.5)  # pointer ray

        sel_objs = []
        for name in pres.get("sel", []) or []:
            ob = bpy.data.objects.get(name)
            if ob:
                sel_objs.append(ob)
                _draw_lines(_selection_box(ob), col, None, 1.5)

        if label_pos is None and sel_objs:  # desktop peer: hang the label above their selection
            ob = sel_objs[0]
            top = max((ob.matrix_world @ Vector(v)).z for v in ob.bound_box)
            label_pos = (ob.matrix_world @ Vector((0, 0, 0))).copy()
            label_pos.z = top + 0.15

        if label_pos is not None:
            sel_txt = ", ".join(pres.get("sel", [])[:3]) or "-"
            text = f"{peer.name}\n{_tool_label(pres.get('tool')) or pres.get('mode', '')}\nSelected: {sel_txt}"
            _draw_label(text, label_pos, col, view_rot)


def draw_local_hud(session):
    """Small status line shown in VR near the origin of view when in a room."""
    pass  # intentionally minimal for the MVP; Freebird's own menu shows room status


# ----------------------------------------------------------------------
def register(get_session):
    if gpu is None or bpy.app.background:
        return

    def _cb():
        try:
            draw_peers(get_session())
        except Exception as e:  # never break the viewport
            print(f"[collab] draw error: {e}")

    for region in ("WINDOW", "XR"):
        try:
            h = bpy.types.SpaceView3D.draw_handler_add(_cb, (), region, "POST_VIEW")
            _handles.append((h, region))
        except Exception as e:
            print(f"[collab] cannot add draw handler for {region}: {e}")


def unregister():
    for h, region in _handles:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(h, region)
        except Exception:
            pass
    _handles.clear()
