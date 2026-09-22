# SPDX-License-Identifier: GPL-2.0-or-later
"""
Wire protocol for Freebird Collaboration Layer.

Framing : 4-byte big-endian length + UTF-8 JSON payload.
Routing : every message carries "t" (type). The hub (relay or direct host)
          forwards a message to every other peer in the room, or only to
          msg["to"] when present. Fields "from" and "room" are stamped by hub.

Message types (client -> hub -> peers):
  hello           {name, role: host|guest, room, color}     first message, hub replies welcome
  welcome         {uid, room, peers:[{uid,name,role,color}], host_uid}
  peer_join       {uid, name, role, color}
  peer_leave      {uid}
  ver             {v}                                        add-on version, sent after welcome and to each joiner
  error           {msg}
  scene           {to, blend_b64, filename}                  host -> new guest, one-shot
  presence        {vr, head:[x,y,z,qw,qx,qy,qz]|null, hands:{L:[..7 + ray(0/1)], R:[..]},
                   tool, sel:[names], mode}                  ~20 Hz
  xform           {objs:{name:[16 floats]}}                  only for changed objects
  obj_add         {name, type, payload:{type, data}, m:[16], parent:name|null,
                   mats:[{n, c:[rgba], p, tex, tx, ...}|null]} (see object_data.py)
  obj_data        {name, payload:{type, data}}                 datablock contents changed. type ARMATURE (0.9+):
                                                               data.bones = [{n, h:[3], t:[3], r, p:name|null, c}]
                                                               = Edit Bone name / head / tail / roll / parent /
                                                               connected, parents first, matched by bone name.
                                                               type MESH (0.10+) also carries the skinning:
                                                               data.vg = [group names], data.w = per group
                                                               [vertex index, weight, ...], data.mods = Armature
                                                               modifiers [{n, i, ob:armature name|null, vg, uvg,
                                                               env, pv, inv, mm, sv}] (object_data.py)
  mat             {mat:{n, ...}}                               material created / changed. Same state dict as in
                                                               obj_add.mats; only the changed keys travel (object_data.py).
                                                               0.11+ adds nt = the whole standard shader node tree
                                                               {nodes:{name:{t, l, i, pr, img, ramp...}}, links, x};
                                                               a delta carries d:1, changed nodes, del, links. Older
                                                               peers ignore nt; the Principled-only keys stay for them
  mat_ren         {old, new}                                   material renamed (identity = session_uid on the sender)
  mat_del         {names:[..]}                                 material deleted
  obj_mats        {name, slots:[[material|null, DATA|OBJECT]]} material slots of an object (assignment / count / link)
  pose            {name, bones:{bone:{rm, l:[3], r:[3|4], s:[3]}}}  Pose Mode bone transforms of an armature object,
                                                               changed bones only, matched by bone name. rm = rotation
                                                               mode, r = the values of that mode (object_data.py)
  img             {id: sha1, name, ext, b64}                   image texture, once per image per session
  img_need        {to, id}                                     receiver lacks a referenced texture -> sender answers with img
  chunk           {id, i, n, d}                                 link.py splits messages > 700 KB
  obj_del         {names:[..]}
  save            {}                                         guest -> host: please save master
  ping / pong     {}
"""

import json
import struct

HEADER = struct.Struct(">I")
MAX_FRAME = 256 * 1024 * 1024  # 256 MB (scene snapshots)
PROTOCOL_VERSION = 1


def encode(msg: dict) -> bytes:
    body = json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_FRAME:
        raise ValueError("frame too large")
    return HEADER.pack(len(body)) + body


class Decoder:
    """Incremental frame decoder. feed(bytes) -> list[dict]."""

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data: bytes):
        self._buf.extend(data)
        out = []
        while True:
            if len(self._buf) < HEADER.size:
                break
            (n,) = HEADER.unpack_from(self._buf, 0)
            if n > MAX_FRAME:
                raise ValueError("frame too large")
            if len(self._buf) < HEADER.size + n:
                break
            body = bytes(self._buf[HEADER.size : HEADER.size + n])
            del self._buf[: HEADER.size + n]
            out.append(json.loads(body.decode("utf-8")))
        return out


def make(t: str, **fields) -> dict:
    fields["t"] = t
    return fields
