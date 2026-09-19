# SPDX-License-Identifier: GPL-2.0-or-later

bl_info = {
    "name": "Freebird Collaboration Layer",
    "author": "chikin + Claude",
    "version": (0, 6, 2),
    "blender": (4, 2, 0),
    "location": "3D View > Sidebar > COLLAB  (and Freebird XR menu via plugin)",
    "description": "Remote co-editing: shared room, host-authoritative scene, live head/hand/pointer/selection/tool presence",
    "category": "3D View",
}

import random

import bpy
from bpy.props import EnumProperty, FloatVectorProperty, IntProperty, StringProperty

from . import presence
from .session import CollabSession

TICK_INTERVAL = 1.0 / 60.0

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
        description="wss://<your-relay>  (internet)   |   ws://192.168.x.x:7788  (LAN relay)   |   tcp://host:7788",
        default="",
    )
    direct_port: IntProperty(name="Direct Port", default=7788, min=1, max=65535)

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "display_name")
        col.prop(self, "color")
        col.separator()
        col.prop(self, "mode")
        if self.mode == "RELAY":
            col.prop(self, "relay_url")
            row = col.row(align=True)
            row.operator("collab.check_relay", icon="PLUGIN")
            row.label(text=_relay_check_result or "Set once. Users only ever type the room code.")
        else:
            col.prop(self, "direct_port")


def _prefs():
    return bpy.context.preferences.addons[__name__].preferences


# ----------------------------------------------------------------------
# public API (used by the Freebird VR-menu plugin and by tests)
# ----------------------------------------------------------------------
def create_room():
    p = _prefs()
    return _session.create_room(p.mode, p.display_name, tuple(p.color), relay_url=p.relay_url, direct_port=p.direct_port)


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
    return _session.join_room("RELAY", p.display_name, tuple(p.color), code=target.strip().upper(), relay_url=p.relay_url)


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

        url = _prefs().relay_url
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

    def draw(self, context):
        s = _session
        p = _prefs()
        layout = self.layout
        if not s.active:
            layout.operator("collab.create_room", icon="WORLD")
            box = layout.box()
            box.prop(context.window_manager, "collab_join_target", text="Code" if p.mode == "RELAY" else "Host IP")
            box.operator("collab.join_room", icon="LINKED")
            if s.error:
                layout.label(text=s.error, icon="ERROR")
            relay_txt = (p.relay_url.split("://")[-1][:28] or "relay URL not set!") if p.mode == "RELAY" else "direct (LAN)"
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
        print(f"[collab] tick error: {e}")
    return TICK_INTERVAL


def _redraw():
    try:
        for win in bpy.context.window_manager.windows:
            for area in win.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
    except Exception:
        pass


classes = (
    COLLAB_Preferences,
    COLLAB_OT_create_room,
    COLLAB_OT_join_room,
    COLLAB_OT_leave_room,
    COLLAB_OT_save_master,
    COLLAB_OT_copy_code,
    COLLAB_OT_check_relay,
    COLLAB_PT_panel,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.WindowManager.collab_join_target = StringProperty(name="Room", default="")
    _session.on_change = _redraw
    presence.register(get_session)
    if not bpy.app.timers.is_registered(_timer):
        bpy.app.timers.register(_timer, first_interval=0.5, persistent=True)


def unregister():
    _session.leave()
    if bpy.app.timers.is_registered(_timer):
        bpy.app.timers.unregister(_timer)
    presence.unregister()
    del bpy.types.WindowManager.collab_join_target
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
