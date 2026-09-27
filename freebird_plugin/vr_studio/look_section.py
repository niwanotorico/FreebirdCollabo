# SPDX-License-Identifier: GPL-2.0-or-later
"""LOOK section: 8 material-ball tiles (4 x 2) with names. Press one = give the selection that
Look (its colour is kept)."""

import os

import bpy
from mathutils import Vector

from . import materials
from .area import StudioSection, Tile
from .color_section import WIDTH
from .looks import LOOK_NAMES

COLUMNS = 4
LABEL_H = 0.008
INNER = WIDTH - 2 * 0.006
GAP = 0.006
TILE = (INNER - (COLUMNS - 1) * GAP) / COLUMNS  # ~0.039 m
ROW = TILE + LABEL_H + 0.002
ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")


class LookSection(StudioSection):
    title = "LOOK"
    idle_message = "Select, then pick a look"

    def __init__(self):
        rows = (len(LOOK_NAMES) + COLUMNS - 1) // COLUMNS
        super().__init__(WIDTH, rows * ROW - 0.002)
        self.tiles = []
        for i, name in enumerate(LOOK_NAMES):
            row, col = divmod(i, COLUMNS)
            x, y = col * (TILE + GAP), (rows - 1 - row) * ROW + LABEL_H
            icon = os.path.join(ICON_DIR, f"look_{name.lower()}.png")
            t = Tile(self, name, TILE, None, src=icon if os.path.exists(icon) else None,
                     background=None if os.path.exists(icon) else (0.3, 0.3, 0.35), hover_fill=True)
            t.look = name
            t.position = Vector((x, y, 0.0))
            self.tiles.append(t)
            self.body.append_child(t)
            label = self._text(name, (0, 0), 18, (0.85, 0.88, 0.95, 1))
            w = label.bounds_local.size.x * label.scale.x
            label.position = Vector((x + max(0.0, (TILE - w) / 2), y - LABEL_H + 0.0015, 0.0))
            self.body.append_child(label)

    def press(self, tile):
        try:
            targets = materials.selected_targets(bpy.context)
            report = materials.apply_look(targets, tile.look)
            if report["objects"]:
                bpy.ops.ed.undo_push(message=f"VR look {tile.look}")
                for t in self.tiles:
                    t.set_current(t is tile)
            message = materials.describe(report)
            if report["objects"] and tile.look == "Toon":
                message += " (Material Preview)"
            print(f"[vr_studio] look {tile.look}: {report}")
        except Exception as e:
            import traceback

            message = "Look failed (see console)"
            print(f"[vr_studio] look {tile.look} failed: {e}\n{traceback.format_exc()}")
        self.finish(message)

    def refresh(self, mat):
        look = materials.detect_look(mat)
        for t in self.tiles:
            t.set_current(t.look == look)
