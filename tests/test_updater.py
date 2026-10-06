# SPDX-License-Identifier: GPL-2.0-or-later
"""
In-app updater test (single process, no network; every file operation happens in temp folders).

    blender -b --factory-startup --python tests/blender_runner.py -- tests/test_updater.py

Checks:
  1  official asset: exact name freebird_collab_addon.zip, one only, uploaded, https; sha256 from digest
  2  resolve_latest: newer release + asset, vr-studio tag ignored, fresh lookup
  3  zip safety: zip-slip / absolute / drive / outside folder / symlink / missing __init__ are refused
  4  download: only the official URL, size cap, size / SHA-256 mismatch, redirect to another host refused
  5  verify: syntax error and wrong version refused; the repo's real distribution zip passes
  6  swap: success, and rollback keeps the old folder when the rename / version check fails
  7  run_update end to end: success replaces the folder and leaves no temp / .old; any failure keeps the old one
  8  refuses dev installs (git checkout / link)
  9  Blender: button disabled in a room, enabled otherwise; nothing starts without a click
"""

import hashlib
import io
import os
import stat
import sys
import tempfile
import types
import zipfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bpy  # noqa: E402
import addon_utils  # noqa: E402


def make_addon_source(version, extra=""):
    return f'bl_info = {{"name": "x", "version": {tuple(version)!r}, "blender": (4, 2, 0)}}\n{extra}\n'


def make_zip(path, version=(9, 9, 9), files=None, extra_entries=()):
    """Write a distribution-shaped zip. files: {relative path: text}; extra_entries: [(arcname, bytes, attr)]."""
    files = files if files is not None else {"__init__.py": make_addon_source(version), "mod.py": "X = 1\n"}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, text in files.items():
            z.writestr("freebird_collab/" + rel, text)
        for name, data, attr in extra_entries:
            i = zipfile.ZipInfo(name)
            i.external_attr = attr << 16
            z.writestr(i, data)
    return Path(path)


def expect_error(fn, *a, **k):
    try:
        fn(*a, **k)
    except fc_updater.UpdateError as e:
        return str(e)
    raise AssertionError("UpdateError expected")


def installed(addons, version, marker="old"):
    pkg = Path(addons) / "freebird_collab"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(make_addon_source(version), encoding="utf-8")
    (pkg / "marker.txt").write_text(marker, encoding="utf-8")
    return pkg


def fake_opener(data):
    def opener(url):
        r = io.BytesIO(data)
        return r
    return opener


