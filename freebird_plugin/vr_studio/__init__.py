# SPDX-License-Identifier: GPL-2.0-or-later
"""
VR Studio: "Blender is powerful but hard in VR" -> "in VR you just touch it".

A Freebird XR plugin (folder plugin). Install by copying the whole `vr_studio` folder to
    ~/.freebird/plugins/vr_studio/      (C:\\Users\\<you>\\.freebird\\plugins\\vr_studio)
and restarting Blender (Freebird loads plugins when it starts).

v0.1 (MVP): COLOR
    Freebird menu -> CUSTOM (plugins) -> "Color" toggles a 32-colour panel on the left hand.
    Select objects with Freebird's select tool, point at a colour, pull the trigger: done.
    Undo / Redo are Freebird's own (left joystick / controller buttons).

Independent of the freebird_collab add-on: it only edits Blender materials, and the
collab material sync (when a room is open) carries the change to everyone.
"""

import os

fb_info = {"name": "VR Studio", "version": (0, 1, 0)}

PLUGIN_ID = "vr_studio"
ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")

_panel = None


def _get_panel():
    global _panel
    if _panel is None:
        from .panel import ColorPanel

        _panel = ColorPanel()
    return _panel


def _toggle_color():
    panel = _get_panel()
    panel.attach()
    panel.toggle()


def _on_xr_start(self, event_name, event):
    _get_panel().attach()  # stays hidden until the "Color" button is pressed


def _on_xr_end(self, event_name, event):
    if _panel is not None:
        _panel.detach()


def _icon(name):
    p = os.path.join(ICON_DIR, name)
    return p if os.path.exists(p) else None


def register():
    from bl_xr import root, xr_session
    from freebird.api import add_launcher_button

    add_launcher_button(PLUGIN_ID, "color", "Color", _toggle_color, _icon("color.png"))
    root.add_event_listener("fb.xr_start", _on_xr_start)
    root.add_event_listener("fb.xr_end", _on_xr_end)
    try:
        if xr_session.is_running:  # plugin (re)loaded while already in VR
            _get_panel().attach()
    except Exception:
        pass


def unregister():
    global _panel
    from bl_xr import root
    from freebird.api import remove_launcher_button

    remove_launcher_button(PLUGIN_ID, "color")
    root.remove_event_listener("fb.xr_start", _on_xr_start)
    root.remove_event_listener("fb.xr_end", _on_xr_end)
    if _panel is not None:
        _panel.detach()
        _panel = None
