# SPDX-License-Identifier: GPL-2.0-or-later
"""
Release test for the VR Studio zip (Blender add-on that installs the Freebird plugin).
Every scenario runs in fresh Blender processes with HOME pointing at an empty fake user folder,
so the Blender add-ons folder and ~/.freebird are brand new and the dev checkout is never used.

    FREEBIRD_XR_DIR=<...>/scripts/addons/freebird_xr python3 tests/test_vr_studio_release.py [zip]
        (default: builds the zip with tools/build_vr_studio_release.py)

 layout   zip = one add-on folder: __init__.py + payload/vr_studio (plugin only) + README/LICENSE
 fresh    Install from Disk + enable on a PC without VR Studio (and without ~/.freebird)
          -> ~/.freebird/plugins/vr_studio is created, identical to the bundled plugin;
          "restart" -> Freebird's own loader shows Color / Look / View and they work;
          all vr_studio modules come from ~/.freebird/plugins (no dev folder, no zip needed)
 live     same, but with Freebird already running: the buttons appear without a restart
 update   an existing (modified) v0.3.0 / an older v0.2.0 is backed up intact, then replaced;
          a newer v0.4.0 is left alone; a failed swap puts the old copy back
 disable  disabling / removing the add-on leaves the plugin in place and working
 remove   "Remove VR Studio Plugin" deletes only the plugin folder; the add-on does not
          reinstall it on the next start
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ADDON = "vr_studio_installer"


# ----------------------------------------------------------------------------- zip layout
def check_zip(zip_path):
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        assert z.testzip() is None
        assert {n.split("/")[0] for n in names} == {ADDON}, "zip must contain exactly one add-on folder"
        assert f"{ADDON}/__init__.py" in names and f"{ADDON}/payload/vr_studio/__init__.py" in names
        assert not any("__pycache__" in n or n.endswith((".pyc", ".cmd")) for n in names), names
        payload = [n for n in names if n.startswith(f"{ADDON}/payload/")]
        assert all(n.endswith(".py") or ("/icons/" in n and n.endswith(".png")) for n in payload), payload
        assert len([n for n in payload if "/icons/" in n]) == 15
        rest = sorted(set(names) - set(payload))
        assert rest == sorted(f"{ADDON}/{x}" for x in ("__init__.py", "README.md", "LICENSE.txt")), rest
        init = z.read(f"{ADDON}/__init__.py").decode()
        for bad in ("color_section", "look_section", "palette", "materials"):
            assert bad not in init, f"setup add-on must not contain VR Studio features ({bad})"
        assert b"@VERSION@" not in z.read(f"{ADDON}/README.md")
    print(f"layout OK: {len(names)} files, {len(payload)} plugin files under {ADDON}/payload/")


# ----------------------------------------------------------------------------- child processes
PRELUDE = r'''
import os, sys, json, shutil, filecmp
FB, HOME, ZIP, DEV = os.environ["T_FB"], os.environ["T_HOME"], os.environ["T_ZIP"], os.environ["T_DEV"].split(os.pathsep)
assert os.path.expanduser("~") == HOME
import bpy, addon_utils
PLUG = os.path.join(HOME, ".freebird", "plugins", "vr_studio")
BACK = os.path.join(HOME, ".freebird", "plugin_backups")

def addon_dir():
    return os.path.join(bpy.utils.user_resource("SCRIPTS"), "addons", "vr_studio_installer")

def install_addon():
    assert bpy.ops.preferences.addon_install(filepath=ZIP, overwrite=True) == {"FINISHED"}
    assert os.path.isfile(os.path.join(addon_dir(), "__init__.py")), addon_dir()
    assert addon_dir().startswith(HOME)
    bpy.ops.preferences.addon_enable(module="vr_studio_installer")
    m = sys.modules["vr_studio_installer"]
    assert os.path.realpath(m.__file__).startswith(os.path.realpath(HOME)), m.__file__
    return m

def start_freebird():
    sys.path.insert(0, FB)
    from bl_xr import Image
    Image.base_dir = FB
    import freebird  # noqa
    from freebird import plugin_manager
    return plugin_manager

def same_tree(a, b):
    for d, dirs, names in os.walk(a):
        dirs[:] = [x for x in dirs if x != "__pycache__"]
        for n in names:
            if n.endswith(".pyc"):
                continue
            rel = os.path.relpath(os.path.join(d, n), a)
            if not filecmp.cmp(os.path.join(a, rel), os.path.join(b, rel), shallow=False):
                return False
    return True

def check_vr_studio_in_freebird(press=True):
    """Freebird's own loader + Plugins (CUSTOM) menu: Color / Look / View appear and work."""
    from freebird import api, plugin_manager
    from freebird.ui import main_menu as mm
    import bl_xr
    loaded = {p.plugin_id: p for p in plugin_manager.get_loaded_plugins()}
    assert "vr_studio" in loaded, f"not loaded: {list(loaded)}"
    mod = loaded["vr_studio"].module
    mm.custom_launcher.refresh()
    by_label = {b.tooltip.text: b for b in mm.custom_launcher.grid.child_nodes}
    for label in ("Color", "Look", "View"):
        assert label in by_label, f"{label} missing from Plugins menu: {list(by_label)}"
        assert by_label[label].icon.src.startswith(os.path.join(PLUG, "icons")), by_label[label].icon.src
    if press:
        for ob in bpy.context.view_layer.objects:
            ob.select_set(ob.name == "Cube")
        bpy.context.view_layer.objects.active = bpy.data.objects["Cube"]
        bpy.data.objects["Cube"].data.materials.clear()
        for label in ("Color", "Look", "View"):
            by_label[label].dispatch_event("pointer_main_press_end", None)
        secs = mod._sections
        assert {k: len(s.tiles) for k, s in secs.items()} == {"color": 32, "look": 8, "view": 4}
        secs["color"].tiles[4].dispatch_event("pointer_main_press_end", None)
        assert bpy.data.objects["Cube"].material_slots[0].material.name == "VR Cube"
        secs["look"].tiles[4].dispatch_event("pointer_main_press_end", None)
        secs["view"].tiles[0].dispatch_event("pointer_main_press_end", None)
        assert bpy.context.window_manager.xr_session_settings.shading.type == "WIREFRAME"
        mod._on_xr_end(None, "fb.xr_end", None)
    # nothing from the dev checkout / staged dev copies
    prefix = loaded["vr_studio"].module_name
    files = [os.path.realpath(m.__file__) for n, m in sys.modules.items() if n.startswith(prefix) and getattr(m, "__file__", None)]
    assert len(files) >= (8 if press else 1) and all(f.startswith(os.path.realpath(PLUG)) for f in files), files
    for p in sys.path:
        for d in DEV:
            assert not os.path.realpath(p or ".").startswith(os.path.realpath(d)), f"dev path on sys.path: {p}"
    print("  Freebird Plugins menu:", [l for l in by_label if l in ("Color", "Look", "View")], "- modules from", PLUG)

