# SPDX-License-Identifier: GPL-2.0-or-later
"""The 32-colour VR palette. Pure data, no bpy: 4 rows x 8 columns, top row first.

Colours are written as sRGB hex (what people pick and what the swatch shows);
Blender material colours are scene-linear, see srgb_to_linear().
"""

COLUMNS = 8

# fmt: off
PALETTE = [
    # neutrals + skin / wood
    ("White",      "#FFFFFF"), ("Light Gray", "#D0D0D0"), ("Gray",       "#9A9A9A"), ("Dark Gray",  "#5E5E5E"),
    ("Charcoal",   "#333333"), ("Black",      "#111111"), ("Skin",       "#F1C9A5"), ("Brown",      "#8D5B3C"),
    # light
    ("Pink Light", "#F4A6A6"), ("Peach",      "#FFCC8A"), ("Lemon",      "#FFF3A0"), ("Mint",       "#A8DDA9"),
    ("Aqua",       "#8FE0E8"), ("Sky",        "#9CCBF7"), ("Lavender",   "#CDA4DE"), ("Rose",       "#F7A8C8"),
    # vivid
    ("Red",        "#E53935"), ("Orange",     "#FB8C00"), ("Yellow",     "#FDD835"), ("Green",      "#43A047"),
    ("Teal",       "#00ACC1"), ("Blue",       "#1E88E5"), ("Purple",     "#8E24AA"), ("Magenta",    "#E91E63"),
    # dark
    ("Maroon",     "#8E1B1B"), ("Rust",       "#A64B00"), ("Olive",      "#9E8600"), ("Forest",     "#1B5E20"),
    ("Deep Teal",  "#006064"), ("Navy",       "#0D47A1"), ("Plum",       "#4A148C"), ("Wine",       "#880E4F"),
]
# fmt: on

assert len(PALETTE) == 32


def hex_to_srgb(hex_str):
    h = hex_str.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def _channel_to_linear(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def srgb_to_linear(rgb):
    return tuple(_channel_to_linear(c) for c in rgb)


def swatches():
    """[(name, srgb tuple, linear tuple)] in display order."""
    out = []
    for name, hx in PALETTE:
        srgb = hex_to_srgb(hx)
        out.append((name, srgb, srgb_to_linear(srgb)))
    return out
