# SPDX-License-Identifier: GPL-2.0-or-later

bl_info = {
    "name": "Freebird Collaboration Layer",
    "author": "chikin + Claude",
    "version": (0, 12, 1),
    "blender": (4, 2, 0),
    "location": "3D View > Sidebar > COLLAB  (and Freebird XR menu via plugin)",
    "description": "Remote co-editing: shared room, host-authoritative scene, live head/hand/pointer/selection/tool presence",
    "category": "3D View",
}

import os
import random

import bpy
from bpy.props import EnumProperty, FloatVectorProperty, IntProperty, StringProperty

from . import history
from . import presence
from . import update_check
from . import updater
from .session import LOCAL_UNDO_MODES, CollabSession

TICK_INTERVAL = 1.0 / 60.0

# Public relay preset as the Relay URL default, so participants only type the Room Code.
# Anyone running their own relay (docs/internet-relay.md) can overwrite the field in the preferences.
DEFAULT_RELAY_URL = "wss://freebird-relay.chickenos.workers.dev"

_session = CollabSession()


def get_session() -> CollabSession:
    return _session


# ----------------------------------------------------------------------
# preferences (set once, never during normal use)
# ----------------------------------------------------------------------
def _random_color():
    random.seed()
    return random.choice([(0.25, 0.65, 1.0), (1.0, 0.45, 0.25), (0.35, 0.9, 0.45), (0.95, 0.8, 0.2), (0.8, 0.4, 1.0)])


class COLLAB_Preferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    display_name: StringProperty(name="Display Name", default="User")
    color: FloatVectorProperty(name="My Color", subtype="COLOR", size=3, min=0, max=1, default=_random_color())
    mode: EnumProperty(
        name="Connection",
        items=[
            ("RELAY", "Relay server (room code)", "Both users connect to a relay; join with a 6-letter room code"),
            ("DIRECT", "Direct (IP address)", "Host listens on a port; guest enters the host IP (LAN / VPN / debug)"),
        ],
        default="RELAY",
    )
    relay_url: StringProperty(
        name="Relay URL",
        description=(
            "Preset to the public relay; normally leave as is. "
            "Change only for your own relay: wss://<your-relay> | ws://192.168.x.x:7788 (LAN) | tcp://host:7788. "
            "Empty = use the preset"
        ),
        default=DEFAULT_RELAY_URL,
    )
    direct_port: IntProperty(name="Direct Port", default=7788, min=1, max=65535)

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "display_name")
        col.prop(self, "color")
        col.separator()
        col.prop(self, "mode")
        if self.mode == "RELAY":
            row = col.row(align=True)
            row.prop(self, "relay_url")
            row.operator("collab.reset_relay_url", text="", icon="LOOP_BACK")
            row = col.row(align=True)
            row.operator("collab.check_relay", icon="PLUGIN")
            row.label(text=_relay_check_result or "Relay is preset. Just share the Room Code and Join.")
        else:
            col.prop(self, "direct_port")


def _prefs():
    return bpy.context.preferences.addons[__name__].preferences


def _relay_url(p=None):
    """Relay URL to use: the preference value, or the preset when the field was left empty."""
    p = p or _prefs()
    return p.relay_url.strip() or DEFAULT_RELAY_URL


# ----------------------------------------------------------------------
# public API (used by the Freebird VR-menu plugin and by tests)
# ----------------------------------------------------------------------
def create_room():
    p = _prefs()
    return _session.create_room(p.mode, p.display_name, tuple(p.color), relay_url=_relay_url(p), direct_port=p.direct_port)


def join_room(code_or_host=None):
    p = _prefs()
    wm = bpy.context.window_manager
    target = code_or_host if code_or_host is not None else wm.collab_join_target
    if p.mode == "DIRECT":
        host, _, port = target.partition(":")
        return _session.join_room(
            "DIRECT", p.display_name, tuple(p.color), direct_host=host.strip() or "127.0.0.1",
            direct_port=int(port) if port else p.direct_port,
        )
    return _session.join_room("RELAY", p.display_name, tuple(p.color), code=target.strip().upper(), relay_url=_relay_url(p))


def leave_room():
    _session.leave()


def room_status():
    return _session.status


