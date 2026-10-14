#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""
Measured relay load: real add-on sessions (one Blender `bpy` process per user) against a counting relay.
Compares the v0.12.1 add-on (taken from git) with the current checkout, same scenario, same relay.

    python3 tools/relay_load_measure.py                         # needs `pip install bpy==5.0.1`
    python3 tools/relay_load_measure.py --users 2,4 --seconds 30 --scenarios idle,drag,vr

Scenarios (measured after everyone joined and the host's scene was loaded):
    idle   desktop, nobody touches anything
    drag   desktop, user 0 moves the Cube on every tick (others idle)
    vr     every user "in VR" (bpy has no headset: sample_xr returns a model pose, see relay_load_sim.py),
           user 0 walks around and drags the Cube, the others stand still

The relay is the current freebird_collab/hub.py with a counter on each incoming WebSocket message
(= one webSocketMessage() call of the Cloudflare Durable Object). "ping" is listed separately:
the Cloudflare relay of this change answers it with an auto-response, so it is no DO call there.
"""

import argparse
import io
import json
import math
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEGACY_REF = "da4b3fd"  # v0.12.1


def legacy_root():
    out = os.path.join(tempfile.gettempdir(), f"collab_legacy_{LEGACY_REF}")
    if not os.path.isdir(os.path.join(out, "freebird_collab")):
        data = subprocess.check_output(["git", "-C", ROOT, "archive", LEGACY_REF, "freebird_collab", "relay"])
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            tf.extractall(out)
    return out


# ----------------------------------------------------------------------
# one user (child process)
# ----------------------------------------------------------------------
def _state(path, key, value=None):
    f = f"{path}.{key}"
    if value is None:
        try:
            with open(f) as fh:
                return json.load(fh)
        except Exception:
            return None
    with open(f + ".tmp", "w") as fh:
        json.dump(value, fh)
    os.replace(f + ".tmp", f)


def user(idx, n, url, root, scenario, state):
    sys.path.insert(0, root)
    import bpy
    import freebird_collab.session as sm

    if scenario == "vr":
        active = idx == 0
        phase = idx * 1.7

        def pose(t, base):
            if active:
                x, y, ang = base[0] + 0.2 * math.sin(t * 0.9 + phase), base[1] + 0.2 * math.cos(t * 0.7 + phase), 0.6 * math.sin(t * 0.5)
            else:
                x, y, ang = base[0] + 0.01 * math.sin(t * 1.6 + phase), base[1] + 0.01 * math.cos(t * 1.3 + phase), 0.02 * math.sin(t * 0.8)
            return sm._pose((x, y, base[2]), (math.cos(ang / 2), 0.0, 0.0, math.sin(ang / 2)))

        def fake_xr():
            t = time.time()
            return pose(t, (0, 0, 1.6)), {"L": pose(t + 0.3, (-0.2, 0.3, 1.2)), "R": pose(t + 0.7, (0.2, 0.3, 1.2))}

        sm.sample_xr = fake_xr
    s = sm.CollabSession()
    if idx == 0:
        assert s.create_room("RELAY", "U0", (1, 0.5, 0.2), relay_url=url)
        while s.uid is None:
            s.tick()
            time.sleep(0.02)
        _state(state, "room", s.room)
    else:
        while _state(state, "room") is None:
            time.sleep(0.05)
        assert s.join_room("RELAY", f"U{idx}", (0.2, 1, 0.2), code=_state(state, "room"), relay_url=url)
    t0 = time.time()
    while not (len(s.peers) == n - 1 and (idx == 0 or s._scene_loaded_from_host)):
        s.tick()
        time.sleep(1 / 60)
        if time.time() - t0 > 90:
            raise SystemExit(f"U{idx}: room never filled")
    _state(state, f"ready{idx}", True)
    cube = bpy.data.objects["Cube"]
    while not _state(state, "stop"):
        if idx == 0 and scenario in ("drag", "vr"):
            cube.location.x = round(math.sin(time.time()), 4)
        s.tick()
        time.sleep(1 / 60)  # TICK_INTERVAL of the add-on timer
    s.leave()
    sys.stdout.flush()
    os._exit(0)


# ----------------------------------------------------------------------
# coordinator
# ----------------------------------------------------------------------
def counting_hub():
    sys.path.insert(0, os.path.join(ROOT, "relay"))
    from _netlib import hub as hubmod

    h = hubmod.Hub("127.0.0.1", 0, log=lambda *a: None)
    orig = h._handle
    tls = threading.local()
    h.counts = {}
    h.lock = threading.Lock()

    def counted(c, msg):
        if getattr(tls, "inner", False):  # message inside a batch: same webSocketMessage() call
            return orig(c, msg)
        t = msg.get("t")
        with h.lock:
            h.counts[t] = h.counts.get(t, 0) + 1
            if t == "batch":
                h.counts["_inner"] = h.counts.get("_inner", 0) + len(msg.get("m") or ())
        tls.inner = True
        try:
            return orig(c, msg)
        finally:
            tls.inner = False

    h._handle = counted
    h.start()
    return h


def measure(version, n, scenario, seconds, py):
    root = ROOT if version == "new" else legacy_root()
    hub = counting_hub()
    url = f"ws://127.0.0.1:{hub.port}"
    state = os.path.join(tempfile.mkdtemp(prefix="collab_load_"), "s")
    procs = []
    for i in range(n):
        tmp = tempfile.mkdtemp(prefix=f"collab_load_u{i}_")  # own temp dir: guests write <tmp>/collab_host_<file>.blend
        procs.append(subprocess.Popen([py, os.path.abspath(__file__), "_user", str(i), str(n), url, root, scenario, state],
                                      env=dict(os.environ, TMPDIR=tmp), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        if i == 0:
            time.sleep(1.0)
    t0 = time.time()
    while not all(_state(state, f"ready{i}") for i in range(n)):
        if time.time() - t0 > 120 or any(p.poll() is not None for p in procs):
            for p in procs:
                p.kill()
            hub.stop()
            raise RuntimeError(f"{version} {n} {scenario}: setup failed")
        time.sleep(0.1)
    time.sleep(2.0)  # let the join burst (scene, ver, first presence) pass
    with hub.lock:
        hub.counts.clear()
    t0 = time.time()
    time.sleep(seconds)
    with hub.lock:
        counts = dict(hub.counts)
    dt = time.time() - t0
    _state(state, "stop", True)
    for p in procs:
        try:
            p.wait(30)
        except subprocess.TimeoutExpired:
            p.kill()
    hub.stop()
    per_min = {k: round(v * 60 / dt, 1) for k, v in sorted(counts.items())}
    total = sum(v for k, v in per_min.items() if not k.startswith("_"))
    ping = per_min.get("ping", 0.0)
    return {"version": version, "users": n, "scenario": scenario, "per_min": per_min, "in": round(total, 1),
            "do_calls": round(total - (ping if version == "new" else 0), 1)}


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "_user":
        a = sys.argv[2:]
        return user(int(a[0]), int(a[1]), a[2], a[3], a[4], a[5])
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", default="2,3,4")
    ap.add_argument("--scenarios", default="idle,drag,vr")
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--python", default=sys.executable, help="Python with the bpy module")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    rows = []
    for scenario in a.scenarios.split(","):
        for n in (int(x) for x in a.users.split(",")):
            old = measure("old", n, scenario, a.seconds, a.python)
            new = measure("new", n, scenario, a.seconds, a.python)
            rows.append((old, new))
            print(f"# {scenario} {n}: old {old['per_min']}  new {new['per_min']}", file=sys.stderr, flush=True)
    if a.json:
        print(json.dumps(rows, indent=1))
        return
    names = {"idle": "デスクトップ放置", "drag": "デスクトップ操作中（1人がドラッグ）", "vr": "VR（1人が動きながらドラッグ、他は立っているだけ）"}
    print(f"(実測。各 {a.seconds:.0f} 秒を 1 分あたりに換算。DO 呼び出し = リレーが処理した受信メッセージ数、"
          "新版の ping は Cloudflare では自動応答なので除外。req = DO 呼び出し / 20)\n")
    print("| シナリオ | 人数 | 変更前 DO呼び出し/分 | 変更後 DO呼び出し/分 | 変更前 req/分 | 変更後 req/分 | 削減率 | 変更後の内訳/分 |")
    print("| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |")
    for old, new in rows:
        red = (1 - new["do_calls"] / old["do_calls"]) * 100 if old["do_calls"] else 0
        detail = ", ".join(f"{k} {v:g}" for k, v in new["per_min"].items() if not k.startswith("_"))
        inner = new["per_min"].get("_inner")
        if inner:
            detail += f"（batch の中身 {inner:g}）"
        print(f"| {names.get(old['scenario'], old['scenario'])} | {old['users']} | {old['do_calls']:,.0f} | {new['do_calls']:,.0f} | "
              f"{old['do_calls'] / 20:,.1f} | {new['do_calls'] / 20:,.1f} | {red:.0f}% | {detail} |")


if __name__ == "__main__":
    main()