def main():
    global fc_updater
    addon_utils.enable("freebird_collab", default_set=True, persistent=True)
    fc = sys.modules["freebird_collab"]
    up, uc = fc.updater, fc.update_check
    fc_updater = up
    cur = fc.bl_info["version"]
    tmp = Path(tempfile.mkdtemp(prefix="collab_updater_test_"))

    # 1
    good = {"name": "freebird_collab_addon.zip", "state": "uploaded", "size": 123,
            "browser_download_url": up.DOWNLOAD_PREFIX + "v9.9.9/freebird_collab_addon.zip", "digest": "sha256:ABCDEF"}
    a = uc.find_asset({"assets": [good]})
    assert a == {"url": good["browser_download_url"], "size": 123, "sha256": "abcdef"}, a
    assert uc.find_asset({"assets": [dict(good, digest=None)]})["sha256"] == ""
    assert uc.find_asset({"assets": [good, dict(good)]}) is None  # two matches -> ambiguous
    assert uc.find_asset({"assets": [dict(good, name="vr_studio-v0.3.0.zip")]}) is None
    assert uc.find_asset({"assets": [dict(good, name="freebird_collab_addon.zip.sig")]}) is None
    assert uc.find_asset({"assets": [dict(good, state="starter")]}) is None
    assert uc.find_asset({"assets": [dict(good, browser_download_url="http://x/freebird_collab_addon.zip")]}) is None
    assert uc.find_asset({}) is None and uc.find_asset({"assets": None}) is None
    print("PASS 1 asset must be exactly one freebird_collab_addon.zip")

    # 2
    newer = "v%d.%d.%d" % (cur[0], cur[1] + 1, 0)
    rels = [
        {"tag_name": "vr-studio-v9.0.0", "assets": [good]},
        {"tag_name": "v0.1.0"},
        {"tag_name": newer, "html_url": "https://github.com/x/y", "assets": [good]},
    ]
    f = uc.resolve_latest(cur, fetch=lambda u: rels)
    assert f["tag"] == newer and f["version"] == (cur[0], cur[1] + 1, 0) and f["asset"]["sha256"] == "abcdef"
    assert uc.resolve_latest(cur, fetch=lambda u: [{"tag_name": "v%d.%d.%d" % cur}]) is None
    assert uc.resolve_latest(cur, fetch=lambda u: [{"tag_name": newer}])["asset"] is None
    print("PASS 2 resolve_latest")

    # 3
    z = make_zip(tmp / "ok.zip")
    with zipfile.ZipFile(z) as zf:
        assert [r for _, r, _d in up.safe_entries(zf)] == ["__init__.py", "mod.py"]
    bad_entries = {
        "zip-slip": [("freebird_collab/../evil.py", b"x", 0o100644)],
        "zip-slip nested": [("freebird_collab/a/../../evil.py", b"x", 0o100644)],
        "absolute": [("/etc/evil.py", b"x", 0o100644)],
        "drive": [("C:/evil.py", b"x", 0o100644)],
        "backslash": [("freebird_collab\\..\\evil.py", b"x", 0o100644)],
        "outside folder": [("other/evil.py", b"x", 0o100644)],
        "symlink": [("freebird_collab/link.py", b"/etc/passwd", stat.S_IFLNK | 0o777)],
    }
    for label, extra in bad_entries.items():
        zp = make_zip(tmp / "bad.zip", extra_entries=extra)
        with zipfile.ZipFile(zp) as zf:
            expect_error(up.safe_entries, zf)
        assert not (tmp / "evil.py").exists()
        print("   refused:", label)
    zp = make_zip(tmp / "noinit.zip", files={"mod.py": "X=1\n"})
    with zipfile.ZipFile(zp) as zf:
        expect_error(up.safe_entries, zf)
    expect_error(up.extract_verified, tmp / "missing.zip", tmp / "n1" / "freebird_collab", (9, 9, 9))
    (tmp / "garbage.zip").write_bytes(b"not a zip")
    expect_error(up.extract_verified, tmp / "garbage.zip", tmp / "n2" / "freebird_collab", (9, 9, 9))
    print("PASS 3 zip-slip / absolute / drive / outside / symlink / no __init__ / garbage refused")

    # 4
    data = z.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    url = up.DOWNLOAD_PREFIX + "v9.9.9/freebird_collab_addon.zip"
    dest = tmp / "dl.zip"
    up.download(url, dest, len(data), sha, opener=fake_opener(data))
    assert dest.read_bytes() == data and not Path(str(dest) + ".part").exists()
    assert "official" in expect_error(up.download, "https://evil.example/freebird_collab_addon.zip", tmp / "x.zip", opener=fake_opener(data))
    assert "official" in expect_error(up.download, "http://github.com/niwanotorico/FreebirdCollabo/releases/download/a", tmp / "x.zip", opener=fake_opener(data))
    assert "SHA-256" in expect_error(up.download, url, tmp / "x.zip", len(data), "0" * 64, opener=fake_opener(data))
    assert "size" in expect_error(up.download, url, tmp / "x.zip", len(data) + 1, "", opener=fake_opener(data))
    old_cap, up.MAX_ZIP_BYTES = up.MAX_ZIP_BYTES, 10
    try:
        assert "larger" in expect_error(up.download, url, tmp / "x.zip", opener=fake_opener(data))
    finally:
        up.MAX_ZIP_BYTES = old_cap
    assert not (tmp / "x.zip").exists()
    assert up._host_ok("https://objects.githubusercontent.com/x") and up._host_ok("https://release-assets.githubusercontent.com/x")
    assert up._host_ok("https://github.com/x")
    for bad in ("https://evil.com/x", "http://github.com/x", "https://github.com.evil.com/x", "https://notgithub.com/x"):
        assert not up._host_ok(bad), bad
    h = up._SafeRedirect()
    expect_error(h.redirect_request, None, None, 302, "Found", {}, "https://evil.example/x")
    print("PASS 4 download: official URL only, size cap, size / SHA-256 mismatch, redirect host check")

    # 5
    syn = make_zip(tmp / "syntax.zip", files={"__init__.py": make_addon_source((9, 9, 9)), "mod.py": "def (:\n"})
    assert "compile" in expect_error(up.extract_verified, syn, tmp / "e1" / "freebird_collab", (9, 9, 9))
    assert "version" in expect_error(up.extract_verified, z, tmp / "e2" / "freebird_collab", (9, 9, 8))
    nobl = make_zip(tmp / "nobl.zip", files={"__init__.py": "X = 1\n"})
    assert "bl_info" in expect_error(up.extract_verified, nobl, tmp / "e3" / "freebird_collab", (9, 9, 9))
    assert up.extract_verified(z, tmp / "e4" / "freebird_collab", (9, 9, 9)) == (9, 9, 9)
    real = Path(ROOT) / "freebird_collab_addon.zip"
    rv = up.extract_verified(real, tmp / "e5" / "freebird_collab", cur)
    assert rv == tuple(cur), rv  # the committed distribution zip is in the format the updater accepts
    print("PASS 5 verify: syntax / version / bl_info refused; real distribution zip", rv, "accepted")

    # 6
    addons = tmp / "sw1"
    pkg = installed(addons, (0, 1, 0))
    new = make_zip(tmp / "n.zip", version=(0, 2, 0))
    up.extract_verified(new, tmp / "sw1_new" / "freebird_collab", (0, 2, 0))
    up.swap(pkg, tmp / "sw1_new" / "freebird_collab", (0, 2, 0))
    assert (pkg / "mod.py").exists() and not (pkg / "marker.txt").exists() and not (addons / up.OLD_NAME).exists()

    def rollback_case(name, fail_on, expected_version=(0, 2, 0)):
        addons = tmp / name
        pkg = installed(addons, (0, 1, 0))
        up.extract_verified(new, tmp / (name + "_new") / "freebird_collab", (0, 2, 0))
        real_rename, calls = up._rename, []

        def flaky(src, dst):
            calls.append((Path(src).name, Path(dst).name))
            if fail_on(len(calls), src, dst):
                raise OSError("simulated failure")
            return real_rename(src, dst)

        up._rename = flaky
        try:
            expect_error(up.swap, pkg, tmp / (name + "_new") / "freebird_collab", expected_version)
        finally:
            up._rename = real_rename
        assert (pkg / "marker.txt").read_text() == "old", name  # old add-on is back, untouched
        assert not (addons / up.OLD_NAME).exists(), name

    rollback_case("sw2", lambda n, s, d: n == 1)          # cannot move the current folder aside
    rollback_case("sw3", lambda n, s, d: n == 2)          # cannot move the new folder in
    rollback_case("sw4", lambda n, s, d: False, (7, 7, 7))  # installed version check fails
    print("PASS 6 swap + rollback (old folder kept in all three failure points)")

    # 7
    def resolve_for(version):
        def resolve(current):
            return {"version": version, "tag": "v%d.%d.%d" % version,
                    "asset": {"url": url, "size": len(data), "sha256": sha}}
        return resolve

    addons = tmp / "e2e"
    pkg = installed(addons, (0, 1, 0))
    assert up.run_update((0, 1, 0), pkg, resolve=resolve_for((9, 9, 9)), opener=fake_opener(data)) is True
    assert up.state == "done" and "restart Blender" in up.message, (up.state, up.message)
    assert (pkg / "mod.py").exists() and not (pkg / "marker.txt").exists()
    assert sorted(p.name for p in addons.iterdir()) == ["freebird_collab"], list(addons.iterdir())
    print("PASS 7a success:", up.message)

    def fails(label, **kw):
        addons = tmp / ("fail_" + label)
        pkg = installed(addons, (0, 1, 0))
        ok = up.run_update((0, 1, 0), pkg, **kw)
        assert ok is False and up.state == "error", (label, up.state, up.message)
        assert (pkg / "marker.txt").read_text() == "old", label
        assert sorted(p.name for p in addons.iterdir()) == ["freebird_collab"], (label, list(addons.iterdir()))
        print("   kept old version on:", label, "->", up.message)

    bad_sha = lambda c: {"version": (9, 9, 9), "tag": "v9.9.9", "asset": {"url": url, "size": len(data), "sha256": "0" * 64}}
    fails("sha", resolve=bad_sha, opener=fake_opener(data))
    fails("version", resolve=resolve_for((9, 9, 8)), opener=fake_opener(data))
    fails("noasset", resolve=lambda c: {"version": (9, 9, 9), "tag": "v9.9.9", "asset": None}, opener=fake_opener(data))
    fails("uptodate", resolve=lambda c: None, opener=fake_opener(data))
    fails("garbage", resolve=lambda c: {"version": (9, 9, 9), "tag": "v9.9.9", "asset": {"url": url, "size": 0, "sha256": ""}},
          opener=fake_opener(b"garbage"))

    def offline(u):
        raise OSError("offline")

    fails("offline", resolve=resolve_for((9, 9, 9)), opener=offline)

    def no_network(c):
        raise OSError("offline")

    fails("resolve_offline", resolve=no_network, opener=fake_opener(data))
    print("PASS 7b every failure keeps the old add-on and leaves no temp / .old")

    # 8
    gitroot = tmp / "dev"
    (gitroot / ".git").mkdir(parents=True)
    pkg = installed(gitroot, (0, 1, 0))
    assert "git" in up.blocked_reason(pkg)
    assert up.run_update((0, 1, 0), pkg, resolve=resolve_for((9, 9, 9)), opener=fake_opener(data)) is False
    assert (pkg / "marker.txt").exists() and up.state == "error"
    plain = installed(tmp / "plain", (0, 1, 0))
    assert up.blocked_reason(plain) is None
    link_root = tmp / "linked"
    link_root.mkdir()
    try:
        os.symlink(plain, link_root / "freebird_collab", target_is_directory=True)
        assert "link" in up.blocked_reason(link_root / "freebird_collab")
    except OSError:
        print("   (symlink not permitted here; link case skipped)")
    print("PASS 8 development installs refused:", up.blocked_reason(Path(ROOT) / "freebird_collab"))

    # 9
    up._set("idle")
    assert not bpy.app.timers.is_registered(fc._update_install_poll) and up.state == "idle"  # nothing runs by itself
    uc.latest_version, uc.latest_tag = (9, 9, 9), "v9.9.9"
    uc.latest_asset = {"url": url, "size": len(data), "sha256": sha}
    fc._block_reason = ""  # pretend this is a normal install
    started = []
    real_start = up.start
    up.start = lambda cur: started.append(cur) or True
    try:
        assert bpy.ops.collab.update_addon.poll()
        real_link = fc._session.link
        fc._session.link = types.SimpleNamespace(connected=True)  # in a room
        assert fc._session.active
        assert not bpy.ops.collab.update_addon.poll()
        try:
            bpy.ops.collab.update_addon()
            raise AssertionError("operator must not run in a room")
        except RuntimeError:
            pass
        assert started == []
        fc._session.link = real_link
        assert not fc._session.active
        up._set("running")
        assert not bpy.ops.collab.update_addon.poll()  # no second start
        up._set("idle")
        fc._block_reason = "installed as a link (development setup)"
        assert not bpy.ops.collab.update_addon.poll()  # dev install -> Release page button instead
        fc._block_reason = ""
        assert bpy.ops.collab.update_addon() == {"FINISHED"} and started == [cur]
    finally:
        up.start = real_start
        fc._session.link = None
        fc._block_reason = None
        uc.latest_version, uc.latest_tag, uc.latest_asset = None, "", None
        up._set("idle")
        if bpy.app.timers.is_registered(fc._update_install_poll):
            bpy.app.timers.unregister(fc._update_install_poll)
    print("PASS 9 button: disabled in a room / while running / dev install; starts only on click")

    print("ALL PASS")


if __name__ == "__main__":
    main()
