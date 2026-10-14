# SPDX-License-Identifier: GPL-2.0-or-later
"""
Relay traffic reduction (docs/relay-load-analysis.md). Pure stdlib, no bpy, so tools/ and tests can use it.

  PresenceThrottle  presence goes out only when it changed (with a small dead band for VR tracking noise),
                    plus a heartbeat so receivers keep drawing us, and never while nobody else is in the room.
  batch_groups      splits one tick's outgoing messages into "batch" envelopes. Only used when the hub said
                    welcome.batch = 1; the hub unpacks them and forwards each message on its own, so peers on
                    older add-ons receive exactly what they always did.
"""

import json

PRESENCE_HZ = 20.0  # max presence rate while it is changing
# Receivers on v0.12.x hide a peer whose last presence is older than 3 s (presence.STALE_SECONDS),
# so the heartbeat must stay well below that, also with some network jitter.
PRESENCE_HEARTBEAT = 1.0
POS_TOL = 0.002  # metres: below this the head / hand moved too little to redraw (tracking noise)
ROT_TOL = 0.002  # quaternion component (~0.2 degrees)

BATCHABLE = frozenset(("presence", "xform", "pose", "ver", "obj_mats", "obj_del", "mat_ren", "mat_del", "img_need"))
BATCH_MAX_BYTES = 256 * 1024  # well below link.CHUNK_BYTES, so a batch is never split into chunks


def _pose_moved(a, b):
    if a is None or b is None:
        return a is not b
    if len(a) != len(b):
        return True
    for i, (x, y) in enumerate(zip(a, b)):
        tol = POS_TOL if i < 3 else ROT_TOL if i < 7 else 0.0
        if abs(x - y) > tol:
            return True
    return False


def presence_changed(prev, cur):
    """True when cur differs from prev enough to be worth sending (dead band on head / hand poses only)."""
    if prev is None:
        return True
    if set(prev) != set(cur):
        return True
    for k, v in cur.items():
        if k == "head":
            if _pose_moved(prev.get(k), v):
                return True
        elif k == "hands":
            a, b = prev.get(k) or {}, v or {}
            if set(a) != set(b) or any(_pose_moved(a[h], b[h]) for h in b):
                return True
        elif prev.get(k) != v:
            return True
    return False


class PresenceThrottle:
    def __init__(self, hz=PRESENCE_HZ, heartbeat=PRESENCE_HEARTBEAT):
        self.min_dt = 1.0 / hz
        self.heartbeat = heartbeat
        self.last = None  # presence dict last sent
        self.last_t = 0.0

    def reset(self):
        """Send on the next check (somebody joined and has not seen us yet)."""
        self.last = None

    def should_send(self, now, pres, has_peers):
        if not has_peers:
            self.last = None  # the first one after a peer arrives goes out at once
            return False
        if now - self.last_t < self.min_dt:
            return False
        if self.last is None or now - self.last_t >= self.heartbeat or presence_changed(self.last, pres):
            self.last = pres
            self.last_t = now
            return True
        return False


def batch_groups(msgs):
    """Order-preserving split of outgoing messages into single messages and batch envelopes."""
    out = []
    group, size = [], 0

    def flush():
        nonlocal group, size
        if len(group) == 1:
            out.append(group[0])
        elif group:
            out.append({"t": "batch", "m": group})
        group, size = [], 0

    for m in msgs:
        if m.get("t") not in BATCHABLE:
            flush()
            out.append(m)
            continue
        n = len(json.dumps(m, separators=(",", ":"), ensure_ascii=False))
        if n > BATCH_MAX_BYTES:
            flush()
            out.append(m)
            continue
        if size + n > BATCH_MAX_BYTES:
            flush()
        group.append(m)
        size += n
    flush()
    return out

