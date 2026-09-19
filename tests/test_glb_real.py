# SPDX-License-Identifier: GPL-2.0-or-later
"""
Reproduction with real GLB assets: GUEST imports each file while connected,
HOST must end up with every Base-Color-textured material wired to an image.

    COLLAB_GLBS=/path/a.glb,/path/b.glb python3 tests/test_glb_real.py direct
    (order matters: files are imported in the given order, in one session)
"""

import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_data import _idle, _wait  # noqa: E402
from test_sync import MODE_URLS, PORT, RELAY_URL, ROOT, STATE, _read_state, _write_state  # noqa: E402

GLBS = [p for p in os.environ.get("COLLAB_GLBS", "").split(",") if p]
# who imports each file: "guest" (default) or "host", comma separated, e.g. COLLAB_GLB_SIDES=host,guest
SIDES = (os.environ.get("COLLAB_GLB_SIDES", "").split(",") + ["guest"] * len(GLBS))[: len(GLBS)]
SIDES = [x or "guest" for x in SIDES]
# host imported (and deleted) these files BEFORE the room existed -> orphan packed images stay in bpy.data.images
PRE_HOST = [p for p in os.environ.get("COLLAB_GLB_PRE_HOST", "").split(",") if p]
# simulate broken dedupe bookkeeping: the importing side never pushes images proactively,
# so the receiver must recover through img_need
NO_PUSH = os.environ.get("COLLAB_GLB_NO_PUSH") == "1"


def _textured_report(prefix_objects):
    """For each object: list of (material, has_base_color_image, image size, has uv)."""
    import bpy

    from freebird_collab import object_data

    out = {}
    for name in prefix_objects:
        ob = bpy.data.objects.get(name)
        if ob is None or ob.type != "MESH":
            out[name] = None
            continue
        rows = []
        for slot in ob.material_slots:
            m = slot.material
            img = object_data._base_color_image(m) if m else None
            rows.append((m.name if m else None, img is not None, tuple(img.size) if img else None))
        out[name] = (rows, ob.data.uv_layers.active is not None, len(ob.data.vertices))
    return out


def _import_and_report(s, path):
    import bpy

    before = set(bpy.data.objects.keys())
    bpy.ops.object.select_all(action="DESELECT")
    bpy.ops.import_scene.gltf(filepath=path)
    bpy.context.view_layer.update()
    new = [n for n in bpy.data.objects.keys() if n not in before and bpy.data.objects[n].type == "MESH"]
    rep = _textured_report(new)
    expected = {n: [sum(1 for _, h, _ in r[0] if h), r[2]] for n, r in rep.items() if r}
    _idle(s, 1.0)
    return expected


def _wait_received(s, expected, label):
    def done():
        rep = _textured_report(expected)
        for name, (n_tex, n_verts) in expected.items():
            r = rep.get(name)
            if r is None or r[2] != n_verts or not r[1]:
                return False
            if sum(1 for _, has, _ in r[0] if has) != n_tex:
                return False
        return True

    _wait(done, 300, s, f"received textured {label}")
    rep = _textured_report(expected)
    return {k: (v[2], sum(1 for _, h, _ in v[0] if h), "uv" if v[1] else "NO-UV") for k, v in rep.items()}


def run_host(mode):
    import bpy

    sys.path.insert(0, ROOT)
    from freebird_collab.session import CollabSession

    s = CollabSession()
    for path in PRE_HOST:  # a previous try: import, then delete the objects the UI way (data blocks linger)
        bpy.ops.import_scene.gltf(filepath=path)
        bpy.ops.object.select_all(action="SELECT")
        bpy.ops.object.delete()
        print("HOST pre-imported+deleted", os.path.basename(path), "- images lingering:", len(bpy.data.images))
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(tempfile.gettempdir(), "collab_glbreal_master.blend"))
    if mode == "direct":
        assert s.create_room("DIRECT", "Host", (1, 0.5, 0.2), direct_port=PORT)
    else:
        assert s.create_room("RELAY", "Host", (1, 0.5, 0.2), relay_url=RELAY_URL)
    _wait(lambda: s.uid is not None, 5, s, "welcome")
    _write_state(room=s.room)
    _wait(lambda: _read_state().get("guest_ready"), 30, s, "guest ready")
    if NO_PUSH:
        s._send_images = lambda mats: None

    for i, path in enumerate(GLBS):
        key = f"glb{i}"
        if SIDES[i] == "host":
            expected = _import_and_report(s, path)
            print(f"HOST imported {os.path.basename(path)}:", expected)
            _write_state(**{key + "_objects": expected})
            _wait(lambda: _read_state().get(key + "_guest_ok"), 300, s, "guest ok")
            continue
        _wait(lambda: _read_state().get(key + "_objects"), 120, s, f"guest imported {path}")
        expected = _read_state()[key + "_objects"]  # {name: [n_textured_slots, n_verts]}
        print(f"HOST OK {os.path.basename(path)}:", _wait_received(s, expected, os.path.basename(path)))
        _write_state(**{key + "_host_ok": True})
    print("HOST images with collab_id:", len([im for im in bpy.data.images if im.get("collab_id")]))
    _write_state(host_done=True)
    _idle(s, 0.5)
    s.leave()
    print("HOST PASS")