# ----------------------------------------------------------------------
# operators
# ----------------------------------------------------------------------
class COLLAB_OT_create_room(bpy.types.Operator):
    bl_idname = "collab.create_room"
    bl_label = "Create Room"
    bl_description = "Host a room. Your Blender scene becomes the master scene"

    def execute(self, context):
        if create_room():
            self.report({"INFO"}, "Room created")
        else:
            self.report({"ERROR"}, _session.error or "failed")
        return {"FINISHED"}


class COLLAB_OT_join_room(bpy.types.Operator):
    bl_idname = "collab.join_room"
    bl_label = "Join Room"
    bl_description = "Join a room. The host's scene replaces your current scene"

    def execute(self, context):
        if join_room():
            self.report({"INFO"}, "Joining room")
        else:
            self.report({"ERROR"}, _session.error or "failed")
        return {"FINISHED"}


class COLLAB_OT_leave_room(bpy.types.Operator):
    bl_idname = "collab.leave_room"
    bl_label = "Leave Room"

    def execute(self, context):
        leave_room()
        return {"FINISHED"}


class COLLAB_OT_save_master(bpy.types.Operator):
    bl_idname = "collab.save_master"
    bl_label = "Save Master Scene"
    bl_description = "Save the host's scene (guests ask the host to save)"

    def execute(self, context):
        _session.request_save()
        return {"FINISHED"}


_relay_check_result = ""


class COLLAB_OT_check_relay(bpy.types.Operator):
    bl_idname = "collab.check_relay"
    bl_label = "Check Relay"
    bl_description = "Connect to the relay URL once and report whether it answers"

    def execute(self, context):
        global _relay_check_result
        import time

        from .link import Link
        from .protocol import make

        url = _relay_url()
        t0 = time.time()
        try:
            link = Link(url)
            link.connect(timeout=10)
            link.send(make("hello", name="check", role="host", room="", color=[1, 1, 1]))
            deadline = time.time() + 10
            result = "no answer (timeout)"
            while time.time() < deadline:
                for m in link.poll():
                    if m.get("t") == "welcome":
                        result = f"OK  {int((time.time() - t0) * 1000)} ms"
                        deadline = 0
                        break
                    if m.get("t") in ("error", "_disconnected"):
                        result = f"NG  {m.get('msg', 'disconnected')}"
                        deadline = 0
                        break
                time.sleep(0.05)
            link.close()
        except Exception as e:
            result = f"NG  {e}"
        _relay_check_result = result
        self.report({"INFO"} if result.startswith("OK") else {"ERROR"}, f"Relay: {result}")
        return {"FINISHED"}


class COLLAB_OT_reset_relay_url(bpy.types.Operator):
    bl_idname = "collab.reset_relay_url"
    bl_label = "Reset Relay URL"
    bl_description = "Put the preset relay URL back into the Relay URL field"

    def execute(self, context):
        global _relay_check_result
        _prefs().relay_url = DEFAULT_RELAY_URL
        _relay_check_result = ""
        self.report({"INFO"}, f"Relay URL reset to {DEFAULT_RELAY_URL}")
        return {"FINISHED"}


class _RoomUndoBase:
    """Ctrl+Z / Ctrl+Shift+Z while in a room: undo / redo only your own changes (Blender's undo would rewind
    everybody's). Outside a room, and in Edit / Sculpt Mode (undo local to the mesh), poll fails and the key
    falls through to Blender's normal undo."""

    @classmethod
    def poll(cls, context):
        return _session.active and _session.uid is not None and not context.mode.startswith(LOCAL_UNDO_MODES)


class COLLAB_OT_undo(_RoomUndoBase, bpy.types.Operator):
    bl_idname = "collab.undo"
    bl_label = "Undo (mine)"
    bl_description = "Undo your last change in the room. Other people's work is never touched"

    def execute(self, context):
        if not _session.undo_local():
            self.report({"INFO"}, _session.history.last_msg or "nothing to undo")
        return {"FINISHED"}


class COLLAB_OT_redo(_RoomUndoBase, bpy.types.Operator):
    bl_idname = "collab.redo"
    bl_label = "Redo (mine)"
    bl_description = "Redo your last undone change in the room"

    def execute(self, context):
        if not _session.redo_local():
            self.report({"INFO"}, _session.history.last_msg or "nothing to redo")
        return {"FINISHED"}


