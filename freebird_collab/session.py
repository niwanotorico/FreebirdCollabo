# SPDX-License-Identifier: GPL-2.0-or-later
"""
CollabSession: the one object that owns the network link, the peer table,
and the Blender-facing sync logic.  Everything that touches bpy runs on the
main thread inside tick(), which the add-on drives from a bpy.app.timer.
In headless tests, tick() is simply called in a loop.
"""

import base64
import json
import os
import sys
import tempfile
import time

import bpy
from mathutils import Matrix

from . import hub as hubmod
from .link import Link
from . import object_data
from .protocol import make

EPS = 1e-4
PRESENCE_HZ = 20.0
XFORM_HZ = 30.0
PING_SECONDS = 20.0
DATA_HZ = 5.0  # max obj_data checks/sends per second (per object)
DATA_BUDGET_BPS = 1_500_000  # bytes/s per object: a 3 MB mesh is re-sent at most every 2 s while being edited
SWEEP_SECONDS = 2.0  # safety-net re-check of small datablocks (lights, cameras, empties, text, materials)
MAT_HZ = 5.0  # max material / material-slot checks per second
POSE_HZ = 15.0  # max pose-bone checks/sends per second (per armature, changed bones only)
SCENE_EDIT_TYPES = ("xform", "obj_add", "obj_data", "obj_del", "img", "img_need", "mat", "mat_ren", "mat_del", "obj_mats", "pose")
IGNORE_PREFIXES = ("FB-",)  # Freebird's own tracking empties
ADDON_VERSION = "0.9.0"  # exchanged in "ver" after join: material (0.7+) / pose (0.8+) / armature (0.9+) sync need it on BOTH sides
VER_TIMEOUT = 8.0  # seconds after a peer joins before "peer runs an old add-on" is logged
MAT_KEYS = {"c": "viewport color", "vm": "viewport metallic", "vr": "viewport roughness", "rm": "render method",
            "bc": "backface culling", "p": "bsdf", "tex": "base color texture", "tx": "textures", "gp": "gp style", "l": "unsynced links"}


def _log(msg):
    print(f"[collab] {msg}")


def _describe_mat(item):
    """Short human summary of a material state / delta for the log: 'bsdf: Base Color, Roughness; render method'."""
    parts = []
    for key, label in MAT_KEYS.items():
        if key not in item:
            continue
        if key in ("p", "tx"):
            names = list(item[key])
            parts.append(f"{label}: {', '.join(names) if len(names) <= 6 else f'{len(names)} inputs'}")
        else:
            parts.append(label)
    return "; ".join(parts) or "name only"


def _mat_to_list(m: Matrix):
    return [round(v, 5) for row in m for v in row]


def _list_to_mat(vals):
    return Matrix([vals[0:4], vals[4:8], vals[8:12], vals[12:16]])


def _world_matrix(ob):
    """Current world matrix without waiting for a depsgraph update (matrix_basis is
    always fresh; matrix_world lags until the next evaluation)."""
    return ob.matrix_basis if ob.parent is None else ob.matrix_world


def _set_world_matrix(ob, m):
    if ob.parent is None:
        ob.matrix_basis = m
    else:
        ob.matrix_world = m


def _depth(ob):
    d = 0
    while ob.parent is not None and d < 64:
        ob = ob.parent
        d += 1
    return d


def _mat_differs(a, b):
    if a is None or b is None:
        return True
    for x, y in zip(a, b):
        if abs(x - y) > EPS:
            return True
    return False


class Peer:
    __slots__ = ("uid", "name", "role", "color", "presence", "last_seen", "version", "joined_at", "ver_warned")

    def __init__(self, uid, name, role, color):
        self.uid = uid
        self.name = name
        self.role = role
        self.color = tuple(color) if color else (1.0, 1.0, 1.0)
        self.presence = None
        self.last_seen = time.time()
        self.version = None  # add-on version the peer reported ("ver"); None = not received (yet)
        self.joined_at = time.time()
        self.ver_warned = False