def run_guest(mode):
    import bpy

    sys.path.insert(0, ROOT)
    from freebird_collab.session import CollabSession

    s = CollabSession()
    t0 = time.time()
    while not _read_state().get("room") and time.time() - t0 < 20:
        time.sleep(0.05)
    code = _read_state()["room"]
    if mode == "direct":
        assert s.join_room("DIRECT", "Guest", (0.2, 0.6, 1), direct_host="127.0.0.1", direct_port=PORT)
    else:
        assert s.join_room("RELAY", "Guest", (0.2, 0.6, 1), code=code, relay_url=RELAY_URL)
    _wait(lambda: s._scene_loaded_from_host, 20, s, "scene from host")
    _write_state(guest_ready=True)
    if NO_PUSH:
        s._send_images = lambda mats: None

    for i, path in enumerate(GLBS):
        key = f"glb{i}"
        if SIDES[i] == "host":
            _wait(lambda: _read_state().get(key + "_objects"), 120, s, f"host imported {path}")
            expected = _read_state()[key + "_objects"]
            print(f"GUEST OK {os.path.basename(path)}:", _wait_received(s, expected, os.path.basename(path)))
            _write_state(**{key + "_guest_ok": True})
            continue
        expected = _import_and_report(s, path)
        print(f"GUEST imported {os.path.basename(path)}:", expected)
        _write_state(**{key + "_objects": expected})
        _wait(lambda: _read_state().get(key + "_host_ok"), 300, s, "host ok")
    print("GUEST tx:", {k: tuple(v) for k, v in s.stats["tx_by_type"].items()})
    _wait(lambda: _read_state().get("host_done"), 60, s, "host done")
    s.leave()
    print("GUEST PASS")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "direct"
    role = sys.argv[2] if len(sys.argv) > 2 else None
    if role == "host":
        return run_host(mode)
    if role == "guest":
        return run_guest(mode)
    if not GLBS:
        print("set COLLAB_GLBS=a.glb,b.glb")
        sys.exit(2)
    import glob

    for f in glob.glob(STATE + "*"):
        os.remove(f)
    procs = []
    env = dict(os.environ)
    if mode in MODE_URLS:
        env["COLLAB_RELAY_URL"] = os.environ.get("COLLAB_RELAY_URL") or MODE_URLS[mode]
        if not os.environ.get("COLLAB_RELAY_URL"):
            procs.append(subprocess.Popen([sys.executable, os.path.join(ROOT, "relay", "collab_relay.py"), str(PORT)]))
            if mode == "wss":
                procs.append(subprocess.Popen([sys.executable, os.path.join(ROOT, "tests", "tls_proxy.py"), str(PORT + 1), str(PORT)]))
                env["SSL_CERT_FILE"] = os.path.join(tempfile.gettempdir(), "collab_test_cert.pem")
            time.sleep(1.0)
    run_mode = "direct" if mode == "direct" else "relay"
    host = subprocess.Popen([sys.executable, __file__, run_mode, "host"], env=env)
    time.sleep(1.0)
    guest = subprocess.Popen([sys.executable, __file__, run_mode, "guest"], env=env)
    rc_h, rc_g = host.wait(900), guest.wait(900)
    for p in procs:
        p.terminate()
    print(f"\n=== GLB-REAL {mode.upper()}: host rc={rc_h} guest rc={rc_g} ===")
    sys.exit(0 if rc_h == 0 and rc_g == 0 else 1)


if __name__ == "__main__":
    main()
