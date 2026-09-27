# SPDX-License-Identifier: GPL-2.0-or-later
"""
VR Studio for Freebird XR - Blender add-on that installs / updates / removes the VR Studio
Freebird plugin. It contains NO VR Studio features itself: the bundled `payload/vr_studio`
folder (the Freebird plugin, unchanged) is copied to

    ~/.freebird/plugins/vr_studio        (C:\\Users\\<you>\\.freebird\\plugins\\vr_studio)

where Freebird XR loads it. Nothing in Freebird XR or other add-ons is modified.

Rules:
  * Enabling the add-on right after installing a new zip sets the plugin up once:
      - not installed                -> copy it
      - same files already there     -> nothing to do
      - older / different copy       -> move that copy to ~/.freebird/plugin_backups/, then copy
      - a NEWER version is installed -> leave it alone (Install button still available)
  * Disabling or removing this add-on never touches the plugin (it keeps working in Freebird).
  * "Remove VR Studio Plugin" in the add-on preferences deletes the plugin folder after a
    confirmation. Backups are never deleted automatically.
"""

bl_info = {
    "name": "VR Studio for Freebird XR",
    "author": "niwanotorico",
    "version": (0, 3, 0),
    "blender": (4, 2, 0),
    "location": "Edit > Preferences > Add-ons > VR Studio for Freebird XR",
    "description": "Installs the VR Studio plugin (Color / Look / View panels) into Freebird XR",
    "doc_url": "https://github.com/niwanotorico/FreebirdCollabo",
    "tracker_url": "https://github.com/niwanotorico/FreebirdCollabo/issues",
    "category": "3D View",
}

import ast
import filecmp
import json
import os
import shutil
import sys
import time

import bpy

PLUGIN_NAME = "vr_studio"
HERE = os.path.dirname(os.path.abspath(__file__))
PAYLOAD = os.path.join(HERE, "payload", PLUGIN_NAME)
STATE_FILE = os.path.join(HERE, "setup_state.json")  # lives in this add-on's folder: a new zip = fresh state

_last_message = ""


# ----------------------------------------------------------------------------- paths / versions
def freebird_dir():
    # same rule as Freebird XR (settings_manager.get_freebird_dir): ~/.freebird
    return os.path.join(os.path.expanduser("~"), ".freebird")


def plugins_dir():
    return os.path.join(freebird_dir(), "plugins")


def plugin_dir():
    return os.path.join(plugins_dir(), PLUGIN_NAME)


def backups_dir():
    # outside plugins/, so Freebird never loads a backup as a second copy
    return os.path.join(freebird_dir(), "plugin_backups")


def read_version(folder):
    """fb_info["version"] of a plugin folder as a tuple, None if missing/unreadable."""
    init = os.path.join(folder, "__init__.py")
    try:
        with open(init, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "fb_info" for t in node.targets):
                v = ast.literal_eval(node.value).get("version")
                return tuple(v) if v else ()
    except Exception:
        return None
    return () if os.path.isfile(init) else None


def vstr(v):
    return ".".join(str(x) for x in v) if v else "unknown"


def _payload_files():
    out = []
    for d, dirs, names in os.walk(PAYLOAD):
        dirs[:] = [x for x in dirs if x != "__pycache__"]
        out += [os.path.relpath(os.path.join(d, n), PAYLOAD) for n in names if not n.endswith(".pyc")]
    return sorted(out)


def same_as_payload(folder):
    """True if every bundled file exists in `folder` with identical content
    (extra files such as __pycache__ are ignored)."""
    for rel in _payload_files():
        other = os.path.join(folder, rel)
        if not os.path.isfile(other) or not filecmp.cmp(os.path.join(PAYLOAD, rel), other, shallow=False):
            return False
    return True


def status():
    """(code, installed_version) with code in
    missing | current | older | modified | newer | broken"""
    bundled = read_version(PAYLOAD)
    if not os.path.isdir(plugin_dir()):
        return "missing", None
    installed = read_version(plugin_dir())
    if installed is None:
        return "broken", None
    if same_as_payload(plugin_dir()):
        return "current", installed
    if installed and bundled and installed > bundled:
        return "newer", installed
    if installed == bundled:
        return "modified", installed
    return "older", installed


def freebird_available():
    return any(m == "freebird.plugin_manager" or m.endswith(".freebird.plugin_manager") for m in sys.modules) or \
        os.path.isdir(freebird_dir())


