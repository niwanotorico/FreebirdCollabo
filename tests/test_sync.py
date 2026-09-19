# SPDX-License-Identifier: GPL-2.0-or-later
"""
Headless two-instance sync test (no VR, no Freebird needed).

    python3 tests/test_sync.py relay     # relay mode: starts a relay, then HOST + GUEST processes
    python3 tests/test_sync.py direct    # direct mode: HOST embeds the hub

Each Blender instance is a separate `bpy` (pip wheel) process.  Checks the
MVP conditions that can be verified without a headset:
  1/2  create + join      3 scene shared on join      7 selection visible
  8    tool state visible 9 Cube transform sync both ways + new object sync
  10   host save
"""

import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 7799
# relay | ws | wss | direct   (wss uses a local TLS wrapper, see main())
MODE_URLS = {"relay": f"tcp://127.0.0.1:{PORT}", "ws": f"ws://127.0.0.1:{PORT}", "wss": f"wss://localhost:{PORT + 1}"}
RELAY_URL = os.environ.get("COLLAB_RELAY_URL", "")
STATE = os.path.join(tempfile.gettempdir(), "collab_test_state.json")


def _write_state(**kw):  # one file per key: no read-modify-write race between processes
    for k, v in kw.items():
        tmp = STATE + f".{k}.tmp"
        json.dump(v, open(tmp, "w"))
        os.replace(tmp, STATE + f".{k}")


def _read_state():
    import glob

    d = {}
    for f in glob.glob(STATE + ".*"):
        if f.endswith(".tmp"):
            continue
        try:
            d[f.rsplit(".", 1)[1]] = json.load(open(f))
        except Exception:
            pass
    return d


def _wait(cond, timeout, session=None, what=""):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if session:
            session.tick()
        if cond():
            return True
        time.sleep(0.02)
    raise AssertionError(f"timeout waiting for {what}")


# ----------------------------------------------------------------------
def run_host(mode):
    import bpy

    sys.path.insert(0, ROOT)
    from freebird_collab.session import CollabSession
    from mathutils import Vector

    s = CollabSession()
    blend = os.path.join(tempfile.gettempdir(), "collab_master.blend")
    bpy.data.objects["Cube"].location = (1.0, 2.0, 3.0)
    bpy.ops.wm.save_as_mainfile(filepath=blend)
    if mode == "direct":
        assert s.create_room("DIRECT", "Host", (1, 0.5, 0.2), direct_port=PORT)
    else:
        assert s.create_room("RELAY", "Host", (1, 0.5, 0.2), relay_url=RELAY_URL)
    _wait(lambda: s.uid is not None, 5, s, "welcome")
    print("HOST room:", s.room, s.status)
    _write_state(room=s.room)

    _wait(lambda: len(s.peers) == 1, 20, s, "guest join")
    guest = next(iter(s.peers.values()))
    print("HOST sees peer:", guest.name, guest.role)

    # 9: host moves Cube -> guest must see it
    bpy.data.objects["Cube"].location = Vector((5.0, 0.0, 0.0))
    # 7/8: host selects Cube and reports a fake Freebird tool
    bpy.data.objects["Cube"].select_set(True)
    _wait(lambda: _read_state().get("guest_saw_cube_at_5"), 20, s, "guest to see cube move")

    # guest moves Cube back + adds an object -> host must see both
    _wait(lambda: abs(bpy.data.objects["Cube"].location.x - (-2.0)) < 1e-3, 20, s, "guest cube move")
    _wait(lambda: "GuestSphere" in bpy.data.objects, 20, s, "guest new object")
    _wait(lambda: bpy.data.objects["GuestSphere"].location.z > 3.9, 20, s, "guest object transform")
    print("HOST: GuestSphere verts:", len(bpy.data.objects["GuestSphere"].data.vertices))

    # 7: presence from guest with selection + tool
    _wait(lambda: guest.presence and "GuestSphere" in guest.presence.get("sel", []), 10, s, "guest selection")
    print("HOST sees guest presence:", {k: guest.presence[k] for k in ("tool", "sel", "vr", "mode")})

    # 10: save master (triggered by guest's request)
    mtime0 = os.path.getmtime(blend)
    _wait(lambda: os.path.getmtime(blend) > mtime0, 20, s, "host save via guest request")
    print("HOST: master saved OK ->", blend)
    _write_state(host_done=True)
    for _ in range(30):
        s.tick()
        time.sleep(0.02)
    s.leave()
    print("HOST PASS")


def run_guest(mode):
    import bpy

    sys.path.insert(0, ROOT)
    from freebird_collab.session import CollabSession
    from mathutils import Vector

    s = CollabSession()
    _wait(lambda: _read_state().get("room"), 20, None, "room code")
    code = _read_state()["room"]
    # guest starts with a different scene: no Cube at all
    bpy.data.objects.remove(bpy.data.objects["Cube"])
    assert "Cube" not in bpy.data.objects
    if mode == "direct":
        assert s.join_room("DIRECT", "Guest", (0.2, 0.6, 1), direct_host="127.0.0.1", direct_port=PORT)
    else:
        assert s.join_room("RELAY", "Guest", (0.2, 0.6, 1), code=code, relay_url=RELAY_URL)
    _wait(lambda: s.uid is not None, 5, s, "welcome")
    print("GUEST:", s.status, "host_uid", s.host_uid)

    # 3: host scene arrives and replaces ours
    _wait(lambda: s._scene_loaded_from_host, 20, s, "scene from host")
    cube = bpy.data.objects.get("Cube")
    assert cube is not None, "Cube missing after scene load"
    print("GUEST: got host scene, Cube at", tuple(round(v, 2) for v in cube.location))

    # 9: see host's move
    _wait(lambda: abs(bpy.data.objects["Cube"].location.x - 5.0) < 1e-3, 20, s, "cube move from host")
    print("GUEST: saw Cube move to x=5")
    _write_state(guest_saw_cube_at_5=True)

    # move it back + add an object
    bpy.data.objects["Cube"].location = Vector((-2.0, 0.0, 0.0))
    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.5, location=(0, 0, 4))
    sph = bpy.context.active_object
    sph.name = "GuestSphere"
    sph.select_set(True)
    for _ in range(40):
        s.tick()
        time.sleep(0.02)

    # 7/8: host presence (selection = Cube)
    host = s.peers[s.host_uid]
    _wait(lambda: host.presence and "Cube" in host.presence.get("sel", []), 10, s, "host selection")
    print("GUEST sees host presence:", {k: host.presence[k] for k in ("tool", "sel", "vr", "mode")})

    # 10: ask host to save
    s.request_save()
    _wait(lambda: _read_state().get("host_done"), 30, s, "host done")
    s.leave()
    print("GUEST PASS")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "relay"
    role = sys.argv[2] if len(sys.argv) > 2 else None
    if role == "host":
        return run_host(mode)
    if role == "guest":
        return run_guest(mode)

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
    rc_h, rc_g = host.wait(120), guest.wait(120)
    for p in procs:
        p.terminate()
    print(f"\n=== {mode.upper()} MODE: host rc={rc_h} guest rc={rc_g} ===")
    sys.exit(0 if rc_h == 0 and rc_g == 0 else 1)


if __name__ == "__main__":
    main()