class COLLAB_OT_open_update_page(bpy.types.Operator):
    bl_idname = "collab.open_update_page"
    bl_label = "Update available"
    bl_description = "A newer FreebirdCollabo is on GitHub. Opens the Release page in your browser (nothing is installed)"

    def execute(self, context):
        bpy.ops.wm.url_open(url=update_check.latest_url)
        return {"FINISHED"}


_block_reason = None  # cached updater.blocked_reason(): "" = update allowed, text = why not


def _update_block_reason():
    global _block_reason
    if _block_reason is None:
        try:
            _block_reason = updater.blocked_reason() or ""
        except Exception as e:
            _block_reason = f"cannot check install folder ({e})"
    return _block_reason


def _can_update_here():
    """The newest release has the official zip and this install may be replaced in place."""
    return bool(update_check.latest_asset) and not _update_block_reason()


class COLLAB_OT_update_addon(bpy.types.Operator):
    bl_idname = "collab.update_addon"
    bl_label = "Update available"
    bl_description = (
        "Download the newest FreebirdCollabo from GitHub and replace this add-on. "
        "Starts only when you press it; restart Blender afterwards. Unavailable while in a room"
    )

    @classmethod
    def poll(cls, context):
        return not _session.active and updater.state not in ("running", "done") and _can_update_here()

    def execute(self, context):
        if updater.start(bl_info["version"]):
            if not bpy.app.timers.is_registered(_update_install_poll):
                bpy.app.timers.register(_update_install_poll, first_interval=0.5, persistent=True)
        return {"FINISHED"}


class COLLAB_OT_copy_code(bpy.types.Operator):
    bl_idname = "collab.copy_code"
    bl_label = "Copy Room Code"

    def execute(self, context):
        context.window_manager.clipboard = _session.room
        self.report({"INFO"}, f"Copied {_session.room}")
        return {"FINISHED"}


# ----------------------------------------------------------------------
# panel
# ----------------------------------------------------------------------
class COLLAB_PT_panel(bpy.types.Panel):
    bl_label = "COLLAB"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "COLLAB"

    @staticmethod
    def _draw_update(layout, in_room):
        if updater.state == "running":
            layout.label(text=f"Updating: {updater.message}", icon="TIME")
            return
        if updater.state == "done":
            row = layout.row()
            row.alert = True
            row.label(text=updater.message, icon="CHECKMARK")
            return
        if updater.state == "error":
            layout.label(text=updater.message[:80], icon="ERROR")
        if not update_check.latest_version:
            return
        row = layout.row()
        row.alert = True  # small red hint, like Freebird's own update notice
        text = f"Update available ({update_check.latest_tag})"
        if _can_update_here():
            row.enabled = not in_room
            row.operator("collab.update_addon", text=text, icon="IMPORT")
            if in_room:
                layout.label(text="Leave the room to update")
        else:  # no official zip in the release, or a development install: just open the Release page
            row.operator("collab.open_update_page", text=text, icon="URL")

    def draw(self, context):
        s = _session
        p = _prefs()
        layout = self.layout
        self._draw_update(layout, s.active)
        if not s.active:
            layout.operator("collab.create_room", icon="WORLD")
            box = layout.box()
            box.prop(context.window_manager, "collab_join_target", text="Code" if p.mode == "RELAY" else "Host IP")
            box.operator("collab.join_room", icon="LINKED")
            if s.error:
                layout.label(text=s.error, icon="ERROR")
            if p.mode == "RELAY":
                url = _relay_url(p)
                relay_txt = "default relay" if url == DEFAULT_RELAY_URL else url.split("://")[-1][:28]
            else:
                relay_txt = "direct (LAN)"
            layout.label(text=f"{p.display_name}  ·  {relay_txt}", icon="PREFERENCES")
            return

        box = layout.box()
        box.label(text=s.status, icon="WORLD" if s.role == "host" else "LINKED")
        if s.role == "host":
            row = box.row(align=True)
            row.label(text=f"Room: {s.room if p.mode == 'RELAY' else 'direct ' + str(p.direct_port)}")
            row.operator("collab.copy_code", text="", icon="COPYDOWN")
        for peer in s.peers.values():
            pres = peer.presence or {}
            tool = pres.get("tool", "")
            sel = ", ".join(pres.get("sel", [])[:2])
            line = f"{peer.name} ({peer.role}){'  VR' if pres.get('vr') else ''}"
            box.label(text=line, icon="USER")
            if tool or sel:
                box.label(text=f"    {tool}   sel: {sel}")
        if not s.peers:
            box.label(text="waiting for the other user...")
        n_undo, n_redo = s.history.counts()
        row = layout.row(align=True)
        row.operator("collab.undo", text=f"Undo mine ({n_undo})", icon="LOOP_BACK")
        row.operator("collab.redo", text=f"Redo ({n_redo})", icon="LOOP_FORWARDS")
        if s.history.last_msg:
            layout.label(text=s.history.last_msg[:60])
        layout.operator("collab.save_master", icon="FILE_TICK")
        layout.operator("collab.leave_room", icon="X")
        layout.label(text=f"tx {s.stats['tx']}  rx {s.stats['rx']}")


