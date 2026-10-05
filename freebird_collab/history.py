# SPDX-License-Identifier: GPL-2.0-or-later
"""
Per-user Undo / Redo while connected to a room (Gravity Sketch–style co-creation).

Blender's own Undo restores an earlier snapshot of the WHOLE file. In a room that snapshot also predates
what the other people did meanwhile, so one Ctrl+Z (or Freebird's left-stick Undo) wiped their work locally,
and the sync then pushed the rewound state back to them. While connected, Undo / Redo run here instead:

  * every change THIS user sends (object transform, object add / delete, material, material slots, pose)
    is recorded with its state before and after, grouped into steps (one gesture = one step);
  * Undo reverts the last step as a NEW edit that travels through the normal sync, so everybody sees it;
  * another user's work is never touched: an item somebody else changed after us is skipped (and logged),
    never forced back. Redo replays an undone step the same way.

This module is the bookkeeping (no bpy). CollabSession records into it and performs the actual reverts;
install_freebird_hook() routes Freebird's Undo / Redo (left stick, controller buttons) here.
"""

import sys

STEP_IDLE = 0.8  # s without a local change: the open step is closed (end of a gesture)
STEP_SPLIT = 0.2  # a change to an item not in the open step, this long after its last change, starts a new step
MAX_STEPS = 100

# item kinds: x = object world matrix, o = object existence (add / delete), s = material slots,
#             p = pose bones of an armature object, m = material state
_OWNER = {"x": "ob", "o": "ob", "s": "ob", "p": "ob", "m": "mat"}


def owner(key):
    """Conflict scope of an item: any remote change to the same object (or material) counts."""
    return (_OWNER[key[0]], key[1])


class Item:
    __slots__ = ("key", "before", "after", "ver")

    def __init__(self, key, before, after, ver):
        self.key = key
        self.before = before
        self.after = after
        self.ver = ver


class Step:
    __slots__ = ("items", "t_last", "t_add")

    def __init__(self, now):
        self.items = {}  # key -> Item, in recording order
        self.t_last = now
        self.t_add = None  # detection pass that added objects in this step (one operation)

    def label(self):
        kinds = {"x": "move", "o": "add/delete", "s": "material slots", "p": "pose", "m": "material"}
        names = sorted({k[1] for k in self.items})
        what = ", ".join(sorted({kinds[k[0]] for k in self.items}))
        return f"{what}: {', '.join(names[:4])}{', ...' if len(names) > 4 else ''}"


def _merge_pose(it, before, after):
    """Pose items keep the FIRST known state of every bone and the LAST sent one."""
    merged = dict(before or {})
    merged.update(it.before or {})
    it.before = merged
    it.after = dict(it.after or {}, **(after or {}))


class LocalHistory:
    def __init__(self):
        self.undo_stack = []
        self.redo_stack = []
        self.open = None
        self.touch = {}  # owner -> number of remote changes applied to it
        self.replaying = 0  # > 0 while Undo / Redo itself sends: never recorded
        self.last_msg = ""

    def clear(self):
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.open = None
        self.touch.clear()
        self.last_msg = ""

    # -- recording -----------------------------------------------------
    def version(self, key):
        return self.touch.get(owner(key), 0)

    def touched(self, kind, name):
        """A remote change was applied to this object ("ob") or material ("mat")."""
        k = (kind, name)
        self.touch[k] = self.touch.get(k, 0) + 1

    def record(self, kind, name, before, after, now):
        if self.replaying:
            return
        key = (kind, name)
        add = kind == "o" and after is not None
        step = self.open
        if step is not None and (now - step.t_last >= STEP_IDLE or self._splits(step, key, add, now)):
            self._close()
            step = None
        if step is None:
            step = self.open = Step(now)
        if add:
            step.t_add = now
        it = step.items.get(key)
        if it is None:
            step.items[key] = Item(key, before, after, self.version(key))
        else:
            if kind == "p":
                _merge_pose(it, before, after)
            else:
                it.after = after  # "before" stays the state at the start of the step
            it.ver = self.version(key)
        step.t_last = now
        self.redo_stack.clear()  # a new edit ends the redo chain, as everywhere else

    @staticmethod
    def _splits(step, key, add, now):
        if add:
            # every Create / Duplicate is its own step, however quick; objects added by ONE operation
            # (multi-select Duplicate, GLB import) arrive in the same detection pass (same now) and stay together
            return step.t_add != now
        added = step.items.get(("o", key[1])) if key[0] == "x" else None
        if added is not None and added.after is not None:
            return False  # dragging what this step just added (Duplicate, then move) belongs to the add
        return key not in step.items and now - step.t_last >= STEP_SPLIT

    def tick(self, now):
        if self.open is not None and now - self.open.t_last >= STEP_IDLE:
            self._close()

    def _close(self):
        step, self.open = self.open, None
        if step is not None and step.items:
            self.undo_stack.append(step)
            del self.undo_stack[:-MAX_STEPS]

    # -- undo / redo ---------------------------------------------------
    def pop(self, undo):
        self._close()
        stack = self.undo_stack if undo else self.redo_stack
        return stack.pop() if stack else None

    def push_back(self, step, undo, applied):
        """After a revert: the items that went through move to the other stack, at their current version."""
        step.items = {it.key: it for it in applied}
        for it in applied:
            it.ver = self.version(it.key)
        if step.items:
            (self.redo_stack if undo else self.undo_stack).append(step)

    def counts(self):
        return len(self.undo_stack) + (1 if self.open is not None and self.open.items else 0), len(self.redo_stack)


# ----------------------------------------------------------------------
# Freebird XR: its Undo / Redo (left stick left / right, the UNDO / REDO controller buttons) all end in
# freebird.undo_redo._run(bpy.ops.ed.undo | bpy.ops.ed.redo). While a room is active we answer those two
# here and leave everything else (and Edit / Sculpt Mode, whose undo is local to the edited mesh) to Freebird.
# Nothing in Freebird's files is changed; the original function is put back on leave / unregister.
# ----------------------------------------------------------------------
_hook = {"mod": None, "orig": None}


def op_name(task):
    try:
        return task.idname_py()
    except Exception:
        s = str(task)
        return "ed.undo" if "ed.undo" in s else "ed.redo" if "ed.redo" in s else ""


def freebird_hook_installed():
    return _hook["mod"] is not None


def install_freebird_hook(handle):
    """handle(name) -> True / False when the room answered ("ed.undo" / "ed.redo"), None to let Freebird do it."""
    if _hook["mod"] is not None:
        return True
    mod = sys.modules.get("freebird.undo_redo")
    if mod is None:
        for name, m in list(sys.modules.items()):
            if name.endswith("freebird.undo_redo"):
                mod = m
                break
    orig = getattr(mod, "_run", None) if mod is not None else None
    if orig is None:
        return False

    def _run(task):
        name = op_name(task)
        if name in ("ed.undo", "ed.redo"):
            try:
                done = handle(name)
            except Exception as e:
                print(f"[collab] room {name} failed: {e}")
                done = False
            if done is not None:
                return done  # True also lets Freebird dispatch fb.undo / fb.redo (tools reset their state)
        return orig(task)

    _run._collab_orig = orig
    mod._run = _run
    _hook["mod"], _hook["orig"] = mod, orig
    print("[collab] Freebird Undo / Redo now undo only your own changes while in the room")
    return True


def uninstall_freebird_hook():
    mod, orig = _hook["mod"], _hook["orig"]
    if mod is not None and orig is not None and getattr(mod._run, "_collab_orig", None) is orig:
        mod._run = orig
    _hook["mod"] = _hook["orig"] = None