class CollabSession:
    def __init__(self):
        self.link = None
        self.hub = None  # only when hosting in direct mode
        self.role = None  # "host" | "guest"
        self.room = ""
        self.uid = None
        self.host_uid = None
        self.name = "user"
        self.color = (0.2, 0.6, 1.0)
        self.peers = {}  # uid -> Peer
        self.status = "offline"
        self.error = ""
        # sync state
        self.tracked = {}  # obj name -> last known matrix list
        self.known = set()  # object names present last tick
        self._last_presence_t = 0.0
        self._last_xform_t = 0.0
        self._last_ping_t = 0.0
        self._scene_loaded_from_host = False
        self._pending_scene = None
        self._deferred = []  # messages that arrived while a scene snapshot is pending
        # object-data sync (geometry / curve / light / camera / text / empty)
        self.data_digest = {}  # obj name -> digest of last sent or applied data
        self._dirty = set()  # object names flagged by the depsgraph handler
        self._dirty_data = set()  # (datablock type, name) flagged by the depsgraph handler
        self._next_data_t = {}  # obj name -> earliest time the next obj_data may go out
        self._pending_data = {}  # obj name -> payload waiting until the object leaves local Edit Mode
        self._pending_parent = {}  # child name -> parent name not yet present
        self._bones_sent = {}  # armature obj name -> bone list last sent (for the change summary in the log)
        self._sent_images = set()  # image ids already pushed to the room this session
        self._snapshot_images = set()  # image ids that were in the .blend snapshot (sent as host / received as guest)
        self._waiting_tex = {}  # image id -> {material names} waiting for that texture
        # material sync (Issue #2): datablock state by name, identity by session_uid so a rename is a rename
        self.mat_names = {}  # material uid -> name at the last check
        self.mat_digest = {}  # material name -> digest of last sent or applied state
        self.slot_state = {}  # obj name -> [[material name | None, link], ...] last sent or applied
        self._mat_state = {}  # material name -> last sent or applied state (deltas are computed against it)
        self._mat_rx = {}  # material name -> texture refs received (linked once a missing image arrives)
        self._mat_sent_t = {}  # material name -> when we last sent a change (host settles crossing edits)
        self._dirty_mats = set()  # material names flagged by the depsgraph handler
        self._dirty_trees = set()  # pointers of shader node trees flagged by the handler (owner resolved later)
        self._last_mat_t = 0.0
        self._last_mat_sweep_t = 0.0
        # pose sync (Pose Mode bone transforms, matched by bone name)
        self.pose_state = {}  # armature obj name -> {bone name: state} last sent or applied
        self._dirty_pose = set()  # armature object names flagged by the depsgraph handler
        self._dirty_arm = set()  # Armature datablock names flagged by the handler (owner resolved later)
        self._pending_pose = {}  # obj name -> {bone: state} waiting until the armature leaves local Edit Mode
        self._last_pose_t = 0.0
        self._last_pose_sweep_t = 0.0
        self._logged = set()
        self._last_data_t = 0.0
        self._last_sweep_t = 0.0
        self._handler = None
        self.stats = {"tx": 0, "rx": 0, "tx_bytes": 0, "tx_by_type": {}}
        self.on_change = None  # callback -> request UI redraw
        self.local_presence = None  # what we last sent (for HUD/tests)

    # ------------------------------------------------------------------
    # connection lifecycle
    # ------------------------------------------------------------------
    @property
    def active(self):
        return self.link is not None and self.link.connected

    def create_room(self, mode, name, color, relay_url="", direct_port=7788):
        self.leave()
        self.role = "host"
        self.name, self.color = name, tuple(color)
        try:
            if mode == "DIRECT":
                self.hub = hubmod.Hub("0.0.0.0", direct_port, fixed_room="LOCAL", log=_log)
                self.hub.start()
                self._connect(f"tcp://127.0.0.1:{self.hub.port}", room="")
            else:
                self._connect(relay_url, room="")
        except (OSError, ValueError, ConnectionError) as e:
            self._fail(f"create failed: {e}")
            return False
        self.status = "creating room..."
        return True

    def join_room(self, mode, name, color, code="", relay_url="", direct_host="", direct_port=7788):
        self.leave()
        self.role = "guest"
        self.name, self.color = name, tuple(color)
        try:
            if mode == "DIRECT":
                self._connect(f"tcp://{direct_host}:{direct_port}", room="LOCAL")
            else:
                if not code.strip():
                    raise ValueError("enter the room code")
                self._connect(relay_url, room=code)
        except (OSError, ValueError, ConnectionError) as e:
            self._fail(f"join failed: {e}")
            return False
        self.status = "joining..."
        return True

    def leave(self):
        if self.link:
            self.link.close()
        if self.hub:
            self.hub.stop()
        self.link = self.hub = None
        self.peers.clear()
        self.room = ""
        self.uid = self.host_uid = None
        self.role = None
        self.tracked.clear()
        self.known.clear()
        self.data_digest.clear()
        self._dirty.clear()
        self._dirty_data.clear()
        self._next_data_t.clear()
        self._pending_data.clear()
        self._pending_parent.clear()
        self._bones_sent.clear()
        self._sent_images.clear()
        self._snapshot_images.clear()
        self._waiting_tex.clear()
        self.mat_names.clear()
        self.mat_digest.clear()
        self.slot_state.clear()
        self._mat_rx.clear()
        self._mat_state.clear()
        self._mat_sent_t.clear()
        self._dirty_mats.clear()
        self._dirty_trees.clear()
        self.pose_state.clear()
        self._dirty_pose.clear()
        self._dirty_arm.clear()
        self._pending_pose.clear()
        self._logged.clear()
        self._scene_loaded_from_host = False
        self._pending_scene = None
        self._deferred = []
        self._uninstall_handler()
        self.status = "offline"
        self._notify()

    # -- depsgraph handler: tells us WHICH objects changed, so we never poll geometry --
    def _install_handler(self):
        if self._handler is not None:
            return
        from bpy.app.handlers import persistent

        session = self
        data_types = (bpy.types.Mesh, bpy.types.Curve, bpy.types.Light, bpy.types.Camera)
        grease_pencil_type = getattr(bpy.types, "GreasePencil", None)
        if grease_pencil_type is not None:
            data_types += (grease_pencil_type,)

        @persistent
        def _on_depsgraph(scene, depsgraph=None):
            if depsgraph is None:
                return
            try:
                for u in depsgraph.updates:
                    idb = u.id
                    if isinstance(idb, bpy.types.Object):
                        if u.is_updated_geometry:
                            session._dirty.add(idb.name)
                        if idb.type == "ARMATURE":  # a posed bone updates the object (transform / geometry)
                            session._dirty_pose.add(idb.name)
                    elif isinstance(idb, bpy.types.Armature):
                        session._dirty_arm.add(idb.name)  # pose check
                        session._dirty_data.add(("Armature", idb.name))  # bone structure check (leaving Edit Mode tags it)
                    elif isinstance(idb, data_types):
                        session._dirty_data.add((type(idb).__name__, idb.name))
                    elif isinstance(idb, bpy.types.Material):
                        session._dirty_mats.add(idb.name)
                    elif isinstance(idb, bpy.types.ShaderNodeTree):
                        session._dirty_trees.add(idb.original.as_pointer())
            except Exception:
                pass

        self._handler = _on_depsgraph
        bpy.app.handlers.depsgraph_update_post.append(self._handler)

    def _uninstall_handler(self):
        if self._handler is not None:
            try:
                bpy.app.handlers.depsgraph_update_post.remove(self._handler)
            except ValueError:
                pass
            self._handler = None

    def _connect(self, url, room):
        if not url.strip():
            raise ValueError("relay URL is empty (set it in the add-on preferences)")
        self.link = Link(url, log=_log)
        self.link.connect()
        self.link.send(make("hello", name=self.name, role=self.role, room=room, color=list(self.color)))

    def _fail(self, msg):
        _log(msg)
        self.error = msg
        self.leave()
        self.status = "error"
        self.error = msg

    def _send(self, msg):
        if self.link and self.link.connected:
            self.link.send(msg)
            self.stats["tx"] += 1
            if msg.get("t") in ("obj_data", "obj_add", "xform", "scene", "img", "mat", "mat_ren", "mat_del", "obj_mats", "pose"):
                n = len(json.dumps(msg, separators=(",", ":")))
                self.stats["tx_bytes"] += n
                bt = self.stats["tx_by_type"].setdefault(msg["t"], [0, 0])
                bt[0] += 1
                bt[1] += n

    def _notify(self):
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # main-thread tick
    # ------------------------------------------------------------------
    def tick(self):
        if not self.link:
            return
        for msg in self.link.poll():
            self.stats["rx"] += 1
            try:
                self._handle(msg)
            except Exception as e:  # never kill the timer
                import traceback

                _log(f"error handling {msg.get('t')}: {e}\n{traceback.format_exc()}")
        if self._pending_scene:
            scene, self._pending_scene = self._pending_scene, None
            self._apply_scene(scene)
            deferred, self._deferred = self._deferred, []
            for msg in deferred:  # replay edits that raced ahead of the snapshot
                try:
                    self._handle(msg)
                except Exception as e:
                    _log(f"error replaying {msg.get('t')}: {e}")
            # NOTE: no extra "resync" here on purpose: the hub delivers messages in order,
            # so everything the host changed before the snapshot is *in* the snapshot and
            # everything after it was deferred above. A late full-state push would clobber
            # edits the guest makes right after loading.
        if not self.active or self.uid is None:
            return
        now = time.time()
        if self._pending_data:
            self._retry_pending_data()
        if self._pending_pose:
            self._retry_pending_pose()
        # a guest must not push its own (about to be replaced) scene to the host
        if self.role != "guest" or self._scene_loaded_from_host:
            if now - self._last_xform_t >= 1.0 / XFORM_HZ:
                self._last_xform_t = now
                self._sync_objects()
            if now - self._last_data_t >= 1.0 / DATA_HZ:
                self._last_data_t = now
                self._sync_object_data(now)
            if now - self._last_mat_t >= 1.0 / MAT_HZ:
                self._last_mat_t = now
                try:
                    self._sync_materials(now)
                except Exception as e:  # never take presence / ping down with a material problem
                    import traceback

                    self._log_once(f"material sync error: {e}\n{traceback.format_exc()}")
            if now - self._last_pose_t >= 1.0 / POSE_HZ:
                self._last_pose_t = now
                try:
                    self._sync_poses(now)
                except Exception as e:  # a broken rig must not take presence / ping down
                    import traceback

                    self._log_once(f"pose sync error: {e}\n{traceback.format_exc()}")
        self._check_peer_versions(now)
        if now - self._last_presence_t >= 1.0 / PRESENCE_HZ:
            self._last_presence_t = now
            self._send_presence()
        if now - self._last_ping_t >= PING_SECONDS:  # keep proxies / tunnels from idling us out
            self._last_ping_t = now
            self._send(make("ping"))

    # ------------------------------------------------------------------
    # incoming
    # ------------------------------------------------------------------
    def _handle(self, msg):
        t = msg.get("t")
        if self._pending_scene is not None and t in SCENE_EDIT_TYPES:
            self._deferred.append(msg)
            return
        if t == "welcome":
            self.uid = msg["uid"]
            self.room = msg["room"]
            self.host_uid = msg.get("host_uid")
            for p in msg.get("peers", []):
                self.peers[p["uid"]] = Peer(p["uid"], p["name"], p["role"], p.get("color"))
            self.status = f"{'HOST' if self.role == 'host' else 'JOINED'} room {self.room}"
            self.error = ""
            self._snapshot_tracking()  # start tracking from the current scene
            self._install_handler()
            self._send(make("ver", v=ADDON_VERSION))  # tell everyone what we speak
            _log(f"{self.status} (add-on v{ADDON_VERSION}, {len(self.mat_names)} materials tracked, {len(self.peers)} peers)")
            self._notify()
        elif t == "error":
            self._fail(msg.get("msg", "error"))
            self._notify()
        elif t == "_disconnected":
            if self.link:
                self._fail("disconnected")
            self._notify()
        elif t == "peer_join":
            p = Peer(msg["uid"], msg["name"], msg["role"], msg.get("color"))
            self.peers[p.uid] = p
            if p.role == "host":
                self.host_uid = p.uid
            if self.role == "host":
                self._send_scene(p.uid)
            self._send(make("ver", to=p.uid, v=ADDON_VERSION))
            self._notify()
        elif t == "ver":
            p = self.peers.get(msg.get("from"))
            if p is not None:
                p.version = str(msg.get("v", "?"))
                _log(f"peer {p.name} runs add-on v{p.version}")
        elif t == "peer_leave":
            self.peers.pop(msg["uid"], None)
            self._notify()
        elif t == "scene":
            if self.role == "guest":
                self._pending_scene = msg  # apply outside of message loop
        elif t == "presence":
            p = self.peers.get(msg.get("from"))
            if p:
                p.presence = msg
                p.last_seen = time.time()
                self._notify()  # redraw desktop viewports (VR view redraws every frame anyway)
        elif t == "xform":
            self._apply_xform(msg.get("objs", {}))
        elif t == "obj_add":
            self._apply_obj_add(msg)
        elif t == "obj_data":
            self._apply_obj_data(msg)
        elif t == "img":
            try:
                object_data.store_image(msg["id"], base64.b64decode(msg["b64"]), msg.get("name", "image"), msg.get("ext", "png"))
                _log(f"received image {msg.get('name')} ({len(msg['b64']) * 3 // 4 // 1024} KB)")
            except Exception as e:
                _log(f"image {msg.get('name')} failed: {e}")
            self._sent_images.add(msg["id"])  # the room has it: never push it back when we edit that material
            self._apply_waiting_materials(msg["id"])
        elif t == "img_need":  # peer lacks a texture we referenced: look it up by content id and send it
            self._answer_img_need(msg)
        elif t == "obj_del":
            self._apply_obj_del(msg.get("names", []))
        elif t == "mat":
            self._apply_mat(msg)
        elif t == "mat_ren":
            self._apply_mat_ren(msg.get("old"), msg.get("new"))
        elif t == "mat_del":
            self._apply_mat_del(msg.get("names", []))
        elif t == "obj_mats":
            self._apply_obj_mats(msg)
        elif t == "pose":
            self._apply_pose(msg)
        elif t == "save":
            if self.role == "host":
                self.save_master()
        elif t == "resync":  # explicit full-state request (kept for tooling / future use)
            if self.role == "host" and self.tracked:
                self._send(make("xform", to=msg.get("from"), objs=dict(self.tracked)))
        elif t == "pong":
            pass

    # ------------------------------------------------------------------
    # scene snapshot (host -> guest, once per join)
    # ------------------------------------------------------------------
    def _send_scene(self, to_uid):
        tmp = os.path.join(tempfile.gettempdir(), f"collab_scene_{os.getpid()}.blend")
        try:
            bpy.ops.wm.save_as_mainfile(filepath=tmp, copy=True, compress=True)
            with open(tmp, "rb") as f:
                data = f.read()
        except Exception as e:
            _log(f"scene snapshot failed: {e}")
            return
        name = os.path.basename(bpy.data.filepath) or "untitled.blend"
        self._send(make("scene", to=to_uid, filename=name, blend_b64=base64.b64encode(data).decode("ascii")))
        try:  # remember which packed images the guest now has, so obj_add never re-ships them.
            # Only images that really went into the file count (0-user leftovers are dropped on save).
            with bpy.data.libraries.load(tmp) as (data_from, _data_to):
                saved = set(data_from.images)
            for img in bpy.data.images:
                if img.name not in saved or img.packed_file is None:
                    continue
                got = object_data.image_bytes(img)
                if got:
                    self._snapshot_images.add(object_data.image_id(got[0]))
        except Exception as e:
            _log(f"snapshot image index failed ({e}); images will be re-sent when referenced")
            self._snapshot_images.clear()
        _log(f"sent scene snapshot ({len(data) // 1024} KB) to {to_uid}")

    def _apply_scene(self, msg):
        data = base64.b64decode(msg["blend_b64"])
        tmp = os.path.join(tempfile.gettempdir(), "collab_host_" + msg.get("filename", "scene.blend"))
        with open(tmp, "wb") as f:
            f.write(data)
        _log(f"loading host scene ({len(data) // 1024} KB)")
        bpy.ops.wm.open_mainfile(filepath=tmp, load_ui=False)
        self._scene_loaded_from_host = True
        self._snapshot_tracking()
        for img in bpy.data.images:  # everything packed in the host's file is already on the host:
            if img.packed_file is not None:  # editing a GLB material must not upload its textures back
                got = object_data.image_bytes(img)
                if got:
                    self._snapshot_images.add(object_data.image_id(got[0]))
        self.status = f"JOINED room {self.room} (scene from host)"
        self._notify()

    # ------------------------------------------------------------------
    # object sync
    # ------------------------------------------------------------------
    def _iter_objects(self):
        try:
            in_scene = set(bpy.context.scene.objects.keys())
        except Exception:
            in_scene = None
        for ob in bpy.data.objects:
            if ob.name.startswith(IGNORE_PREFIXES):
                continue
            if ob.library:  # linked data, skip
                continue
            if in_scene is not None and ob.name not in in_scene:  # orphans / other scenes
                continue
            yield ob

    def _snapshot_tracking(self):
        self.tracked = {ob.name: _mat_to_list(_world_matrix(ob)) for ob in self._iter_objects()}
        self.known = set(self.tracked)
        self.data_digest = {}
        for ob in self._iter_objects():
            try:
                self.data_digest[ob.name] = object_data.quick_digest(ob)
            except Exception as e:
                _log(f"digest failed for {ob.name}: {e}")
        self._dirty.clear()
        self._dirty_data.clear()
        self._snapshot_materials()
        self._snapshot_poses()

    def _sync_objects(self):
        changed = {}
        current = set()
        new_objs = []
        for ob in self._iter_objects():
            current.add(ob.name)
            try:
                m = _mat_to_list(_world_matrix(ob))
            except Exception as e:
                self._log_once(f"xform read failed for {ob.name}: {e}")
                continue
            if ob.name not in self.known:
                new_objs.append((ob, m))
                continue
            if _mat_differs(self.tracked.get(ob.name), m):
                self.tracked[ob.name] = m
                changed[ob.name] = m
        # new objects (e.g. a GLB import creates many at once): parents first, one message each,
        # and one broken object must never block the others
        new_objs.sort(key=lambda t: _depth(t[0]))
        for ob, m in new_objs:
            try:
                self._send_obj_add(ob, m)
            except Exception as e:
                self._log_once(f"obj_add failed for {ob.name}: {e}")
            self.tracked[ob.name] = m
        removed = self.known - current
        if removed:
            self._send(make("obj_del", names=sorted(removed)))
            for n in removed:
                self.tracked.pop(n, None)
                self.data_digest.pop(n, None)
                self._next_data_t.pop(n, None)
                self.slot_state.pop(n, None)
                self.pose_state.pop(n, None)
                self._pending_pose.pop(n, None)
                self._bones_sent.pop(n, None)
        self.known = current
        if changed:
            self._send(make("xform", objs=changed))

    def _log_once(self, msg):
        if msg not in self._logged:
            self._logged.add(msg)
            _log(msg)

    def _send_obj_add(self, ob, m):
        payload = None
        try:
            payload = object_data.serialize(ob, limit=object_data.MAX_MESH_VERTS_ADD)
        except Exception as e:
            _log(f"serialize failed for {ob.name}: {e}")
        if payload is None:  # unsupported type or too big: show a placeholder so selection labels still make sense
            payload = {"type": "EMPTY", "data": {"display_type": "CUBE", "display_size": 0.2}, "placeholder": ob.type}
            self.data_digest[ob.name] = None
            self._log_once(f"{ob.name} ({ob.type}) sent as placeholder: unsupported type or over the live-sync size limit")
        else:
            self.data_digest[ob.name] = object_data.quick_digest(ob)
        try:
            mats = object_data.serialize_materials(ob)
            self._send_images(mats)
            self.slot_state[ob.name] = object_data.serialize_slots(ob)
            for slot in ob.material_slots:  # the state rides inside obj_add: no separate "mat" needed
                if slot.material is not None:
                    self._remember_material(slot.material)
        except Exception as e:
            self._log_once(f"materials for {ob.name}: {e}")
            mats = []
        parent = ob.parent.name if ob.parent else None
        self._send(make("obj_add", name=ob.name, type=ob.type, payload=payload, m=m, parent=parent, mats=mats))
        _log(f"sent obj_add {ob.name} ({ob.type}, {object_data.payload_bytes(payload) // 1024} KB{', parent ' + parent if parent else ''})")
        self._next_data_t[ob.name] = time.time() + max(1.0 / DATA_HZ, object_data.payload_bytes(payload) / DATA_BUDGET_BPS)

    def _apply_mats(self, ob, mats, from_uid):
        """obj_add: upsert the materials it carries and fill the slots."""
        try:
            missing = object_data.apply_materials(ob, mats)
        except Exception as e:
            _log(f"materials for {ob.name}: {e}")
            return
        for item in mats or []:
            if item:
                self._note_textures(item)
                mat = bpy.data.materials.get(item["n"])
                if mat is not None:
                    self._remember_material(mat)
        self._want_textures(mats, missing, from_uid, ob.name)
        self.slot_state[ob.name] = object_data.serialize_slots(ob)

    def _want_textures(self, items, missing, from_uid, what):
        missing = set(missing)
        for item in items or []:
            for tex in object_data.material_textures(item):
                if tex["id"] not in missing:
                    continue
                waiting = self._waiting_tex.setdefault(tex["id"], set())
                if not waiting:  # first time we miss this id: ask the sender for it
                    self._send(make("img_need", to=from_uid, id=tex["id"]))
                    _log(f"texture {tex['id'][:8]} for {what} not here yet, requested from {from_uid}")
                waiting.add(item["n"])

    def _apply_waiting_materials(self, img_id):
        for mat_name in self._waiting_tex.pop(img_id, ()):
            refs = {k: t for k, t in self._mat_rx.get(mat_name, {}).items() if t["id"] == img_id}
            if not refs or mat_name not in bpy.data.materials:
                continue  # a newer state no longer uses this image
            try:
                mat, _missing = object_data.apply_material({"n": mat_name, "tx": refs}, textures_only=True)
                self._remember_material(mat)
            except Exception as e:
                _log(f"texture for material {mat_name}: {e}")

    def _answer_img_need(self, msg):
        img_id = msg.get("id")
        img = object_data.find_image(img_id)
        got = object_data.image_bytes(img) if img else None
        if not got or object_data.image_id(got[0]) != img_id:
            _log(f"img_need {img_id[:8]}: image not found here")
            return
        self._send(make("img", to=msg.get("from"), id=img_id, name=img.name, ext=got[1], b64=base64.b64encode(got[0]).decode("ascii")))
        _log(f"re-sent image {img.name} ({len(got[0]) // 1024} KB) on request")

    def _send_images(self, mats):
        """Push Base Color textures referenced by mats, each image at most once per session."""
        for img_id, data, name, ext in object_data.images_for_materials(mats):
            if img_id in self._sent_images or self._image_in_snapshot(img_id):
                continue
            self._sent_images.add(img_id)
            self._send(make("img", id=img_id, name=name, ext=ext, b64=base64.b64encode(data).decode("ascii")))
            _log(f"sent image {name} ({len(data) // 1024} KB, {img_id[:8]})")

    def _image_in_snapshot(self, img_id):
        """Images that travelled inside the .blend snapshot (host: sent, guest: received) need no re-send.
        With 3+ peers a later joiner may lack one; img_need recovers that."""
        return img_id in self._snapshot_images

    def _check_peer_versions(self, now):
        for p in self.peers.values():
            if p.version is None and not p.ver_warned and now - p.joined_at > VER_TIMEOUT:
                p.ver_warned = True
                _log(f"WARNING: peer {p.name} sent no add-on version in {VER_TIMEOUT:.0f}s: they run an add-on older "
                     f"than 0.7.0. Material sync needs v0.7+, pose sync v0.8+ and armature / bone sync v0.9+ on BOTH PCs; "
                     f"transform / mesh still work")

    # ------------------------------------------------------------------
    # material sync (Issue #2): create / delete / rename, Principled BSDF values, image textures, slots
    # ------------------------------------------------------------------
    @staticmethod
    def _iter_materials():
        for mat in bpy.data.materials:
            if not mat.library:
                yield mat

    def _remember_material(self, mat):
        """Record the current local state as "in sync" (echo suppression after send or apply)."""
        try:
            self.mat_names[object_data.material_uid(mat)] = mat.name
            item = object_data.serialize_material(mat)
            self._mat_state[mat.name] = item
            self.mat_digest[mat.name] = object_data.state_digest(item)
        except Exception as e:
            self._log_once(f"material digest failed for {mat.name}: {e}")
        self._dirty_mats.discard(mat.name)

    def _snapshot_materials(self):
        self.mat_names = {}
        self.mat_digest = {}
        self._mat_state = {}
        for mat in self._iter_materials():
            self._remember_material(mat)
        self.slot_state = {}
        for ob in self._iter_objects():
            slots = object_data.serialize_slots(ob)
            if slots is not None:
                self.slot_state[ob.name] = slots
        self._dirty_mats.clear()
        self._dirty_trees.clear()

    def _sync_materials(self, now):
        sweep = now - self._last_mat_sweep_t >= SWEEP_SECONDS  # safety net: edits that never reach the depsgraph
        if sweep:
            self._last_mat_sweep_t = now
        dirty, self._dirty_mats = self._dirty_mats, set()
        trees, self._dirty_trees = self._dirty_trees, set()
        current = {}
        for mat in self._iter_materials():
            uid = object_data.material_uid(mat)
            current[uid] = mat.name
            old = self.mat_names.get(uid)
            if old is not None and old != mat.name:
                self._renamed_material(old, mat.name)
                self._send(make("mat_ren", old=old, new=mat.name))
                _log(f"sent material rename {old} -> {mat.name}")
            if old is None or sweep or mat.name in dirty or (
                    trees and mat.node_tree is not None and mat.node_tree.as_pointer() in trees):
                try:
                    self._send_material_if_changed(mat)
                except Exception as e:
                    self._log_once(f"material sync failed for {mat.name}: {e}")
        removed = [name for uid, name in self.mat_names.items() if uid not in current and name not in current.values()]
        if removed:
            self._send(make("mat_del", names=sorted(removed)))
            _log(f"sent material delete {', '.join(sorted(removed))}")
            for name in removed:
                self._forget_material(name)
        self.mat_names = current
        # slots last: the materials they name were sent above, and the hub keeps the order
        for ob in self._iter_objects():
            if ob.name not in self.known:
                continue  # not announced yet; obj_add will carry its materials
            slots = object_data.serialize_slots(ob)
            if slots is not None and slots != self.slot_state.get(ob.name):
                self.slot_state[ob.name] = slots
                self._send(make("obj_mats", name=ob.name, slots=slots))
                _log(f"sent obj_mats {ob.name} {[n for n, _ in slots]}")

    def _send_material_if_changed(self, mat, full=False):
        """Send what changed since the last sent / applied state (only the changed Principled inputs,
        so two people tuning different sliders of one material do not overwrite each other)."""
        item = object_data.serialize_material(mat)
        digest = object_data.state_digest(item)
        if digest == self.mat_digest.get(mat.name) and not full:
            return False
        prev = None if full else self._mat_state.get(mat.name)
        self.mat_digest[mat.name] = digest
        self._mat_state[mat.name] = item
        out = object_data.material_delta(prev, item) if prev else item
        if len(out) == 1:
            _log(f"material {mat.name} changed only in unsynced parts ({', '.join(item.get('l') or [])}): nothing sent")
            return False
        self._send_images([out])
        self._send(make("mat", mat=out))
        self._mat_sent_t[mat.name] = time.time()
        _log(f"sent mat {mat.name} ({_describe_mat(out)}{', full' if prev is None else ''})")
        return True

    def _forget_material(self, name):
        for table in (self.mat_digest, self._mat_state, self._mat_rx, self._mat_sent_t):
            table.pop(name, None)

    def _renamed_material(self, old, new):
        for table in (self.mat_digest, self._mat_rx, self._mat_sent_t):
            if old in table:
                table[new] = table.pop(old)
        if old in self._mat_state:
            self._mat_state[new] = dict(self._mat_state.pop(old), n=new)
            self.mat_digest[new] = object_data.state_digest(self._mat_state[new])
        for waiting in self._waiting_tex.values():
            if old in waiting:
                waiting.discard(old)
                waiting.add(new)
        for slots in self.slot_state.values():
            for slot in slots:
                if slot[0] == old:
                    slot[0] = new

    def _apply_mat(self, msg):
        item = msg.get("mat")
        if not item or not item.get("n"):
            return
        name = item["n"]
        local = bpy.data.materials.get(name)
        if local is not None and name in self.mat_digest and not local.library:
            try:  # a local edit we have not sent yet must go out first, or the remote state would bury it
                self._send_material_if_changed(local)
            except Exception as e:
                self._log_once(f"material sync failed for {name}: {e}")
        try:
            mat, missing = object_data.apply_material(item)
        except Exception as e:
            import traceback

            _log(f"apply mat {name} FAILED: {e}\n{traceback.format_exc()}")
            return
        _log(f"applied mat {name} from {msg.get('from')} ({_describe_mat(item)}{', waiting for texture' if missing else ''})")
        self._note_textures(item)
        self._remember_material(mat)
        self._want_textures([item], missing, msg.get("from"), f"material {mat.name}")
        if self.role == "host" and time.time() - self._mat_sent_t.get(name, 0.0) < 1.0:
            # both sides changed this material at the same moment: values crossed on the wire and would end up
            # swapped. The host's result (remote applied on top of its own) is the one everybody keeps.
            self._send_material_if_changed(mat, full=True)

    def _note_textures(self, item):
        refs = self._mat_rx.setdefault(item["n"], {})
        for ident in item.get("p") or {}:  # that input is a plain value now
            refs.pop(ident, None)
        if item.get("tex"):
            refs["Base Color"] = item["tex"]
        refs.update(item.get("tx") or {})

    def _apply_mat_ren(self, old, new):
        if not old or not new or old == new:
            return
        mat = bpy.data.materials.get(old)
        if mat is None:
            _log(f"mat_ren {old} -> {new}: {old} not here; its state will arrive under the new name")
            return
        squatter = bpy.data.materials.get(new)
        if squatter is not None and squatter != mat:  # remote naming authority, same as objects
            mat.user_remap(squatter)
            self.mat_names.pop(object_data.material_uid(mat), None)
            self._forget_material(old)
            bpy.data.materials.remove(mat)
            mat = squatter
        else:
            mat.name = new
        self._renamed_material(old, new)
        self._remember_material(mat)
        _log(f"applied mat_ren {old} -> {mat.name}")

    def _apply_mat_del(self, names):
        for name in names:
            mat = bpy.data.materials.get(name)
            if mat is not None and not mat.library:
                self.mat_names.pop(object_data.material_uid(mat), None)
                bpy.data.materials.remove(mat)
            self._forget_material(name)
        _log(f"applied mat_del {', '.join(names)}")
        for ob in self._iter_objects():  # slots that pointed at it are now empty on both sides
            if ob.name in self.slot_state:
                self.slot_state[ob.name] = object_data.serialize_slots(ob)

    def _apply_obj_mats(self, msg):
        ob = bpy.data.objects.get(msg.get("name", ""))
        if ob is None:
            _log(f"obj_mats for unknown object {msg.get('name')}: ignored")
            return
        try:
            object_data.apply_slots(ob, msg.get("slots"))
        except Exception as e:
            _log(f"apply obj_mats {ob.name} FAILED: {e}")
            return
        _log(f"applied obj_mats {ob.name} {[n for n, _ in msg.get('slots') or []]}")
        self.slot_state[ob.name] = object_data.serialize_slots(ob)
        for slot in ob.material_slots:  # placeholders created for unknown names must not echo back
            if slot.material is not None and object_data.material_uid(slot.material) not in self.mat_names:
                self._remember_material(slot.material)


    # ------------------------------------------------------------------
    # pose sync: Pose Mode bone Location / Rotation / Scale, matched by bone name
    # ------------------------------------------------------------------
    def _iter_armatures(self):
        for ob in self._iter_objects():
            if ob.type == "ARMATURE" and ob.pose is not None:
                yield ob

    def _remember_pose(self, ob, bones=None):
        """Record the current local pose (or just `bones` of it) as "in sync" (echo suppression)."""
        cur = object_data.serialize_pose(ob)
        if cur is None:
            return
        if bones is None:
            self.pose_state[ob.name] = cur
        else:
            state = self.pose_state.setdefault(ob.name, {})
            for name in bones:
                if name in cur:
                    state[name] = cur[name]
        self._dirty_pose.discard(ob.name)

    def _snapshot_poses(self):
        self.pose_state = {}
        for ob in self._iter_armatures():
            try:
                self._remember_pose(ob)
            except Exception as e:
                _log(f"pose digest failed for {ob.name}: {e}")
        self._dirty_pose.clear()
        self._dirty_arm.clear()
        self._pending_pose.clear()

    def _sync_poses(self, now):
        sweep = now - self._last_pose_sweep_t >= SWEEP_SECONDS  # safety net: edits that never reach the depsgraph
        if sweep:
            self._last_pose_sweep_t = now
        dirty, self._dirty_pose = self._dirty_pose, set()
        arms, self._dirty_arm = self._dirty_arm, set()
        for ob in self._iter_armatures():
            if ob.name not in self.known:
                continue  # not announced yet
            if not (sweep or ob.mode == "POSE" or ob.name in dirty or ob.name not in self.pose_state
                    or (arms and ob.data is not None and ob.data.name in arms)):
                continue
            if ob.name in self._pending_pose:
                continue  # remote state waits for local Edit Mode to end; do not push ours over it
            try:
                self._send_pose_if_changed(ob)
            except Exception as e:
                self._log_once(f"pose sync failed for {ob.name}: {e}")

    def _send_pose_if_changed(self, ob):
        cur = object_data.serialize_pose(ob)
        if cur is None:
            return False
        prev = self.pose_state.get(ob.name)
        delta = object_data.pose_delta(prev, cur)
        if delta and prev is not None and ob.mode != "EDIT":
            # a bone that was just added / renamed here must reach the peer as structure (obj_data) BEFORE its pose,
            # or the peer skips the pose as "bone not in this rig". Throttled structure = pose waits for the next tick
            if self._sync_one_object_data(ob, time.time()) == "wait":
                return False
        self.pose_state[ob.name] = cur
        if not delta or prev is None:  # first sight of a rig = baseline only (the peer has the same rig from the snapshot)
            return False
        self._send(make("pose", name=ob.name, bones=delta))
        names = sorted(delta)
        _log(f"sent pose {ob.name} ({len(names)} bone{'s' if len(names) != 1 else ''}: "
             f"{', '.join(names) if len(names) <= 6 else ', '.join(names[:6]) + ', ...'})")
        return True

    def _apply_pose(self, msg):
        name = msg.get("name", "")
        bones = msg.get("bones")
        ob = bpy.data.objects.get(name)
        if ob is None or not isinstance(bones, dict):
            self._log_once(f"pose for unknown object {name}: ignored")
            return
        if ob.type != "ARMATURE":
            self._log_once(f"pose for {name}: not an armature here ({ob.type}), ignored")
            return
        if ob.mode == "EDIT":  # bones are being restructured locally: apply once Edit Mode ends
            self._pending_pose.setdefault(name, {}).update(bones)
            return
        self._apply_pose_now(ob, bones, msg.get("from"))

    def _apply_pose_now(self, ob, bones, from_uid):
        try:
            applied, skipped = object_data.apply_pose(ob, bones)
        except Exception as e:
            import traceback

            _log(f"apply pose {ob.name} FAILED: {e}\n{traceback.format_exc()}")
            return
        self._remember_pose(ob, applied)  # echo suppression: what we just wrote is "in sync"
        _log(f"applied pose {ob.name} from {from_uid} ({len(applied)} bone{'s' if len(applied) != 1 else ''})")
        if skipped:
            self._log_once(f"pose {ob.name}: bones not in this rig ignored: {', '.join(sorted(skipped))}")

    def _retry_pending_pose(self):
        for name in list(self._pending_pose):
            ob = bpy.data.objects.get(name)
            if ob is None or ob.type != "ARMATURE":
                del self._pending_pose[name]
            elif ob.mode != "EDIT":
                self._apply_pose_now(ob, self._pending_pose.pop(name), "peer")

    def _collect_dirty(self, now):
        """Names of objects whose data may have changed since we last sent/applied it."""
        names = set(self._dirty)
        self._dirty.clear()
        if self._dirty_data:
            wanted = self._dirty_data
            self._dirty_data = set()
            for ob in self._iter_objects():
                d = ob.data
                if d is not None and (type(d).__name__, d.name) in wanted:
                    names.add(ob.name)
        # objects being edited locally: bmesh edits are cheap to re-check and must not be missed
        for ob in bpy.data.objects:
            if ob.mode == "EDIT" and not ob.name.startswith(IGNORE_PREFIXES):
                names.add(ob.name)
            elif ob.type == "GREASEPENCIL" and ob.mode in (
                "PAINT_GREASE_PENCIL", "SCULPT_GREASE_PENCIL", "VERTEX_GREASE_PENCIL", "WEIGHT_GREASE_PENCIL"
            ) and not ob.name.startswith(IGNORE_PREFIXES):
                names.add(ob.name)
        # safety-net sweep of the tiny datablocks (a handler miss on a lamp slider must not stick forever)
        if now - self._last_sweep_t >= SWEEP_SECONDS:
            self._last_sweep_t = now
            for ob in self._iter_objects():
                if ob.type in ("LIGHT", "CAMERA", "EMPTY", "FONT", "ARMATURE"):  # armature: bone structure
                    names.add(ob.name)
        return names

    def _sync_object_data(self, now):
        names = self._collect_dirty(now)
        if not names:
            return
        for name in names:
            if name not in self.known:
                continue
            ob = bpy.data.objects.get(name)
            if ob is not None:
                self._sync_one_object_data(ob, now)

    def _sync_one_object_data(self, ob, now):
        """Send obj_data for one object if its data changed since the last send / apply.
        Returns "same" | "sent" | "wait" (pending remote state or throttled: try again next round) | "skip"."""
        name = ob.name
        if name in self._pending_data:
            return "wait"
        if now < self._next_data_t.get(name, 0.0):
            self._dirty.add(name)  # throttled: check again next round (coalesces rapid edits)
            return "wait"
        try:
            digest = object_data.quick_digest(ob)
        except Exception as e:
            _log(f"digest failed for {name}: {e}")
            return "skip"
        if digest is None or digest == self.data_digest.get(name):
            return "same"
        try:
            payload = object_data.serialize(ob)
        except Exception as e:
            self._log_once(f"serialize failed for {name}: {e}")
            return "skip"
        if payload is None:
            return "skip"
        if ob.type == "ARMATURE":
            _log(f"sent armature {name} ({object_data.armature_delta_summary(self._bones_sent.get(name), payload['data']['bones'])})")
            self._bones_sent[name] = payload["data"]["bones"]
            # bones that are new here start as "in sync" at their current pose: no identity-pose message follows,
            # and a pose they already have (rare: posed before this send) is in the delta the caller computed
            cur_pose = object_data.serialize_pose(ob) or {}
            state = self.pose_state.setdefault(name, {})
            for bone, st in cur_pose.items():
                state.setdefault(bone, st)
        self.data_digest[name] = digest
        self._send(make("obj_data", name=name, payload=payload))
        self._next_data_t[name] = now + max(1.0 / DATA_HZ, object_data.payload_bytes(payload) / DATA_BUDGET_BPS)
        return "sent"

    def _apply_obj_data(self, msg):
        name = msg["name"]
        ob = bpy.data.objects.get(name)
        if ob is None:
            return
        payload = msg["payload"]
        if self._editing_data_locally(ob):
            self._pending_data[name] = payload  # apply once the local user leaves the data-editing mode
            return
        self._apply_payload(ob, payload)

    @staticmethod
    def _editing_data_locally(ob):
        if ob.type == "MESH":
            return ob.mode == "EDIT"
        if ob.type == "GREASEPENCIL":
            return ob.mode != "OBJECT"
        if ob.type == "ARMATURE":  # bones can only be written from Edit Mode: wait until we can borrow the view layer
            return ob.mode == "EDIT" or not object_data.armature_editable()
        return False

    def _apply_payload(self, ob, payload):
        try:
            if not object_data.apply(ob, payload):
                return
        except Exception as e:
            _log(f"apply data failed for {ob.name}: {e}")
            return
        self.data_digest[ob.name] = object_data.quick_digest(ob)  # echo suppression
        self._dirty.discard(ob.name)
        if ob.type == "ARMATURE":
            bones = payload.get("data", {}).get("bones") or []
            _log(f"applied armature {ob.name} ({object_data.armature_delta_summary(self._bones_sent.get(ob.name), bones)}, "
                 f"{len(bones)} bones)")
            self._bones_sent[ob.name] = bones
            self._remember_pose(ob)  # new bones start "in sync": their identity pose must not be echoed at the sender
            self._dirty_pose.discard(ob.name)

    def _retry_pending_data(self):
        for name in list(self._pending_data):
            ob = bpy.data.objects.get(name)
            if ob is None:
                del self._pending_data[name]
            elif not self._editing_data_locally(ob):
                payload = self._pending_data.pop(name)
                if ob.type == "ARMATURE" and self._changed_locally(ob):
                    # both sides restructured the same rig: the edit that finished last (ours) wins and goes out now,
                    # instead of the remote state silently burying it (the peer applies ours, so both end up equal)
                    _log(f"armature {name}: local Edit Mode changes win over the structure received meanwhile")
                    self._dirty.add(name)
                    continue
                self._apply_payload(ob, payload)

    def _changed_locally(self, ob):
        try:
            digest = object_data.quick_digest(ob)
        except Exception:
            return False
        return ob.name in self.data_digest and digest is not None and digest != self.data_digest.get(ob.name)

    def _apply_xform(self, objs):
        for name, vals in objs.items():
            ob = bpy.data.objects.get(name)
            if ob is None or len(vals) != 16:
                continue
            _set_world_matrix(ob, _list_to_mat(vals))
            self.tracked[name] = vals  # echo suppression

    def _apply_obj_add(self, msg):
        name = msg["name"]
        payload = msg.get("payload")
        if payload is None:  # v0.3 wire format (mesh only) — kept so mixed versions don't crash
            mesh = msg.get("mesh")
            payload = {"type": "MESH", "data": {"v": mesh["v"], "e": [], "f": mesh["f"]}} if mesh else \
                {"type": "EMPTY", "data": {"display_type": "CUBE", "display_size": 0.2}}
        ob = bpy.data.objects.get(name)
        in_scene = ob is not None and name in bpy.context.scene.objects
        if ob is not None and not in_scene:
            # an orphan (deleted earlier, or left over from a previous import) squats on the name: move it aside
            ob.name = name + ".orphan"
            ob = None
        if ob is not None:  # already here (e.g. both sides imported the same asset): upsert instead of ignoring
            if ob.type == payload["type"] and not payload.get("placeholder"):
                if ob.type == "ARMATURE":
                    self._apply_obj_data({"name": name, "payload": payload})  # may wait for local Edit Mode to end
                else:
                    self._apply_payload(ob, payload)
        else:
            try:
                ob = object_data.new_object(name, payload)
            except Exception as e:
                _log(f"cannot create {name}: {e}")
                return
            bpy.context.scene.collection.objects.link(ob)
            if ob.name != name:  # name collision -> keep remote naming authority
                ob.name = name
            if ob.type == "ARMATURE":  # bones are written in Edit Mode, possible only now that the object is in the scene
                self._apply_obj_data({"name": ob.name, "payload": payload})
        self._apply_mats(ob, msg.get("mats"), msg.get("from"))
        parent = msg.get("parent")
        if parent:
            pob = bpy.data.objects.get(parent)
            if pob is not None and parent in bpy.context.scene.objects:
                ob.parent = pob
            else:
                self._pending_parent[name] = parent  # parent arrives in a later message
        _set_world_matrix(ob, _list_to_mat(msg["m"]))
        self.tracked[name] = msg["m"]
        self.known.add(name)
        self.data_digest[name] = None if payload.get("placeholder") else object_data.quick_digest(ob)
        self._dirty.discard(name)
        self._resolve_pending_parents()

    def _resolve_pending_parents(self):
        for child, parent in list(self._pending_parent.items()):
            cob, pob = bpy.data.objects.get(child), bpy.data.objects.get(parent)
            if cob is None:
                del self._pending_parent[child]
            elif pob is not None:
                world = cob.matrix_world.copy()
                cob.parent = pob
                cob.matrix_world = world
                self.tracked[child] = _mat_to_list(_world_matrix(cob))
                del self._pending_parent[child]

    def _apply_obj_del(self, names):
        for name in names:
            ob = bpy.data.objects.get(name)
            if ob:
                bpy.data.objects.remove(ob, do_unlink=True)
            self.tracked.pop(name, None)
            self.data_digest.pop(name, None)
            self._pending_data.pop(name, None)
            self.slot_state.pop(name, None)
            self.pose_state.pop(name, None)
            self._pending_pose.pop(name, None)
            self._bones_sent.pop(name, None)
            self.known.discard(name)

    # ------------------------------------------------------------------
    # presence (head / hands / ray / selection / tool)
    # ------------------------------------------------------------------
    def _send_presence(self):
        pres = sample_local_presence()
        pres.update(name=self.name, color=list(self.color))
        self.local_presence = pres
        self._send(make("presence", **pres))

    def request_save(self):
        if self.role == "host":
            self.save_master()
        else:
            self._send(make("save"))

    def save_master(self):
        if bpy.data.filepath:
            bpy.ops.wm.save_mainfile()
            _log(f"master scene saved: {bpy.data.filepath}")
            return True
        _log("master scene has no filepath yet; save it once from the host UI")
        return False


