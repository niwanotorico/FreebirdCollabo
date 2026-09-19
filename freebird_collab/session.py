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
SWEEP_SECONDS = 2.0  # safety-net re-check of small datablocks (lights, cameras, empties, text)
IGNORE_PREFIXES = ("FB-",)  # Freebird's own tracking empties


def _log(msg):
    print(f"[collab] {msg}")


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
    __slots__ = ("uid", "name", "role", "color", "presence", "last_seen")

    def __init__(self, uid, name, role, color):
        self.uid = uid
        self.name = name
        self.role = role
        self.color = tuple(color) if color else (1.0, 1.0, 1.0)
        self.presence = None
        self.last_seen = time.time()


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
        self._sent_images = set()  # image ids already pushed to the room this session
        self._snapshot_images = set()  # (host) image ids that were in the .blend sent to guests
        self._waiting_tex = {}  # image id -> [(object name, mats)] waiting for that texture
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
        self._sent_images.clear()
        self._snapshot_images.clear()
        self._waiting_tex.clear()
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
                    elif isinstance(idb, (bpy.types.Mesh, bpy.types.Curve, bpy.types.Light, bpy.types.Camera)):
                        session._dirty_data.add((type(idb).__name__, idb.name))
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
            if msg.get("t") in ("obj_data", "obj_add", "xform", "scene", "img"):
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
        # a guest must not push its own (about to be replaced) scene to the host
        if self.role != "guest" or self._scene_loaded_from_host:
            if now - self._last_xform_t >= 1.0 / XFORM_HZ:
                self._last_xform_t = now
                self._sync_objects()
            if now - self._last_data_t >= 1.0 / DATA_HZ:
                self._last_data_t = now
                self._sync_object_data(now)
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
        if self._pending_scene is not None and t in ("xform", "obj_add", "obj_data", "obj_del", "img", "img_need"):
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
            _log(self.status)
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
            self._notify()
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
            self._apply_waiting_materials(msg["id"])
        elif t == "img_need":  # peer lacks a texture we referenced: look it up by content id and send it
            self._answer_img_need(msg)
        elif t == "obj_del":
            self._apply_obj_del(msg.get("names", []))
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
            self._log_once(f"{ob.name} ({ob.type}) sent as placeholder: unsupported type or over {object_data.MAX_MESH_VERTS_ADD} verts")
        else:
            self.data_digest[ob.name] = object_data.quick_digest(ob)
        try:
            mats = object_data.serialize_materials(ob)
            self._send_images(mats)
        except Exception as e:
            self._log_once(f"materials for {ob.name}: {e}")
            mats = []
        parent = ob.parent.name if ob.parent else None
        self._send(make("obj_add", name=ob.name, type=ob.type, payload=payload, m=m, parent=parent, mats=mats))
        _log(f"sent obj_add {ob.name} ({ob.type}, {object_data.payload_bytes(payload) // 1024} KB{', parent ' + parent if parent else ''})")
        self._next_data_t[ob.name] = time.time() + max(1.0 / DATA_HZ, object_data.payload_bytes(payload) / DATA_BUDGET_BPS)

    def _apply_mats(self, ob, mats, from_uid):
        try:
            missing = object_data.apply_materials(ob, mats)
        except Exception as e:
            _log(f"materials for {ob.name}: {e}")
            return
        for img_id in missing:
            waiting = self._waiting_tex.setdefault(img_id, [])
            if not waiting:  # first time we miss this id: ask the sender for it
                self._send(make("img_need", to=from_uid, id=img_id))
                _log(f"texture {img_id[:8]} for {ob.name} not here yet, requested from {from_uid}")
            waiting.append((ob.name, mats))

    def _apply_waiting_materials(self, img_id):
        for ob_name, mats in self._waiting_tex.pop(img_id, []):
            ob = bpy.data.objects.get(ob_name)
            if ob is not None:
                try:
                    object_data.apply_materials(ob, mats)
                except Exception as e:
                    _log(f"materials for {ob_name}: {e}")

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
            if img_id in self._sent_images or (self.role == "host" and self._image_in_snapshot(img_id)):
                continue
            self._sent_images.add(img_id)
            self._send(make("img", id=img_id, name=name, ext=ext, b64=base64.b64encode(data).decode("ascii")))
            _log(f"sent image {name} ({len(data) // 1024} KB, {img_id[:8]})")

    def _image_in_snapshot(self, img_id):
        """Images that were already packed when the guest received the .blend need no re-send."""
        return img_id in self._snapshot_images

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
        # safety-net sweep of the tiny datablocks (a handler miss on a lamp slider must not stick forever)
        if now - self._last_sweep_t >= SWEEP_SECONDS:
            self._last_sweep_t = now
            for ob in self._iter_objects():
                if ob.type in ("LIGHT", "CAMERA", "EMPTY", "FONT"):
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
            if ob is None or name in self._pending_data:
                continue
            if now < self._next_data_t.get(name, 0.0):
                self._dirty.add(name)  # throttled: check again next round (coalesces rapid edits)
                continue
            try:
                digest = object_data.quick_digest(ob)
            except Exception as e:
                _log(f"digest failed for {name}: {e}")
                continue
            if digest is None or digest == self.data_digest.get(name):
                continue
            try:
                payload = object_data.serialize(ob)
            except Exception as e:
                self._log_once(f"serialize failed for {name}: {e}")
                continue
            if payload is None:
                continue
            self.data_digest[name] = digest
            self._send(make("obj_data", name=name, payload=payload))
            self._next_data_t[name] = now + max(1.0 / DATA_HZ, object_data.payload_bytes(payload) / DATA_BUDGET_BPS)

    def _apply_obj_data(self, msg):
        name = msg["name"]
        ob = bpy.data.objects.get(name)
        if ob is None:
            return
        payload = msg["payload"]
        if ob.type == "MESH" and ob.mode == "EDIT":
            self._pending_data[name] = payload  # apply once the local user leaves Edit Mode
            return
        self._apply_payload(ob, payload)

    def _apply_payload(self, ob, payload):
        try:
            if not object_data.apply(ob, payload):
                return
        except Exception as e:
            _log(f"apply data failed for {ob.name}: {e}")
            return
        self.data_digest[ob.name] = object_data.quick_digest(ob)  # echo suppression
        self._dirty.discard(ob.name)

    def _retry_pending_data(self):
        for name in list(self._pending_data):
            ob = bpy.data.objects.get(name)
            if ob is None:
                del self._pending_data[name]
            elif ob.mode != "EDIT":
                self._apply_payload(ob, self._pending_data.pop(name))

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