def write_fake_plugin(version, extra=None):
    """An installed plugin that differs from the bundled one."""
    m = sys.modules.get("vr_studio_installer")
    src = m.PAYLOAD if m else None
    shutil.copytree(src, PLUG)
    init = os.path.join(PLUG, "__init__.py")
    s = open(init, encoding="utf-8").read().replace('"version": (0, 3, 0)', f'"version": {version}')
    open(init, "w", encoding="utf-8").write(s)
    for rel, data in (extra or {}).items():
        p = os.path.join(PLUG, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").write(data)
'''

SCENARIOS = {
    # ---------------------------------------------------------------- 1. fresh PC
    "fresh_install": r'''
assert not os.path.exists(os.path.join(HOME, ".freebird"))
m = install_addon()
assert m.status()[0] == "missing"
m.auto_setup()                                   # what the timer does right after enabling
assert os.path.isfile(os.path.join(PLUG, "__init__.py")) and m.status()[0] == "current", m.status()
assert same_tree(m.PAYLOAD, PLUG) and not os.path.exists(BACK)
assert json.load(open(m.STATE_FILE))["result"] == "missing"
print("  installed ->", PLUG, "|", m._last_message)
bpy.ops.wm.save_userpref()
''',
    "fresh_restart": r'''
# "Blender restart": Freebird loads its plugins; VR Studio must be there. The zip is gone by now.
assert not os.path.exists(ZIP)
pm = start_freebird()
pm.load_plugins()
check_vr_studio_in_freebird()
m = sys.modules.get("vr_studio_installer")
if m is None:
    bpy.ops.preferences.addon_enable(module="vr_studio_installer"); m = sys.modules["vr_studio_installer"]
before = os.path.getmtime(os.path.join(PLUG, "__init__.py"))
m.auto_setup()                                   # second start: nothing to do
assert os.path.getmtime(os.path.join(PLUG, "__init__.py")) == before and m.status()[0] == "current"
''',
    # ---------------------------------------------------------------- live (Freebird already running)
    "live": r'''
os.makedirs(os.path.join(HOME, ".freebird", "plugins"))
pm = start_freebird(); pm.load_plugins()
from freebird import api
assert not [e for e in api.iter_launcher_buttons() if e.plugin_id == "vr_studio"]
m = install_addon(); m.auto_setup()
check_vr_studio_in_freebird(press=False)         # appeared without restarting Blender
assert "Plugins menu" in m._last_message, m._last_message
''',
    # ---------------------------------------------------------------- 2. existing installs
    "update_modified_same_version": r'''
import zipfile
os.makedirs(os.path.dirname(PLUG))
with zipfile.ZipFile(ZIP) as z:        # a v0.3.0 installed by hand earlier, with local extras
    for n in z.namelist():
        if n.startswith("vr_studio_installer/payload/vr_studio/"):
            p = os.path.join(PLUG, n.split("payload/vr_studio/")[1]); os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "wb").write(z.read(n))