# ----------------------------------------------------------------------
# local state sampling helpers (also used by the HUD)
# ----------------------------------------------------------------------
def _pose(loc, rot):
    return [round(loc[0], 4), round(loc[1], 4), round(loc[2], 4), round(rot[0], 4), round(rot[1], 4), round(rot[2], 4), round(rot[3], 4)]


def sample_xr():
    """Returns (head, hands) from Blender's XR session, or (None, None) when not in VR."""
    try:
        wm = bpy.context.window_manager
        st = wm.xr_session_state
        if st is None or not st.is_running(bpy.context):
            return None, None
        head = _pose(st.viewer_pose_location, st.viewer_pose_rotation)
        hands = {}
        for key, idx in (("L", 0), ("R", 1)):
            loc = st.controller_aim_location_get(bpy.context, idx)
            rot = st.controller_aim_rotation_get(bpy.context, idx)
            hands[key] = _pose(loc, rot)
        return head, hands
    except Exception:
        return None, None


def sample_tool():
    fb = sys.modules.get("freebird")
    if fb is not None:
        try:
            tool = fb.tools.active_tool
            if tool:
                return f"fb:{tool}"
        except Exception:
            pass
    try:
        ctx = bpy.context
        mode = ctx.mode
        tool = ctx.workspace.tools.from_space_view3d_mode(mode, create=False)
        return tool.idname.replace("builtin.", "") if tool else ""
    except Exception:
        return ""


def sample_selection():
    try:
        vl = bpy.context.view_layer
        return [ob.name for ob in vl.objects if ob.select_get(view_layer=vl) and not ob.name.startswith(IGNORE_PREFIXES)]
    except Exception:
        return []


def sample_local_presence():
    head, hands = sample_xr()
    try:
        mode = bpy.context.mode
    except Exception:
        mode = "OBJECT"
    return {
        "vr": head is not None,
        "head": head,
        "hands": hands,
        "tool": sample_tool(),
        "sel": sample_selection(),
        "mode": mode,
    }
