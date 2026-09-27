# SPDX-License-Identifier: GPL-2.0-or-later
"""VIEW section: 4 big buttons for the headset's viewport shading (Wireframe / Solid /
Material Preview / Rendered). Local display setting, never synced (see view.py)."""

import os

from mathutils import Vector

from . import view
from .area import StudioSection, Tile
from .look_section import COLUMNS, GAP, ICON_DIR, LABEL_H, TILE
from .color_section import WIDTH

ENGINE_NAMES = {"BLENDER_EEVEE_NEXT": "EEVEE", "BLENDER_EEVEE": "EEVEE", "CYCLES": "Cycles",
                "BLENDER_WORKBENCH": "Workbench"}


class ViewSection(StudioSection):
    title = "VIEW"
    idle_message = "How you see it (just you)"

    def __init__(self):
        super().__init__(WIDTH, TILE + LABEL_H)
        self.tiles = []
        for i, (mode, label, hover) in enumerate(view.MODES):
            x = i * (TILE + GAP)
            icon = os.path.join(ICON_DIR, f"view_{mode.lower()}.png")
            t = Tile(self, hover, TILE, None, src=icon if os.path.exists(icon) else None,
                     background=None if os.path.exists(icon) else (0.3, 0.3, 0.35), hover_fill=True)
            t.mode = mode
            t.position = Vector((x, LABEL_H, 0.0))
            self.tiles.append(t)
            self.body.append_child(t)
            text = self._text(label, (0, 0), 18, (0.85, 0.88, 0.95, 1))
            w = text.bounds_local.size.x * text.scale.x
            text.position = Vector((x + max(0.0, (TILE - w) / 2), 0.0015, 0.0))
            self.body.append_child(text)
        assert len(self.tiles) == COLUMNS

    def press(self, tile):
        try:
            engine = view.set_vr_shading(tile.mode)
            message = tile.label
            if engine:
                message += f" ({ENGINE_NAMES.get(engine, engine)})"
            for t in self.tiles:
                t.set_current(t is tile)
            print(f"[vr_studio] VR shading -> {tile.mode}")
        except Exception as e:
            import traceback

            message = "View failed (see console)"
            print(f"[vr_studio] VR shading {tile.mode} failed: {e}\n{traceback.format_exc()}")
        self.finish(message)

    def refresh(self, mat):
        mode = view.get_vr_shading()
        for t in self.tiles:
            t.set_current(t.mode == mode)
