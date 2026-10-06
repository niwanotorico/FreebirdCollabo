# SPDX-License-Identifier: GPL-2.0-or-later
"""
"Update available" notice test (single process, no network needed).

    blender -b --factory-startup --python tests/blender_runner.py -- tests/test_update_check.py
    ... -- tests/test_update_check.py live     # + ask the real GitHub Releases once (needs internet)

Checks:
  1  version parsing / comparison
  2  latest == installed  -> no notice
  3  latest  > installed  -> notice + Release page URL;  older release -> no notice
  4  offline / unreachable / broken JSON -> no notice, no exception
  5  background enable does not touch the network; Create / Join still go to the session as before
  6  start() through FREEBIRD_COLLAB_UPDATE_URL (file://) on the background thread
  7  notify only: the module has no download / install code
"""

import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bpy  # noqa: E402
import addon_utils  # noqa: E402


def main():
    live = "live" in sys.argv[1:]
    os.environ.pop("FREEBIRD_COLLAB_UPDATE_URL", None)
    mod = addon_utils.enable("freebird_collab", default_set=True, persistent=True)
    assert mod is not None, "add-on failed to enable"
    fc = sys.modules["freebird_collab"]
    uc = fc.update_check
    cur = fc.bl_info["version"]

    # 1
    assert uc.parse_version("v0.12.0") == (0, 12, 0)
    assert uc.parse_version("0.13") == (0, 13, 0)
    assert uc.parse_version("vr-studio-v0.3.0") is None and uc.parse_version("v1.0.0-beta") is None
    assert uc.parse_version("latest") is None and uc.parse_version(None) is None
    assert uc.is_newer((0, 12, 1), (0, 12, 0)) and uc.is_newer((0, 13, 0), (0, 12, 9))
    assert not uc.is_newer((0, 12, 0), (0, 12, 0)) and not uc.is_newer(None, (0, 12, 0))
    print("PASS 1 version parse / compare")

    # 2  (shaped like the real list today: v0.12.0 pre-release + a vr-studio release)
    same = "v" + ".".join(map(str, cur))
    other = {"tag_name": "vr-studio-v9.0.0", "prerelease": True, "html_url": "https://example.com/vr"}
    uc.check(cur, fetch=lambda u: [other, {"tag_name": same, "prerelease": True, "html_url": "https://example.com/s"}])
    assert uc.done and uc.latest_version is None
    print("PASS 2 same version -> no notice:", same, "(vr-studio tag ignored)")

    # 3
    newer = "v%d.%d.%d" % (cur[0], cur[1] + 1, 0)
    page = "https://github.com/niwanotorico/FreebirdCollabo/releases/tag/" + newer
    uc.check(cur, fetch=lambda u: [other, {"tag_name": same}, {"tag_name": newer, "prerelease": True, "html_url": page}])
    assert uc.latest_version == (cur[0], cur[1] + 1, 0) and uc.latest_tag == newer and uc.latest_url == page
    uc.check(cur, fetch=lambda u: [{"tag_name": "v0.1.0"}, {"tag_name": "v99.0.0", "draft": True}])
    assert uc.latest_version is None
    uc.check(cur, fetch=lambda u: {"tag_name": newer, "html_url": "javascript:alert(1)"})
    assert uc.latest_version and uc.latest_url == uc.RELEASES_PAGE
    print("PASS 3 newer release -> notice", uc.latest_tag, "; older / draft -> none; odd URL -> releases page")

    # 4
    def boom(u):
        raise OSError("offline")

    uc.check(cur, fetch=lambda u: {"tag_name": newer})
    uc.check(cur, fetch=boom)  # failure leaves the previous state as is (no crash)
    uc.latest_version = None
    uc.check(cur, fetch=boom)
    assert uc.done and uc.latest_version is None
    uc.check(cur, fetch=lambda u: "not a dict")
    assert uc.latest_version is None
    t0 = time.time()
    uc.check(cur, url="http://127.0.0.1:9/nothing")  # real urllib call to a closed port
    assert uc.latest_version is None and time.time() - t0 < uc.TIMEOUT + 2
    print("PASS 4 offline / unreachable / broken -> silently no notice")

    # 5
    assert bpy.app.background and not bpy.app.timers.is_registered(fc._update_check_poll)
    assert hasattr(bpy.types, "COLLAB_OT_open_update_page")
    calls = []
    real_create, real_join = fc._session.create_room, fc._session.join_room
    fc._session.create_room = lambda *a, **k: calls.append("create") or True
    fc._session.join_room = lambda *a, **k: calls.append("join") or True
    uc.latest_version, uc.latest_tag = (9, 9, 9), "v9.9.9"  # notice showing, user ignores it
    try:
        assert bpy.ops.collab.create_room() == {"FINISHED"}
        assert bpy.ops.collab.join_room() == {"FINISHED"}
        assert calls == ["create", "join"], calls
    finally:
        fc._session.create_room, fc._session.join_room = real_create, real_join
        uc.latest_version, uc.latest_tag = None, ""
    print("PASS 5 no network in background; Create / Join unaffected by the notice")

    # 6
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "fake_release.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump([{"tag_name": "v9.9.9", "html_url": "https://example.com/r"}], f)
        os.environ["FREEBIRD_COLLAB_UPDATE_URL"] = "file:///" + path.replace("\\", "/").lstrip("/")
        try:
            uc.start(cur)
            t0 = time.time()
            while not uc.done and time.time() - t0 < 10:
                time.sleep(0.05)
            assert uc.done and uc.latest_version == (9, 9, 9), (uc.done, uc.latest_version)
        finally:
            os.environ.pop("FREEBIRD_COLLAB_UPDATE_URL", None)
            uc.latest_version, uc.latest_tag = None, ""
    print("PASS 6 background thread via FREEBIRD_COLLAB_UPDATE_URL")

    # 7
    src = open(uc.__file__, encoding="utf-8").read()
    for bad in ("zipfile", "addon_install", "urlretrieve", "open(", "shutil"):
        assert bad not in src.replace("urlopen(", ""), bad
    print("PASS 7 notify only (no download / install code)")

    if live:
        best = uc.newest_release(uc._fetch(uc.RELEASES_API))  # raises if GitHub is unreachable
        uc.check(cur)
        print("LIVE GitHub newest add-on release:", best[1] if best else None, "installed", cur,
              "->", "notice " + uc.latest_tag if uc.latest_version else "no notice")

    print("ALL PASS")


if __name__ == "__main__":
    main()