# ----------------------------------------------------------------------------- install / remove
def _backup_existing():
    """Move the installed plugin folder to plugin_backups/. Returns the backup path."""
    os.makedirs(backups_dir(), exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = os.path.join(backups_dir(), f"{PLUGIN_NAME}_v{vstr(read_version(plugin_dir()))}_{stamp}")
    dest, n = base, 2
    while os.path.exists(dest):
        dest, n = f"{base}_{n}", n + 1
    shutil.move(plugin_dir(), dest)
    return dest


def install_plugin():
    """Copy the bundled plugin into ~/.freebird/plugins/vr_studio.
    An existing copy is moved to plugin_backups/ first (unless it is identical).
    The new copy is fully written next to the target (as a hidden '.'-folder Freebird ignores)
    before anything is moved, and the old copy is put back if the swap fails."""
    if not os.path.isfile(os.path.join(PAYLOAD, "__init__.py")):
        raise RuntimeError("the bundled VR Studio files are missing - please reinstall the add-on zip")
    os.makedirs(plugins_dir(), exist_ok=True)
    if os.path.isdir(plugin_dir()) and same_as_payload(plugin_dir()):
        return None

    staging = os.path.join(plugins_dir(), f".{PLUGIN_NAME}_installing")
    if os.path.exists(staging):
        shutil.rmtree(staging)  # our own leftover from an interrupted run
    shutil.copytree(PAYLOAD, staging, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if not same_as_payload(staging):
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError("copy check failed")

    backup = None
    if os.path.exists(plugin_dir()):
        backup = _backup_existing()
    try:
        os.replace(staging, plugin_dir())
    except Exception:
        if backup and not os.path.exists(plugin_dir()):
            shutil.move(backup, plugin_dir())  # put the old copy back
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return backup


def remove_plugin():
    if os.path.isdir(plugin_dir()):
        shutil.rmtree(plugin_dir())
    return not os.path.exists(plugin_dir())


def reload_freebird_plugins():
    """Ask Freebird XR to reload its plugins (same as its own 'Reload All' button), so the new
    buttons appear without restarting Blender. Returns True if Freebird did it."""
    for name, mod in list(sys.modules.items()):
        if (name == "freebird.plugin_manager" or name.endswith(".freebird.plugin_manager")) and hasattr(mod, "load_plugins"):
            try:
                mod.load_plugins()
                return True
            except Exception as e:
                print(f"[VR Studio setup] Freebird plugin reload failed: {e}")
                return False
    return False


def _read_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_state(**kw):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(kw, f)
    except Exception as e:
        print(f"[VR Studio setup] could not write {STATE_FILE}: {e}")


def _set_message(msg):
    global _last_message
    _last_message = msg
    print(f"[VR Studio setup] {msg}")


def _done_message(backup, reloaded):
    parts = [f"VR Studio {vstr(read_version(PAYLOAD))} is installed."]
    if backup:
        parts.append(f"The previous copy was backed up to {backup}.")
    parts.append("Color / Look / View are in Freebird's Plugins menu." if reloaded
                 else "Restart Blender, then find Color / Look / View in Freebird's Plugins menu.")
    return " ".join(parts)


def auto_setup():
    """Runs once per installed add-on zip (after enabling it). Never overwrites a newer plugin."""
    if _read_state() is not None:
        return None
    code, installed = status()
    try:
        if code == "newer":
            _set_message(f"A newer VR Studio ({vstr(installed)}) is already installed - left unchanged.")
        elif code == "current":
            _set_message(f"VR Studio {vstr(installed)} is already installed and up to date.")
        else:
            backup = install_plugin()
            _set_message(_done_message(backup, reload_freebird_plugins()))
        _write_state(bundled=vstr(read_version(PAYLOAD)), result=code)
    except Exception as e:
        _set_message(f"Could not install VR Studio automatically: {e}. Use the Install button to try again.")
    return None  # one-shot timer


# ----------------------------------------------------------------------------- UI
class VRSTUDIO_OT_install(bpy.types.Operator):
    bl_idname = "vr_studio_setup.install"
    bl_label = "Install / Update VR Studio"
    bl_description = "Copy the bundled VR Studio plugin into Freebird XR's plugins folder (an existing copy is backed up first)"
    bl_options = {"INTERNAL"}

    def invoke(self, context, event):
        code, installed = status()
        if code in ("newer", "modified"):
            return context.window_manager.invoke_confirm(
                self, event, title="Replace installed VR Studio?",
                message=f"Installed: {vstr(installed)} -> bundled: {vstr(read_version(PAYLOAD))}. "
                        "The installed copy will be moved to the backups folder.",
                confirm_text="Replace")
        return self.execute(context)

    def execute(self, context):
        try:
            backup = install_plugin()
        except Exception as e:
            _set_message(f"Install failed: {e}")
            self.report({"ERROR"}, _last_message)
            return {"CANCELLED"}
        _set_message(_done_message(backup, reload_freebird_plugins()))
        self.report({"INFO"}, _last_message)
        return {"FINISHED"}


class VRSTUDIO_OT_remove(bpy.types.Operator):
    bl_idname = "vr_studio_setup.remove"
    bl_label = "Remove VR Studio Plugin"
    bl_description = "Delete the VR Studio plugin folder from Freebird XR's plugins folder (asks first)"
    bl_options = {"INTERNAL"}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(
            self, event, title="Remove VR Studio from Freebird XR?",
            message=f"This deletes {plugin_dir()}. Materials you made stay in your .blend files.",
            confirm_text="Remove", icon="WARNING")

    def execute(self, context):
        try:
            ok = remove_plugin()
        except Exception as e:
            ok = False
            _set_message(f"Could not remove the folder: {e}. Close Blender and delete it by hand: {plugin_dir()}")
        if ok:
            reloaded = reload_freebird_plugins()
            _set_message("VR Studio plugin removed." + ("" if reloaded else " Restart Blender to finish.")
                         + " You can now also remove this add-on.")
            self.report({"INFO"}, _last_message)
            return {"FINISHED"}
        self.report({"ERROR"}, _last_message or "Could not remove the plugin folder")
        return {"CANCELLED"}


class VRSTUDIO_OT_open_folder(bpy.types.Operator):
    bl_idname = "vr_studio_setup.open_folder"
    bl_label = "Open Folder"
    bl_description = "Open the folder in your file manager"
    bl_options = {"INTERNAL"}

    which: bpy.props.EnumProperty(items=[("PLUGINS", "Plugins", ""), ("BACKUPS", "Backups", "")])

    def execute(self, context):
        path = plugins_dir() if self.which == "PLUGINS" else backups_dir()
        if not os.path.isdir(path):
            self.report({"WARNING"}, f"Folder does not exist yet: {path}")
            return {"CANCELLED"}
        bpy.ops.wm.path_open(filepath=path)
        return {"FINISHED"}


_STATUS_TEXT = {
    "missing": ("Not installed", "ERROR"),
    "current": ("Installed {v} - up to date", "CHECKMARK"),
    "older": ("Installed {v} - update available ({b})", "INFO"),
    "modified": ("Installed {v} - differs from this add-on's copy", "INFO"),
    "newer": ("Installed {v} - newer than this add-on ({b})", "INFO"),
    "broken": ("Folder exists but is not a VR Studio plugin", "ERROR"),
}


class VRSTUDIO_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    def draw(self, context):
        layout = self.layout
        code, installed = status()
        bundled = read_version(PAYLOAD)
        text, icon = _STATUS_TEXT[code]

        box = layout.box()
        box.label(text="VR Studio plugin: " + text.format(v=vstr(installed), b=vstr(bundled)), icon=icon)
        box.label(text=f"Bundled in this add-on: {vstr(bundled)}")
        col = box.column(align=True)
        col.scale_y = 0.8
        col.label(text=f"Location: {plugin_dir()}")
        if not freebird_available():
            box.label(text="Freebird XR was not found - install and enable Freebird XR too.", icon="ERROR")

        row = layout.row(align=True)
        row.scale_y = 1.3
        label = {"missing": "Install VR Studio", "older": "Update VR Studio", "current": "Reinstall"}.get(code, "Replace with bundled version")
        row.operator(VRSTUDIO_OT_install.bl_idname, text=label, icon="IMPORT")
        sub = row.row(align=True)
        sub.enabled = code != "missing"
        sub.operator(VRSTUDIO_OT_remove.bl_idname, text="Remove VR Studio Plugin", icon="TRASH")

        row = layout.row(align=True)
        row.operator(VRSTUDIO_OT_open_folder.bl_idname, text="Open Plugins Folder", icon="FILE_FOLDER").which = "PLUGINS"
        b = row.row(align=True)
        b.enabled = os.path.isdir(backups_dir())
        b.operator(VRSTUDIO_OT_open_folder.bl_idname, text="Open Backups Folder", icon="FILE_FOLDER").which = "BACKUPS"

        if _last_message:
            msg = layout.box().column(align=True)
            for i, line in enumerate(_wrap(_last_message, 90)):
                msg.label(text=line, icon="INFO" if i == 0 else "BLANK1")

        help_col = layout.column(align=True)
        help_col.scale_y = 0.8
        help_col.label(text="In VR: Freebird menu > Plugins > Color / Look / View.")
        help_col.label(text="Disabling or removing this add-on keeps the plugin. To uninstall completely:")
        help_col.label(text="press 'Remove VR Studio Plugin' first, then remove this add-on.")


def _wrap(text, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    return lines + ([cur] if cur else [])


CLASSES = (VRSTUDIO_OT_install, VRSTUDIO_OT_remove, VRSTUDIO_OT_open_folder, VRSTUDIO_AddonPreferences)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)
    # deferred: runs after all add-ons (incl. Freebird XR) are registered
    bpy.app.timers.register(auto_setup, first_interval=0.5, persistent=True)


def unregister():
    # Intentionally does NOT remove the plugin: disabling this add-on must not break Freebird.
    if bpy.app.timers.is_registered(auto_setup):
        bpy.app.timers.unregister(auto_setup)
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
