# SPDX-License-Identifier: GPL-2.0-or-later
"""
Load the network-only modules of the add-on (protocol, ws, hub, link) WITHOUT
importing freebird_collab/__init__.py, which needs bpy (Blender).  This lets the
relay scripts run on any plain Python 3 install.

    from _netlib import hub, link, protocol
"""

import importlib.util
import os
import sys
import types

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PKG = "collab_net"  # synthetic package name; relative imports inside the modules resolve to it


def _load():
    if PKG in sys.modules:
        return sys.modules[PKG]
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [os.path.join(ROOT, "freebird_collab")]
    pkg.__package__ = PKG
    sys.modules[PKG] = pkg
    for name in ("protocol", "ws", "hub", "link"):  # order matters (dependencies first)
        path = os.path.join(ROOT, "freebird_collab", f"{name}.py")
        spec = importlib.util.spec_from_file_location(f"{PKG}.{name}", path)
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = PKG
        sys.modules[f"{PKG}.{name}"] = mod
        spec.loader.exec_module(mod)
        setattr(pkg, name, mod)
    return pkg


_pkg = _load()
protocol, ws, hub, link = _pkg.protocol, _pkg.ws, _pkg.hub, _pkg.link
