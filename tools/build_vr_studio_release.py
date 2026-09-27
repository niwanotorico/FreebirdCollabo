# SPDX-License-Identifier: GPL-2.0-or-later
"""
Build the end-user zip for VR Studio: a Blender add-on you install with
Edit > Preferences > Add-ons > Install from Disk.

    python tools/build_vr_studio_release.py      # -> dist/vr_studio-v<version>.zip (+ release notes, .sha256)

Zip layout (Blender extracts the zip into its add-ons folder, so everything lives in ONE folder):

    vr_studio_installer/
        __init__.py            the setup add-on (install / update / remove only, no VR Studio features)
        payload/vr_studio/     the Freebird plugin, copied unchanged from freebird_plugin/vr_studio
                               (only git-tracked .py + icons/*.png; no tests, docs, caches)
        README.md  LICENSE.txt

The version comes from fb_info["version"] in freebird_plugin/vr_studio/__init__.py and must match
bl_info["version"] of the setup add-on. Standard library only.
"""

import ast
import datetime
import hashlib
import os
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "freebird_plugin", "vr_studio")
RELEASE = os.path.join(ROOT, "release")
ADDON_SRC = os.path.join(RELEASE, "vr_studio_installer")
NOTES = os.path.join(RELEASE, "vr_studio")
DIST = os.path.join(ROOT, "dist")
ADDON = "vr_studio_installer"

BUILD_TIME = datetime.datetime.now().timetuple()[:6]


def _dict_literal(path, name):
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise SystemExit(f"{name} not found in {path}")


def versions():
    plugin = tuple(_dict_literal(os.path.join(PLUGIN, "__init__.py"), "fb_info")["version"])
    addon = tuple(_dict_literal(os.path.join(ADDON_SRC, "__init__.py"), "bl_info")["version"])
    if plugin != addon:
        raise SystemExit(f"version mismatch: plugin fb_info {plugin} vs add-on bl_info {addon}")
    return ".".join(str(x) for x in plugin)


def plugin_files():
    """Relative paths (posix) inside freebird_plugin/vr_studio to ship."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "freebird_plugin/vr_studio"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.split()
        files = [p[len("freebird_plugin/vr_studio/"):] for p in out]
        source = "git"
    except Exception:
        files = []
        for d, dirs, names in os.walk(PLUGIN):
            dirs[:] = [x for x in dirs if x != "__pycache__"]
            files += [os.path.relpath(os.path.join(d, n), PLUGIN).replace(os.sep, "/") for n in names]
        source = "folder (not a git checkout - check for stray files!)"
    keep = sorted(p for p in files if p.endswith(".py") or (p.startswith("icons/") and p.endswith(".png")))
    if "__init__.py" not in keep:
        raise SystemExit("vr_studio/__init__.py missing")
    return keep, source


def _text(path, version):
    with open(path, encoding="utf-8") as f:
        return f.read().replace("\r\n", "\n").replace("@VERSION@", version)


def _add(z, arcname, data):
    info = zipfile.ZipInfo(arcname, BUILD_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    z.writestr(info, data)


def build():
    version = versions()
    files, source = plugin_files()
    os.makedirs(DIST, exist_ok=True)
    zip_path = os.path.join(DIST, f"vr_studio-v{version}.zip")

    with zipfile.ZipFile(zip_path, "w") as z:
        with open(os.path.join(ADDON_SRC, "__init__.py"), "rb") as f:
            _add(z, f"{ADDON}/__init__.py", f.read())
        for rel in files:
            with open(os.path.join(PLUGIN, rel), "rb") as f:
                _add(z, f"{ADDON}/payload/vr_studio/{rel}", f.read())
        _add(z, f"{ADDON}/README.md", _text(os.path.join(NOTES, "README.md"), version).encode("utf-8"))
        with open(os.path.join(ROOT, "LICENSE"), encoding="utf-8") as f:
            _add(z, f"{ADDON}/LICENSE.txt", f.read().encode("utf-8"))

    with open(zip_path, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    notes = _text(os.path.join(NOTES, "RELEASE_NOTES.md"), version).replace("@SHA256@", sha)
    with open(os.path.join(DIST, f"RELEASE_NOTES-vr_studio-v{version}.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(notes)
    with open(zip_path + ".sha256", "w", encoding="ascii", newline="\n") as f:
        f.write(f"{sha}  {os.path.basename(zip_path)}\n")

    print(f"VR Studio v{version}: add-on + {len(files)} plugin files (from {source})")
    print(f"  {zip_path}  ({os.path.getsize(zip_path)} bytes)")
    print(f"  sha256 {sha}")
    return zip_path


if __name__ == "__main__":
    sys.exit(0 if build() else 1)
