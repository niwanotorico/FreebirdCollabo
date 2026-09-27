# SPDX-License-Identifier: GPL-2.0-or-later
"""VR viewport shading. Only bpy.

What the headset shows is NOT the 3D Viewport's shading: Blender's XR session draws with its
own View3DShading, `WindowManager.xr_session_settings.shading` (the "VR Scene Inspection"
settings). Freebird (v2.15) never changes it (it only sets base scale / pose / controllers /
object extras on the XR session settings), so VR Studio switches that one.

  * the desktop 3D Viewport keeps its own shading (independent, untouched)
  * local to this Blender: it lives on the WindowManager, not the Scene, so FreebirdCollabo
    neither sends it nor receives it (a guest opens the host's scene with load_ui=False,
    which keeps its own WindowManager). A: Solid / B: Material Preview is fine.
  * kept across VR restarts in the same Blender session (and saved with the .blend's UI)
"""

import bpy

MODES = [
    # shading.type, tile label, hover text
    ("WIREFRAME", "Wire", "Wireframe"),
    ("SOLID", "Solid", "Solid"),
    ("MATERIAL", "Material", "Material Preview"),
    ("RENDERED", "Rendered", "Rendered"),
]


def _xr_shading(context=None):
    context = context or bpy.context
    settings = getattr(context.window_manager, "xr_session_settings", None)
    return getattr(settings, "shading", None)


def get_vr_shading(context=None):
    shading = _xr_shading(context)
    return shading.type if shading is not None else None


def set_vr_shading(mode, context=None):
    """Switch what the headset shows. Returns the render engine name for RENDERED (for the status line)."""
    shading = _xr_shading(context)
    if shading is None:
        raise RuntimeError("no XR session settings")
    shading.type = mode
    return (context or bpy.context).scene.render.engine if mode == "RENDERED" else None
