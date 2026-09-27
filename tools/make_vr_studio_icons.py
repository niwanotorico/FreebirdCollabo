# SPDX-License-Identifier: GPL-2.0-or-later
"""Draws the VR Studio icons (launcher buttons + the 8 Look thumbnails) with numpy + Pillow.

    python3 tools/make_vr_studio_icons.py

Stylised material balls, not renders: they read clearly at ~3 cm in VR, and every Look
(Toon included, which Cycles cannot render) gets a thumbnail in the same style.
"""

import os

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "freebird_plugin", "vr_studio", "icons")
S = 512  # draw size, saved at 128
BASE = np.array([0x4F, 0x86, 0xF7]) / 255.0  # the ball colour for every Look


def _norm(v):
    return v / np.linalg.norm(v, axis=-1, keepdims=True)


def _sphere(r=0.40):
    y, x = np.mgrid[0:S, 0:S]
    px = (x + 0.5) / S * 2 - 1
    py = 1 - (y + 0.5) / S * 2
    d2 = (px ** 2 + py ** 2) / r ** 2
    inside = d2 <= 1.0
    nz = np.sqrt(np.clip(1 - d2, 0, 1))
    n = np.dstack([px / r, py / r, nz])
    # anti-aliased edge
    dist = np.sqrt(px ** 2 + py ** 2)
    alpha = np.clip((r - dist) * S / 2 + 0.5, 0, 1)
    return n, inside, alpha


L = _norm(np.array([-0.55, 0.65, 0.75]))
V = np.array([0.0, 0.0, 1.0])
H = _norm(L + V)


def _env(r):
    """Studio environment seen in a reflection direction: bright sky, dark floor, a soft key light."""
    up = r[..., 1]
    sky = 0.55 + 0.45 * np.clip(up, 0, 1)
    floor = 0.12 + 0.1 * np.clip(1 + up, 0, 1)
    e = np.where(up > 0, sky, floor)
    key = np.clip(np.sum(r * L, axis=-1), 0, 1) ** 30 * 1.5
    return (e + key)[..., None]


def _save(rgb, alpha, name, glow=None):
    rgba = np.dstack([np.clip(rgb, 0, 1), alpha])
    img = Image.fromarray((rgba * 255).astype(np.uint8), "RGBA")
    if glow is not None:
        img = Image.alpha_composite(glow, img)
    img.resize((128, 128), Image.LANCZOS).save(os.path.join(OUT, name))


def ball(look):
    n, inside, alpha = _sphere()
    ndl = np.clip(np.sum(n * L, axis=-1), 0, 1)[..., None]
    ndh = np.clip(np.sum(n * H, axis=-1), 0, 1)[..., None]
    ndv = np.clip(n[..., 2], 0, 1)[..., None]
    fres = 0.04 + 0.96 * (1 - ndv) ** 5
    r = 2 * ndv * n - V
    env = _env(r)
    c = BASE[None, None, :]
    glow = None
    if look == "Clay":
        chalk = c * 0.7 + 0.3 * np.array([0.62, 0.6, 0.58])[None, None, :]  # powdery, a bit desaturated
        rgb = chalk * (0.42 + 0.62 * ndl ** 0.6) + 0.12 * (1 - ndv) ** 2
    elif look == "Matte":
        rgb = c * (0.22 + 0.85 * ndl) + 0.05 * ndh ** 8
    elif look == "Plastic":
        rgb = c * (0.2 + 0.8 * ndl) + 0.45 * ndh ** 45 + 0.1 * fres * env
    elif look == "Glossy":
        rgb = c * (0.18 + 0.8 * ndl) + 1.2 * ndh ** 350 + 0.9 * fres * env + 0.06 * env
    elif look == "Metallic":
        rgb = c * (0.15 + 0.9 * env) + 0.8 * ndh ** 80 * c + 0.3 * ndh ** 300
    elif look == "Glass":
        rim = (1 - ndv) ** 2.5
        rgb = c * (0.35 + 0.4 * (1 - ndv)) + 1.4 * ndh ** 400 + 0.7 * rim * env
        alpha = alpha * (0.35 + 0.65 * rim[..., 0]) + alpha * 0.5 * (ndh[..., 0] ** 400)
        alpha = np.clip(alpha, 0, 1)
    elif look == "Emission":
        core = 0.55 + 0.45 * ndv
        rgb = np.clip(c * 1.3 * core + 0.12 * ndv ** 4, 0, 1)
        halo = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        d = ImageDraw.Draw(halo)
        col = tuple(int(x * 255) for x in np.clip(BASE * 1.3, 0, 1))
        m = int(S * 0.08)
        d.ellipse([m, m, S - m, S - m], fill=col + (150,))
        glow = halo.filter(ImageFilter.GaussianBlur(S * 0.06))
    elif look == "Toon":
        band = np.where(ndl > 0.45, 1.0, np.where(ndl > 0.12, 0.62, 0.3))
        rgb = c * band + np.where(ndh ** 60 > 0.5, 0.35, 0.0)
        # outline
        y, x = np.mgrid[0:S, 0:S]
        dist = np.sqrt(((x + 0.5) / S * 2 - 1) ** 2 + (1 - (y + 0.5) / S * 2) ** 2)
        edge = (dist > 0.40 - 0.035)[..., None]
        rgb = np.where(edge, 0.05, rgb)
    else:
        raise KeyError(look)
    rgb = np.where(inside[..., None], rgb, 0)
    _save(rgb, alpha, f"look_{look.lower()}.png", glow)