# ----------------------------------------------------------------------
# timer
# ----------------------------------------------------------------------
def _timer():
    try:
        _session.tick()
    except Exception as e:
        import traceback

        print(f"[collab] tick error: {e}\n{traceback.format_exc()}")
    return TICK_INTERVAL


def _redraw():
    try:
        for win in bpy.context.window_manager.windows:
            for area in win.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
    except Exception:
        pass


def _update_check_poll():
    """Redraw the panel once the background update check has finished, then stop."""
    if not update_check.done:
        return 1.0
    if update_check.latest_version:
        _redraw()
    return None


def _update_install_poll():
    """Keep the panel fresh while the update thread works, then stop."""
    _redraw()
    return 0.5 if updater.state == "running" else None


def _start_update_check():
    # skip headless runs (tests / render farms); FREEBIRD_COLLAB_UPDATE_URL forces it for debugging
    if bpy.app.background and not os.environ.get("FREEBIRD_COLLAB_UPDATE_URL"):
        return
    try:
        update_check.start(bl_info["version"])
        if not bpy.app.timers.is_registered(_update_check_poll):
            bpy.app.timers.register(_update_check_poll, first_interval=1.0, persistent=True)
    except Exception:
        pass  # never let the update notice break the add-on


classes = (
    COLLAB_Preferences,
    COLLAB_OT_create_room,
    COLLAB_OT_join_room,
    COLLAB_OT_leave_room,
    COLLAB_OT_save_master,
    COLLAB_OT_copy_code,
    COLLAB_OT_undo,
    COLLAB_OT_redo,
    COLLAB_OT_check_relay,
    COLLAB_OT_reset_relay_url,
    COLLAB_OT_open_update_page,
    COLLAB_OT_update_addon,
    COLLAB_PT_panel,
)


_keymaps = []


def _register_keymaps():
    kc = bpy.context.window_manager.keyconfigs.addon
    if kc is None:  # background mode
        return
    km = kc.keymaps.new(name="Screen", space_type="EMPTY")
    for idname, shift in (("collab.undo", False), ("collab.redo", True)):
        kmi = km.keymap_items.new(idname, "Z", "PRESS", ctrl=True, shift=shift)
        _keymaps.append((km, kmi))


def _unregister_keymaps():
    for km, kmi in _keymaps:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass
    _keymaps.clear()


def register():
    for c in classes:
        bpy.utils.register_class(c)
    _register_keymaps()
    bpy.types.WindowManager.collab_join_target = StringProperty(name="Room", default="")
    _session.on_change = _redraw
    presence.register(get_session)
    if not bpy.app.timers.is_registered(_timer):
        bpy.app.timers.register(_timer, first_interval=0.5, persistent=True)
    _start_update_check()


def unregister():
    for fn in (_update_check_poll, _update_install_poll):
        if bpy.app.timers.is_registered(fn):
            bpy.app.timers.unregister(fn)
    _session.leave()
    history.uninstall_freebird_hook()
    _unregister_keymaps()
    if bpy.app.timers.is_registered(_timer):
        bpy.app.timers.unregister(_timer)
    presence.unregister()
    del bpy.types.WindowManager.collab_join_target
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