open(os.path.join(PLUG, "panel.py"), "w").write("# old experiment\n")
os.makedirs(os.path.join(PLUG, "__pycache__")); open(os.path.join(PLUG, "__pycache__", "x.pyc"), "wb").write(b"x")
open(os.path.join(PLUG, "icons", "color.png"), "ab").write(b"\0")   # re-saved icon
snapshot = {os.path.relpath(os.path.join(d, n), PLUG): open(os.path.join(d, n), "rb").read() for d, _, ns in os.walk(PLUG) for n in ns}
m = install_addon()
assert m.status()[0] == "modified", m.status()
m.auto_setup()
assert m.status()[0] == "current" and same_tree(m.PAYLOAD, PLUG)
assert not os.path.exists(os.path.join(PLUG, "panel.py"))
backups = os.listdir(BACK); assert len(backups) == 1 and backups[0].startswith("vr_studio_v0.3.0_"), backups
bk = os.path.join(BACK, backups[0])
got = {os.path.relpath(os.path.join(d, n), bk): open(os.path.join(d, n), "rb").read() for d, _, ns in os.walk(bk) for n in ns}
assert got == snapshot, "backup must be the untouched old copy"
assert not [e for e in os.listdir(os.path.dirname(PLUG)) if e != "vr_studio"], os.listdir(os.path.dirname(PLUG))
print("  old copy backed up intact ->", bk)
pm = start_freebird(); pm.load_plugins(); check_vr_studio_in_freebird(press=False)
''',
    "update_older": r'''
m = install_addon()
write_fake_plugin((0, 2, 0))
assert m.status() == ("older", (0, 2, 0))
m.auto_setup()
assert m.status()[0] == "current" and os.listdir(BACK)[0].startswith("vr_studio_v0.2.0_")
assert m.read_version(os.path.join(BACK, os.listdir(BACK)[0])) == (0, 2, 0)
''',
    "keep_newer": r'''
m = install_addon()
write_fake_plugin((0, 4, 0))
m.auto_setup()
assert m.status() == ("newer", (0, 4, 0)) and not os.path.exists(BACK), m.status()
assert "left unchanged" in m._last_message
bpy.ops.vr_studio_setup.install("EXEC_DEFAULT")   # explicit button (after its confirm) still can replace
assert m.status()[0] == "current" and m.read_version(os.path.join(BACK, os.listdir(BACK)[0])) == (0, 4, 0)
''',
    "swap_failure_restores": r'''
m = install_addon()
write_fake_plugin((0, 2, 0))
real = os.replace
def boom(a, b): raise PermissionError("file in use")
m.os.replace = boom
try:
    m.install_plugin(); raise AssertionError("should fail")
except PermissionError:
    pass
finally:
    m.os.replace = real
