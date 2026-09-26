# SPDX-License-Identifier: GPL-2.0-or-later
"""The in-VR colour panel, built from Freebird's own UI toolkit (bl_xr), like Freebird's main menu.

Freebird's public plugin API (freebird.api) only offers launcher buttons, so the panel itself
uses the bl_xr nodes Freebird's menus are made of: a Grid2D of flat-colour Image nodes (an
Image without `src` draws a rounded rectangle in its "background" colour). No textures are
loaded here, so building the panel is safe at any time (Freebird notes that loading images
inside an application timer can crash Blender).

Placement: the panel rides on the non-dominant (alt) hand like Freebird's main menu, on the
side opposite to Freebird's sub-menus, so the laser of the main hand picks colours.
"""

import bpy
from mathutils import Vector

import bl_xr
from bl_xr import Grid2D, Image, Node, Text, root, xr_session
from bl_xr.utils import apply_haptic_feedback

from . import materials
from .palette import COLUMNS, swatches

SWATCH = 0.018  # metres (the panel keeps its real-world size whatever the world scale)
GAP = 0.004
CELL = SWATCH + GAP
PAD = 0.006
HEADER = 0.016
TEXT_SCALE = 0.0003
SWATCH_Z = 0.003  # in front of the panel background by more than bl_xr's 1 mm raycast tie epsilon
PANEL_BG = (0.0, 0.005, 0.02)
HOVER_BORDER = (0.0022, (1.0, 1.0, 1.0, 1.0))
CURRENT_BORDER = (0.0022, (0.2, 0.55, 1.0, 1.0))

MENU_HALF_WIDTH = 0.04  # Freebird main menu: BUTTON_SIZE 0.04, offset -BUTTON_SIZE, 2 columns
SIDE_GAP = 0.012


class Swatch(Image):
    def __init__(self, name, srgb, linear, panel):
        super().__init__(
            width=SWATCH,
            height=SWATCH,
            style={"background": tuple(srgb), "border_radius": SWATCH * 0.2},
        )
        self.color_name = name
        self.srgb = srgb
        self.linear = linear
        self.panel = panel
        self.add_event_listener("pointer_main_enter", self._on_enter)
        self.add_event_listener("pointer_main_leave", self._on_leave)
        self.add_event_listener("pointer_main_press_end", self._on_press)

    def set_border(self, border):
        if border is None:
            self.style.pop("border", None)
        else:
            self.style["border"] = border

    def _on_enter(self, event_name, event):
        self.panel.hovered = self
        self.set_border(HOVER_BORDER)
        self.panel.status.text = self.color_name
        try:
            apply_haptic_feedback(hand="main")
        except Exception:
            pass

    def _on_leave(self, event_name, event):
        if self.panel.hovered is self:
            self.panel.hovered = None
            self.panel.status.text = self.panel.last_message
        self.set_border(CURRENT_BORDER if self.panel.current is self else None)

    def _on_press(self, event_name, event):
        self.panel.pick(self)


class ColorPanel:
    def __init__(self):
        self.hovered = None
        self.current = None
        self.last_message = "Select, then pick a colour"
        self.visible = False

        items = swatches()
        rows = (len(items) + COLUMNS - 1) // COLUMNS
        grid_w = COLUMNS * CELL - GAP
        grid_h = rows * CELL - GAP
        self.width = grid_w + 2 * PAD
        self.height = grid_h + 2 * PAD + HEADER

        self.grid = Grid2D(
            id="vr_studio_color_grid",
            num_cols=COLUMNS,
            cell_width=CELL,
            cell_height=CELL,
            position=Vector((PAD, PAD, SWATCH_Z)),
            child_nodes=[Swatch(name, srgb, lin, self) for name, srgb, lin in items],
        )
        self.title = Text(
            id="vr_studio_color_title",
            text="COLOR",
            style={"color": (1, 1, 1, 1), "font_size": 24},
            intersects=None,
            position=Vector((PAD, grid_h + PAD + 0.006, SWATCH_Z)),
        )
        self.title.scale = TEXT_SCALE
        self.status = Text(
            id="vr_studio_color_status",
            text=self.last_message,
            style={"color": (0.75, 0.8, 0.9, 1), "font_size": 20},
            intersects=None,
            position=Vector((PAD + 0.03, grid_h + PAD + 0.006, SWATCH_Z)),
        )
        self.status.scale = TEXT_SCALE
        self.background = Image(  # also the raycast catcher between swatches (keeps the laser visible)
            id="vr_studio_color_bg",
            width=self.width,
            height=self.height,
            style={"background": PANEL_BG, "opacity": 0.95, "border_radius": 0.005,
                   "border": (0.002, (0, 0, 0.005, 1))},
        )
        self.background.add_event_listener("pointer_main_press_start", lambda *x: None)

        self.node = Node(
            id="vr_studio_color_panel",
            style={"fixed_scale": True, "visible": False},
            child_nodes=[self.background, self.grid, self.title, self.status],
        )
        self.node.update = self._update

    # -- placement --------------------------------------------------------
    def _local_offset(self):
        if bl_xr.main_hand == "right":  # Freebird's sub-menus open to the right: we go left
            x = -MENU_HALF_WIDTH - SIDE_GAP - self.width
        else:
            x = MENU_HALF_WIDTH + SIDE_GAP
        return Vector((x, 0.0, 0.0))

    def _update(self):
        if not xr_session.is_running:
            return
        rot = xr_session.controller_alt_aim_rotation
        self.node.rotation = rot
        self.node.position = xr_session.controller_alt_aim_position + rot @ (
            self._local_offset() * xr_session.viewer_scale
        )

    # -- show / hide ------------------------------------------------------
    def attach(self):
        if self.node.parent is None:
            root.append_child(self.node)

    def detach(self):
        if self.node.parent is not None:
            self.node.parent.remove_child(self.node)

    def set_visible(self, state):
        self.visible = bool(state)
        self.node.style["visible"] = self.visible

    def toggle(self):
        self.set_visible(not self.visible)

    # -- the action -------------------------------------------------------
    def pick(self, swatch):
        try:
            targets = materials.selected_targets(bpy.context)
            report = materials.apply_color(targets, swatch.linear)
            if report["objects"]:
                for name in report["objects"]:
                    ob = bpy.data.objects.get(name)
                    if ob is not None:  # Solid shading "Object" colour too (Freebird strokes use it)
                        ob.color = (*swatch.linear, 1.0)
                bpy.ops.ed.undo_push(message=f"VR colour {swatch.color_name}")
                if self.current is not None and self.current is not swatch:
                    self.current.set_border(None)
                self.current = swatch
            self.last_message = materials.describe(report)
            print(f"[vr_studio] colour {swatch.color_name}: {report}")
        except Exception as e:
            import traceback

            self.last_message = "Colour failed (see console)"
            print(f"[vr_studio] colour {swatch.color_name} failed: {e}\n{traceback.format_exc()}")
        self.status.text = self.last_message
        try:
            apply_haptic_feedback(hand="main", type="SHORT_LIGHT")
        except Exception:
            pass