def launcher_look():
    """Launcher button icon for LOOK: three balls (matte / glossy / metallic)."""
    big = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    for look, (ox, oy, sc) in (("Matte", (-0.2, -0.15, 0.62)), ("Metallic", (0.22, -0.15, 0.62)), ("Glossy", (0.0, 0.2, 0.62))):
        ball(look)
        im = Image.open(os.path.join(OUT, f"look_{look.lower()}.png")).resize((int(S * sc), int(S * sc)), Image.LANCZOS)
        big.alpha_composite(im, (int(S / 2 + ox * S / 2 - S * sc / 2), int(S / 2 - oy * S / 2 - S * sc / 2)))
    big.resize((128, 128), Image.LANCZOS).save(os.path.join(OUT, "look.png"))


def view_icon(mode):
    """Viewport shading icons, drawn like Blender's own four shading buttons (a ball each)."""
    n, inside, alpha = _sphere(0.62)
    ndl = np.clip(np.sum(n * L, axis=-1), 0, 1)[..., None]
    ndh = np.clip(np.sum(n * H, axis=-1), 0, 1)[..., None]
    ndv = np.clip(n[..., 2], 0, 1)[..., None]
    grey = np.array([0.82, 0.83, 0.86])[None, None, :]
    if mode == "wireframe":
        img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        r, c, w = S * 0.31, S / 2, int(S * 0.022)
        box = [c - r, c - r, c + r, c + r]
        d.ellipse(box, outline=(235, 238, 245, 255), width=w)
        for k in (0.35, 0.72):  # meridians
            d.ellipse([c - r * k, c - r, c + r * k, c + r], outline=(235, 238, 245, 255), width=w)
        for k in (-0.5, 0.0, 0.5):  # parallels
            y = c + r * k
            half = r * np.sqrt(1 - k * k)
            d.ellipse([c - half, y - half * 0.28, c + half, y + half * 0.28], outline=(235, 238, 245, 255), width=w)
        img.resize((128, 128), Image.LANCZOS).save(os.path.join(OUT, "view_wireframe.png"))
        return
    if mode == "solid":
        rgb = grey * (0.35 + 0.65 * ndl) + 0.15 * ndh ** 20
    elif mode == "material":
        c = np.array([0.93, 0.55, 0.25])[None, None, :]
        r = 2 * ndv * n - V
        rgb = c * (0.2 + 0.8 * ndl) + 0.6 * ndh ** 120 + 0.25 * (0.04 + 0.96 * (1 - ndv) ** 5) * _env(r)
    elif mode == "rendered":
        c = np.array([0.93, 0.55, 0.25])[None, None, :]
        r = 2 * ndv * n - V
        rgb = c * (0.08 + 0.95 * ndl ** 1.3) + 0.9 * ndh ** 200 + 0.3 * (0.04 + 0.96 * (1 - ndv) ** 5) * _env(r)
        # soft contact shadow under the ball
        sh = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        ImageDraw.Draw(sh).ellipse([S * 0.22, S * 0.74, S * 0.78, S * 0.86], fill=(255, 255, 255, 60))
        glow = sh.filter(ImageFilter.GaussianBlur(S * 0.03))
        rgb = np.where(inside[..., None], rgb, 0)
        _save(rgb, alpha, "view_rendered.png", glow)
        return
    rgb = np.where(inside[..., None], rgb, 0)
    _save(rgb, alpha, f"view_{mode}.png")


def launcher_view():
    """Launcher icon for VIEW: an eye."""
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    t = np.linspace(-1, 1, 60)
    top = [(S / 2 + x * S * 0.4, S / 2 - (1 - x * x) * S * 0.22) for x in t]
    bottom = [(S / 2 + x * S * 0.4, S / 2 + (1 - x * x) * S * 0.22) for x in t[::-1]]
    d.line(top + bottom + [top[0]], fill=(255, 255, 255, 255), width=int(S * 0.045), joint="curve")
    d.ellipse([S * 0.38, S * 0.38, S * 0.62, S * 0.62], fill=(255, 255, 255, 255))
    im.resize((128, 128), Image.LANCZOS).save(os.path.join(OUT, "view.png"))


def launcher_color():
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    cols = ["#E53935", "#FB8C00", "#FDD835", "#43A047", "#FFFFFF", "#00ACC1", "#1E88E5", "#8E24AA", "#E91E63"]
    m, g = 60, 24
    c = (S - 2 * m - 2 * g) // 3
    for i, h in enumerate(cols):
        x, y = m + (i % 3) * (c + g), m + (i // 3) * (c + g)
        d.rounded_rectangle([x, y, x + c, y + c], radius=c // 5, fill=h)
    im.resize((128, 128), Image.LANCZOS).save(os.path.join(OUT, "color.png"))


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    launcher_color()
    launcher_look()
    launcher_view()
    for mode in ("wireframe", "solid", "material", "rendered"):
        view_icon(mode)
    for name in ("Clay", "Matte", "Glossy", "Plastic", "Metallic", "Glass", "Emission", "Toon"):
        ball(name)
    print("icons ->", OUT)