assert m.status() == ("older", (0, 2, 0)), m.status()   # old copy is back in place
assert not os.path.exists(os.path.join(os.path.dirname(PLUG), ".vr_studio_installing"))
assert os.listdir(BACK) == []
''',
    # ---------------------------------------------------------------- 3. disable / remove the add-on
    "disable_keeps_plugin": r'''
m = install_addon(); m.auto_setup()
bpy.ops.preferences.addon_disable(module="vr_studio_installer")
assert os.path.isfile(os.path.join(PLUG, "__init__.py"))
# = Preferences > Add-ons > Uninstall (its operator needs a UI area, so do what it does: disable + delete folder)
addon_utils.disable("vr_studio_installer", default_set=True)
shutil.rmtree(addon_dir())
assert not os.path.exists(addon_dir()), "add-on folder should be gone"
assert os.path.isfile(os.path.join(PLUG, "__init__.py")) and same_tree(os.path.join(PLUG), PLUG)
pm = start_freebird(); pm.load_plugins(); check_vr_studio_in_freebird(press=False)
print("  add-on disabled + removed, plugin still loads in Freebird")
''',
    # ---------------------------------------------------------------- 4. remove the plugin
    "remove_plugin": r'''
m = install_addon(); m.auto_setup()
assert "invoke_confirm" in m.VRSTUDIO_OT_remove.invoke.__code__.co_names   # the button asks first
assert bpy.ops.vr_studio_setup.remove("EXEC_DEFAULT") == {"FINISHED"}          # = user pressed "Remove"
assert not os.path.exists(PLUG) and os.path.isdir(os.path.dirname(PLUG))
assert m.status()[0] == "missing"
m.auto_setup()                                      # add-on still enabled: must NOT reinstall by itself
assert not os.path.exists(PLUG)
print("  removed:", m._last_message)
''',
}

ORDER = [["fresh_install", "fresh_restart"], ["live"], ["update_modified_same_version"], ["update_older"],
         ["keep_newer"], ["swap_failure_restores"], ["disable_keeps_plugin"], ["remove_plugin"]]


def run_group(names, zip_path, fb):
    tmp = tempfile.mkdtemp(prefix="vrs_rel_")
    try:
        home = os.path.join(tmp, "home")
        os.makedirs(os.path.join(home, "Downloads"))
        zcopy = os.path.join(home, "Downloads", os.path.basename(zip_path))
        shutil.copy(zip_path, zcopy)  # the user's download; deleted before a "restart"
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "BLENDER_USER_SCRIPTS")}
        env.update(HOME=home, USERPROFILE=home, T_FB=fb, T_HOME=home, T_ZIP=zcopy,
                   T_DEV=os.pathsep.join([ROOT] + [p for p in os.environ.get("VRS_DEV_COPIES", "").split(os.pathsep) if p]))
        for i, name in enumerate(names):
            if i > 0:
                os.remove(zcopy)
            script = os.path.join(tmp, f"{name}.py")
            with open(script, "w", encoding="utf-8") as f:
                f.write(PRELUDE + textwrap.dedent(SCENARIOS[name]) + '\nprint("<<OK>>")\n')
            r = subprocess.run([sys.executable, script], cwd=tmp, env=env, capture_output=True, text=True, timeout=300)
            ok = r.returncode == 0 and "<<OK>>" in r.stdout
            lines = [l for l in r.stdout.splitlines() if l.startswith("  ")]
            print(f"[{'PASS' if ok else 'FAIL'}] {name}")
            for l in lines:
                print(l)
            if not ok:
                print(r.stdout[-2500:], r.stderr[-4000:])
                return False
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    fb = os.environ.get("FREEBIRD_XR_DIR")
    if not fb or not os.path.isdir(os.path.join(fb, "freebird")):
        raise SystemExit("set FREEBIRD_XR_DIR to Freebird's freebird_xr folder")
    if len(sys.argv) > 1:
        zip_path = os.path.abspath(sys.argv[1])
    else:
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import build_vr_studio_release

        zip_path = build_vr_studio_release.build()
    check_zip(zip_path)
    ok = all([run_group(g, zip_path, fb) for g in ORDER])
    print("=== VR STUDIO RELEASE:", "PASS ===" if ok else "FAIL ===")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
