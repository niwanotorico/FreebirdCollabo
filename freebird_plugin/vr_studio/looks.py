# SPDX-License-Identifier: GPL-2.0-or-later
"""The 8 Looks. Pure data, no bpy.

A Look owns every Principled BSDF input listed in LOOK_INPUTS (and a few material settings)
EXCEPT the colour: Base Color belongs to Color. Each Look writes the whole LOOK_INPUTS set
(defaults + its overrides), so switching Glass -> Matte never leaves transmission behind,
and Color and Look can be changed in any order without touching each other.

Values tuned for Blender 5.x Principled BSDF (OpenPBR-style, Specular IOR Level 0.5 = the
material's own IOR, AgX view transform) in EEVEE Material Preview, Cycles, and Solid shading.
"""

# Principled inputs a Look controls, with Blender 5.x's own defaults (identifier -> value)
LOOK_INPUTS = {
    "Metallic": 0.0,
    "Roughness": 0.5,
    "IOR": 1.5,
    "Alpha": 1.0,
    "Diffuse Roughness": 0.0,
    "Subsurface Weight": 0.0,
    "Specular IOR Level": 0.5,
    "Specular Tint": (1.0, 1.0, 1.0, 1.0),
    "Anisotropic": 0.0,
    "Transmission Weight": 0.0,
    "Coat Weight": 0.0,
    "Coat Roughness": 0.03,
    "Coat IOR": 1.5,
    "Sheen Weight": 0.0,
    "Sheen Roughness": 0.5,
    "Emission Strength": 0.0,
    "Thin Film Thickness": 0.0,
}

# material-level settings a Look controls (Solid-mode viewport + EEVEE)
LOOK_SETTINGS = {
    "surface_render_method": "DITHERED",
    "use_raytrace_refraction": False,
    "viewport_alpha": 1.0,  # Material.diffuse_color alpha: Solid mode draws < 1 as see-through
}

EMISSION_STRENGTH = 2.0

# fmt: off
LOOKS = [
    # name,      Principled overrides,                                            settings overrides,                              toon
    ("Clay",     {"Roughness": 1.0, "Diffuse Roughness": 1.0, "Specular IOR Level": 0.15,
                  "Sheen Weight": 0.2, "Sheen Roughness": 0.8},                   {},                                               False),
    ("Matte",    {"Roughness": 0.8, "Diffuse Roughness": 0.3, "Specular IOR Level": 0.35}, {},                                      False),
    ("Glossy",   {"Roughness": 0.12, "Coat Weight": 1.0, "Coat Roughness": 0.02},  {},                                               False),
    ("Plastic",  {"Roughness": 0.35, "IOR": 1.46},                                  {},                                               False),
    ("Metallic", {"Metallic": 1.0, "Roughness": 0.25},                              {},                                               False),
    ("Glass",    {"Roughness": 0.0, "IOR": 1.45, "Transmission Weight": 1.0},
                 {"use_raytrace_refraction": True, "viewport_alpha": 0.35},                                                         False),
    ("Emission", {"Emission Strength": EMISSION_STRENGTH},                          {},                                               False),
    ("Toon",     {"Roughness": 0.8, "Specular IOR Level": 0.35},                    {},                                               True),
]
# fmt: on

LOOK_NAMES = [name for name, *_ in LOOKS]
assert len(LOOKS) == 8

# Toon: Diffuse -> Shader to RGB -> ColorRamp (constant, 3 bands) -> Mix Multiply (colour) -> Emission -> Output.
# The Principled BSDF stays in the tree (unplugged) and keeps holding Base Color, so Color keeps working and
# switching back to any other Look just plugs it in again. EEVEE feature (Shader to RGB): Cycles renders it flat.
TOON_PREFIX = "VR Toon "
TOON_RAMP = [(0.0, (0.28, 0.28, 0.32, 1.0)), (0.12, (0.62, 0.62, 0.66, 1.0)), (0.45, (1.0, 1.0, 1.0, 1.0))]


def look_values(name):
    """(Principled input values, material settings, toon) for a Look, defaults filled in."""
    for look_name, inputs, settings, toon in LOOKS:
        if look_name == name:
            values = dict(LOOK_INPUTS)
            values.update(inputs)
            st = dict(LOOK_SETTINGS)
            st.update(settings)
            return values, st, toon
    raise KeyError(name)
