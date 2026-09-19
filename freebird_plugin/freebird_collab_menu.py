# SPDX-License-Identifier: GPL-2.0-or-later
"""
Freebird XR plugin: adds COLLAB buttons to the Freebird VR main menu.
Copy this file to  ~/.freebird/plugins/  (C:\\Users\\<you>\\.freebird\\plugins).
Requires the 'freebird_collab' Blender add-on to be installed and enabled.

Buttons (CUSTOM section of the Freebird menu):
    Create Room   -> host, using the add-on preferences (relay / direct)
    Join Room     -> joins the room code / IP typed in the desktop COLLAB panel
    Leave Room
"""

import sys

fb_info = {"name": "Collab Room", "version": (0, 1, 0)}

PLUGIN_ID = "freebird_collab_menu"


def _addon():
    for name, mod in list(sys.modules.items()):
        if name == "freebird_collab" or name.endswith(".freebird_collab"):
            if hasattr(mod, "create_room"):
                return mod
    return None


def _defer(fn):
    """Run on the next main-thread tick, outside the VR draw callback."""
    import bpy

    def _once():
        try:
            fn()
        except Exception as e:
            print(f"[collab] menu action failed: {e}")
        return None

    bpy.app.timers.register(_once, first_interval=0.0)


def _create():
    m = _addon()
    if m:
        _defer(m.create_room)


def _join():
    m = _addon()
    if m:
        _defer(m.join_room)


def _leave():
    m = _addon()
    if m:
        _defer(m.leave_room)


def register():
    from freebird.api import add_launcher_button

    add_launcher_button(PLUGIN_ID, "create", "Create Room", _create)
    add_launcher_button(PLUGIN_ID, "join", "Join Room", _join)
    add_launcher_button(PLUGIN_ID, "leave", "Leave Room", _leave)


def unregister():
    from freebird.api import remove_launcher_button

    for b in ("create", "join", "leave"):
        remove_launcher_button(PLUGIN_ID, b)
