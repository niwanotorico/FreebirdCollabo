# SPDX-License-Identifier: GPL-2.0-or-later
"""The VR Studio area: one strip next to Freebird's main menu that holds the Studio sections
(Color, Look, later Assets / Modifiers ...), built from Freebird's own UI toolkit (bl_xr).

Placement (every frame, so it rides on the hand like the main menu):
  * the area sits on the side where Freebird opens its sub-menus (right for right-handed),
    bottom-aligned with the main menu, right next to it
  * when one of Freebird's own sub-menus (Pen, Shape, Edit, Plugins, Mirror ...) is open on
    that side and would overlap, the area slides past it, and slides back when it closes
  * visible sections stack upwards in registration order; hidden ones take no space

A section is anything with: node (bl_xr Node, origin bottom-left, metres), width, height,
visible, refresh(context_material) — see StudioSection. Adding "Assets" = one new section
class + one launcher button in __init__.py.

bl_xr notes (Freebird v2.15): an Image without `src` draws a rounded flat-colour rectangle;
raycasts prefer leaf nodes and round distances to 1 mm, so clickable tiles are leaves sitting
3 mm in front of their panel background; textures must not be loaded from a timer.
"""

import sys

import bpy
from mathutils import Vector

import bl_xr
from bl_xr import Image, Node, Text, root, xr_session
from bl_xr.utils import apply_haptic_feedback

PAD = 0.006
HEADER = 0.016
TILE_Z = 0.003  # in front of the panel background by more than bl_xr's 1 mm raycast rounding
TEXT_SCALE = 0.0003
SECTION_GAP = 0.006
SIDE_GAP = 0.008
AREA_Z = 0.005  # Freebird's SUBMENU_Z_OFFSET
PANEL_BG = (0.0, 0.005, 0.02)
HOVER_BORDER = (0.0022, (1.0, 1.0, 1.0, 1.0))
CURRENT_BORDER = (0.0022, (0.2, 0.55, 1.0, 1.0))
HOVER_FILL = (0.064, 0.155, 0.662)
REFRESH_EVERY = 12  # frames between "what does the selection look like now" checks

# Freebird main-menu geometry, used when freebird.ui.main_menu is not importable
FALLBACK_MENU_OFFSET = -0.04  # menu_group sits BUTTON_SIZE to the side of the alt controller
FALLBACK_MAIN = (0.0, 0.08, 0.051)  # main menu left, right, bottom in menu_group space


def haptic(kind="TINY_LIGHT"):
    try:
        apply_haptic_feedback(hand="main", type=kind)
    except Exception:
        pass


class Tile(Image):
    """A clickable square: flat colour (background=...) or thumbnail (src=...). A leaf node."""

    def __init__(self, section, label, size, on_press, src=None, background=None, hover_fill=False):
        style = {"border_radius": size * 0.2}
        if background is not None:
            style["background"] = tuple(background)
        super().__init__(src=src, width=size, height=size, style=style)
        self.section = section
        self.label = label
        self.on_press = on_press
        self.hover_fill = hover_fill
        self.hovered = False
        self.current = False
        self.add_event_listener("pointer_main_enter", self._on_enter)
        self.add_event_listener("pointer_main_leave", self._on_leave)
        self.add_event_listener("pointer_main_press_end", self._on_press)

    def _restyle(self):
        border = HOVER_BORDER if self.hovered else CURRENT_BORDER if self.current else None
        if border is None:
            self.style.pop("border", None)
        else:
            self.style["border"] = border
        if self.hover_fill:
            if self.hovered or self.current:
                self.style["background"] = HOVER_FILL if self.hovered else (0.02, 0.06, 0.2)
            else:
                self.style.pop("background", None)

    def set_current(self, state):
        if self.current != state:
            self.current = state
            self._restyle()

    def _on_enter(self, event_name, event):
        self.hovered = True
        self._restyle()
        self.section.show_status(self.label)
        haptic()

    def _on_leave(self, event_name, event):
        self.hovered = False
        self._restyle()
        self.section.show_status(None)

    def _on_press(self, event_name, event):
        self.section.press(self)


class StudioSection:
    """Base: a panel with a title, a status line and tiles. Subclasses fill self.body."""

    title = "SECTION"
    idle_message = ""

    def __init__(self, width, body_height):
        self.width = width
        self.height = body_height + 2 * PAD + HEADER
        self.visible = False
        self.last_message = self.idle_message
        self.background = Image(  # also catches the laser between tiles, so it stays visible
            width=self.width,
            height=self.height,
            style={"background": PANEL_BG, "opacity": 0.95, "border_radius": 0.005,
                   "border": (0.002, (0, 0, 0.005, 1))},
        )
        self.background.add_event_listener("pointer_main_press_start", lambda *x: None)
        self.title_text = self._text(self.title, (PAD, body_height + PAD + 0.006), 24, (1, 1, 1, 1))
        self.status = self._text(self.last_message, (PAD + 0.032, body_height + PAD + 0.006), 20, (0.75, 0.8, 0.9, 1))
        self.body = Node(position=Vector((PAD, PAD, TILE_Z)))
        self.node = Node(child_nodes=[self.background, self.body, self.title_text, self.status],
                         style={"visible": False})

    @staticmethod
    def _text(text, xy, size, color):
        t = Text(text=text, style={"color": color, "font_size": size}, intersects=None,
                 position=Vector((xy[0], xy[1], TILE_Z)))
        t.scale = TEXT_SCALE
        return t

    def show_status(self, text):
        self.status.text = text if text else self.last_message

    def set_visible(self, state):
        self.visible = bool(state)
        self.node.style["visible"] = self.visible

    def finish(self, message):
        self.last_message = message
        self.status.text = message
        haptic("SHORT_LIGHT")

    def press(self, tile):
        raise NotImplementedError

    def refresh(self, mat):
        """Mark the tile matching the active object's material (mat may be None)."""


