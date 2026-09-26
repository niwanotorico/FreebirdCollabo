# SPDX-License-Identifier: GPL-2.0-or-later
"""
VR Studio: "Blender is powerful but hard in VR" -> "in VR you just touch it".

A Freebird XR plugin (folder plugin). Install by copying the whole `vr_studio` folder to
    ~/.freebird/plugins/vr_studio/      (C:\\Users\\<you>\\.freebird\\plugins\\vr_studio)
and restarting Blender (Freebird loads plugins when it starts).

Freebird menu -> Plugins (CUSTOM) -> "Color" / "Look" / "View" toggle their panels in the VR Studio
area, right next to the main menu on the left hand (on the side Freebird's sub-menus open).
    COLOR  32 colours.   Select objects, point at a colour, pull the trigger: done.
    LOOK   8 materials (Clay / Matte / Glossy / Plastic / Metallic / Glass / Emission / Toon).
           Independent of Color: blue + Matte -> blue + Metallic keeps the blue, and a new
           colour keeps the Look.
    VIEW   the headset's shading: Wireframe / Solid / Material Preview / Rendered.
           Only this person's view (never synced to the collab room).
Undo / Redo are Freebird's own (left joystick / controller buttons).

Independent of the freebird_collab add-on: it only edits Blender materials, and the
collab material + node tree sync (when a room is open) carries the change to everyone.
"""

import os

fb_info = {"name": "VR Studio", "version": (0, 3, 0)}

PLUGIN_ID = "vr_studio"
ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")

# launcher button id, label, icon, section class (module, name). Order = bottom-to-top in the area.
SECTIONS = [
    ("color", "Color", "color.png", ("color_section", "ColorSection")),
    ("look", "Look", "look.png", ("look_section", "LookSection")),
    ("view", "View", "view.png", ("view_section", "ViewSection")),
]

_area = None
_sections = {}


def _build():
    """Build the area and its sections. Called at VR start (Freebird builds its own menus then too;
    thumbnails must not be loaded from an application timer)."""
    global _area
    if _area is not None:
        return _area
    import importlib

    from .area import StudioArea

    area = StudioArea()
    for button_id, _label, _icon, (mod_name, cls_name) in SECTIONS:
        try:
            mod = importlib.import_module(f".{mod_name}", __name__)
            _sections[button_id] = area.add(getattr(mod, cls_name)())
        except Exception as e:
            import traceback

            print(f"[vr_studio] could not build {cls_name}: {e}\n{traceback.format_exc()}")
    _area = area
    return area


def _toggler(button_id):
    def toggle():
        area = _build()
        area.attach()
        section = _sections.get(button_id)
        if section is not None:
            area.toggle(section)

    return toggle


def _on_xr_start(self, event_name, event):
    _build().attach()  # sections stay hidden until their button is pressed


def _on_xr_end(self, event_name, event):
    if _area is not None:
        _area.detach()


def _icon(name):
    p = os.path.join(ICON_DIR, name)
    return p if os.path.exists(p) else None


def register():
    from bl_xr import root, xr_session
    from freebird.api import add_launcher_button

    for button_id, label, icon, _cls in SECTIONS:
        add_launcher_button(PLUGIN_ID, button_id, label, _toggler(button_id), _icon(icon))
    root.add_event_listener("fb.xr_start", _on_xr_start)
    root.add_event_listener("fb.xr_end", _on_xr_end)
    try:
        if xr_session.is_running:  # plugin (re)loaded while already in VR
            _build().attach()
    except Exception:
        pass


def unregister():
    global _area
    from bl_xr import root
    from freebird.api import remove_launcher_button

    for button_id, *_ in SECTIONS:
        remove_launcher_button(PLUGIN_ID, button_id)
    root.remove_event_listener("fb.xr_start", _on_xr_start)
    root.remove_event_listener("fb.xr_end", _on_xr_end)
    if _area is not None:
        _area.detach()
    _area = None
    _sections.clear()