def context_material(context=None):
    """First material of the active (or first selected) object: what the panels highlight."""
    context = context or bpy.context
    vl = context.view_layer
    ob = vl.objects.active
    if ob is None or not ob.select_get(view_layer=vl):
        ob = next((o for o in vl.objects if o.select_get(view_layer=vl)), None)
    if ob is None:
        return None
    for slot in getattr(ob, "material_slots", []):
        if slot.material is not None:
            return slot.material
    return None


class StudioArea:
    def __init__(self):
        self.sections = []
        self.node = Node(id="vr_studio_area", style={"fixed_scale": True, "visible": True})
        self.node.update = self._update
        self._x = None
        self._frame = 0
        self.width = self.height = 0.0

    def add(self, section):
        self.sections.append(section)
        self.node.append_child(section.node)
        return section

    # -- show / hide ------------------------------------------------------
    def attach(self):
        if self.node.parent is None:
            root.append_child(self.node)

    def detach(self):
        if self.node.parent is not None:
            self.node.parent.remove_child(self.node)

    def toggle(self, section):
        section.set_visible(not section.visible)
        self._layout()
        self._refresh()

    # -- layout -----------------------------------------------------------
    def _layout(self):
        y = 0.0
        for s in self.sections:
            if s.visible:
                s.node.position = Vector((0.0, y, 0.0))
                y += s.height + SECTION_GAP
        self.height = max(0.0, y - SECTION_GAP)
        self.width = max([s.width for s in self.sections if s.visible] or [0.0])

    def _menu_space(self):
        """(left, right, bottom, [(l, r, b, t) of Freebird panels open beside the main menu])
        in Freebird's menu_group space (metres, before world scale)."""
        mm = sys.modules.get("freebird.ui.main_menu")
        if mm is None:
            left, right, bottom = FALLBACK_MAIN
            return left, right, bottom, []
        group = mm.menu_group
        panels = group.q("#main_menu_panels")
        off = panels.position if panels is not None else Vector()
        main = mm.main_menu
        b = main.bounds_local
        left, right = off.x + main.position.x + b.min.x, off.x + main.position.x + b.max.x
        bottom = off.y + main.position.y + b.min.y
        others = []
        if group.get_computed_style("visible", True):
            for node in list(mm.submenus.values()) + [mm.submenu_custom, getattr(mm, "mirror_panel", None)]:
                if node is None or not node.get_computed_style("visible", True):
                    continue
                p = off if node.parent is panels else Vector()
                nb = node.bounds_local
                x0, y0 = p.x + node.position.x, p.y + node.position.y
                others.append((x0 + nb.min.x, x0 + nb.max.x, y0 + nb.min.y, y0 + nb.max.y))
        return left, right, bottom, others

    def _target_x(self, left, right, bottom, others):
        top = bottom + self.height
        if bl_xr.main_hand == "right":  # Freebird's sub-menus open on the right: so do we
            edge = right
            for l, r, b, t in others:
                if l >= right - 0.002 and b < top and t > bottom:
                    edge = max(edge, r)
            return edge + SIDE_GAP
        edge = left
        for l, r, b, t in others:
            if r <= left + 0.002 and b < top and t > bottom:
                edge = min(edge, l)
        return edge - SIDE_GAP - self.width

    def _update(self):
        if not xr_session.is_running:
            return
        self._layout()
        visible = any(s.visible for s in self.sections)
        if not visible:
            return
        left, right, bottom, others = self._menu_space()
        tx = self._target_x(left, right, bottom, others)
        self._x = tx if self._x is None or abs(tx - self._x) < 0.001 else self._x + (tx - self._x) * 0.35
        scale = xr_session.viewer_scale
        rot = xr_session.controller_alt_aim_rotation
        origin = xr_session.controller_alt_aim_position + rot @ Vector((FALLBACK_MENU_OFFSET * scale, 0, 0))
        self.node.rotation = rot
        self.node.position = origin + rot @ (Vector((self._x, bottom, AREA_Z)) * scale)
        self._frame += 1
        if self._frame % REFRESH_EVERY == 0:
            self._refresh()

    def _refresh(self):
        try:
            mat = context_material()
        except Exception:
            mat = None
        for s in self.sections:
            if s.visible:
                try:
                    s.refresh(mat)
                except Exception as e:
                    print(f"[vr_studio] refresh {s.title}: {e}")
