#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""
Relay load model: how many messages one room pushes through the relay, before / after the
traffic reduction (docs/relay-load-analysis.md). Plain Python 3, no Blender, no network.

    python3 tools/relay_load_sim.py              # markdown table, 60 s per scenario
    python3 tools/relay_load_sim.py --seconds 300 --json

Every client runs the same send schedule as CollabSession.tick() (60 Hz timer; xform 30 Hz only for
changed objects; presence sampled at 20 Hz; ping every 20 s). "old" = v0.12.1 behaviour, "new" = the
real gate / batching code from freebird_collab/traffic.py. Counted per scenario:

    in      client -> relay messages (each one is one webSocketMessage() call of the Durable Object,
            except "new" pings, which the runtime answers itself: auto-response)
    do      Durable Object invocations = "in" minus auto-answered pings
    req     billed requests = do / 20 (Cloudflare: 20 incoming WebSocket messages = 1 request)
    out     relay -> client deliveries (free of request charge, shown for bandwidth)

Scenario motion is a model (see VR_* constants), not a recording: real VR numbers depend on how
much people move. Desktop numbers are exact for the schedule.
"""

import argparse
import importlib.util
import json
import math
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("fb_traffic", os.path.join(ROOT, "freebird_collab", "traffic.py"))
traffic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(traffic)

TICK = 1.0 / 60.0  # freebird_collab/__init__.py TICK_INTERVAL
XFORM_HZ = 30.0
PRESENCE_HZ = 20.0
PING_SECONDS = 20.0
FREE_REQUESTS_PER_DAY = 100_000
WS_MSGS_PER_REQUEST = 20
# VR motion model: standing user sways a few cm; an active user walks / reaches.
VR_IDLE_SWAY_M = 0.01  # amplitude (m) of slow body sway, ~0.25 Hz
VR_ACTIVE_SPEED = 0.35  # m/s head / hand movement while working
VR_NOISE_M = 0.0003  # tracking jitter


def _r4(v):
    return round(v, 4)


class Client:
    def __init__(self, idx, policy, vr, dragging, rng):
        self.idx = idx
        self.policy = policy  # "old" | "new"
        self.vr = vr  # None | "idle" | "active"
        self.dragging = dragging
        self.rng = rng
        self.gate = traffic.PresenceThrottle()
        self.can_batch = False
        self.last_presence_t = -1.0
        self.last_xform_t = -1.0
        self.last_ping_t = 0.0
        self.phase = rng.random() * 10

    def _pose(self, t, base):
        if self.vr == "active":
            x = base[0] + VR_ACTIVE_SPEED * 0.6 * math.sin(t * 0.9 + self.phase)
            y = base[1] + VR_ACTIVE_SPEED * 0.6 * math.cos(t * 0.7 + self.phase)
            ang = 0.6 * math.sin(t * 0.5 + self.phase)
        else:
            x = base[0] + VR_IDLE_SWAY_M * math.sin(t * 1.6 + self.phase)
            y = base[1] + VR_IDLE_SWAY_M * math.cos(t * 1.3 + self.phase)
            ang = 0.02 * math.sin(t * 0.8 + self.phase)
        n = lambda: self.rng.gauss(0, VR_NOISE_M)  # noqa: E731
        return [_r4(x + n()), _r4(y + n()), _r4(base[2] + n()), _r4(math.cos(ang / 2)), 0.0, 0.0, _r4(math.sin(ang / 2))]

    def presence(self, t):
        if self.vr:
            head = self._pose(t, (0, 0, 1.6))
            hands = {"L": self._pose(t + 0.3, (-0.2, 0.3, 1.2)), "R": self._pose(t + 0.7, (0.2, 0.3, 1.2))}
        else:
            head, hands = None, None
        return {"vr": bool(self.vr), "head": head, "hands": hands, "tool": "select_box", "sel": ["Cube"],
                "mode": "OBJECT", "name": f"user{self.idx}", "color": [0.2, 0.6, 1.0]}

    def tick(self, t, n_peers):
        out = []
        if self.dragging and t - self.last_xform_t >= 1.0 / XFORM_HZ:
            self.last_xform_t = t
            m = [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, _r4(math.sin(t)), _r4(math.cos(t)), 0, 1.0]
            out.append({"t": "xform", "objs": {"Cube": m}})
        if t - self.last_presence_t >= 1.0 / PRESENCE_HZ:
            self.last_presence_t = t
            pres = self.presence(t)
            if self.policy == "old" or self.gate.should_send(t, pres, n_peers > 0):
                out.append(dict(pres, t="presence"))
        if t - self.last_ping_t >= PING_SECONDS:
            self.last_ping_t = t
            out.append({"t": "ping"})
        if self.policy == "new" and self.can_batch:
            out = traffic.batch_groups(out)
        return out


def run(scenario, seconds, seed=1):
    rng = random.Random(seed)
    clients = [Client(i, c["policy"], c.get("vr"), c.get("dragging", False), rng) for i, c in enumerate(scenario["clients"])]
    relay_new = scenario.get("relay", "new") == "new"
    for c in clients:
        c.can_batch = relay_new  # welcome.batch is only sent by the new relay
    n = len(clients)
    cnt = {"in": 0, "do": 0, "out": 0, "by_type": {}}
    t = 0.0
    while t < seconds:
        for c in clients:
            jitter = rng.uniform(0, 0.002)  # Blender timers never fire exactly on time
            for m in c.tick(t + jitter, n - 1):
                cnt["in"] += 1
                typ = m["t"]
                cnt["by_type"][typ] = cnt["by_type"].get(typ, 0) + 1
                if typ == "ping":
                    if not relay_new:
                        cnt["do"] += 1  # old relay: handled in webSocketMessage()
                    continue  # pong goes back to the sender only
                cnt["do"] += 1
                inner = len(m["m"]) if typ == "batch" else 1
                cnt["out"] += inner * (n - 1)
        t += TICK
    per_min = 60.0 / seconds
    res = {k: round(v * per_min, 1) for k, v in cnt.items() if k != "by_type"}
    res["by_type"] = {k: round(v * per_min, 1) for k, v in sorted(cnt["by_type"].items())}
    res["req"] = round(res["do"] / WS_MSGS_PER_REQUEST, 1)
    res["hours_to_free_limit"] = round(FREE_REQUESTS_PER_DAY / (res["req"] * 60), 1) if res["req"] else float("inf")
    return res


def scenarios():
    out = []
    for n in (1, 2, 3, 4):
        out.append((f"{n}人・デスクトップ放置", [{}] * n))
        if n > 1:
            out.append((f"{n}人・デスクトップ操作中（1人がドラッグ）", [{"dragging": True}] + [{}] * (n - 1)))
            out.append((f"{n}人・VR放置（全員VRで立っているだけ）", [{"vr": "idle"}] * n))
            out.append((f"{n}人・VR操作中（1人がVRで動きながらドラッグ）", [{"vr": "active", "dragging": True}] + [{"vr": "idle"}] * (n - 1)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    rows = []
    for name, cl in scenarios():
        old = run({"clients": [dict(c, policy="old") for c in cl], "relay": "old"}, a.seconds)
        new = run({"clients": [dict(c, policy="new") for c in cl], "relay": "new"}, a.seconds)
        rows.append({"scenario": name, "old": old, "new": new})
    # mixed room: one v0.12.1 client among new ones, on the new relay
    for n in (2, 3):
        cl = [{"policy": "old"}] + [{"policy": "new"}] * (n - 1)
        rows.append({"scenario": f"{n}人・デスクトップ放置・v0.12.1が1人混在（新リレー）",
                     "old": run({"clients": [dict(c, policy="old") for c in cl], "relay": "old"}, a.seconds),
                     "new": run({"clients": cl, "relay": "new"}, a.seconds)})
    # new add-ons on a relay that was not redeployed yet (no batch, no auto-response)
    cl = [{"vr": "active", "dragging": True}, {"vr": "idle"}]
    rows.append({"scenario": "2人・VR操作中・新アドオン＋旧リレー（未デプロイ）",
                 "old": run({"clients": [dict(c, policy="old") for c in cl], "relay": "old"}, a.seconds),
                 "new": run({"clients": [dict(c, policy="new") for c in cl], "relay": "old"}, a.seconds)})
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return
    print(f"(1 分あたり。{a.seconds:.0f} 秒シミュレーションの換算。req = DO 課金リクエスト、上限 = 無料枠 10 万 req/日 を使い切るまでの連続時間)\n")
    print("| シナリオ | 変更前 受信msg/分 | 変更後 受信msg/分 | 変更前 req/分 | 変更後 req/分 | 削減率 | 変更前 上限まで | 変更後 上限まで |")
    print("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for r in rows:
        o, nw = r["old"], r["new"]
        red = (1 - nw["req"] / o["req"]) * 100 if o["req"] else 0
        fmt_h = lambda h: "∞" if h == float("inf") else f"{h:,.1f} h"  # noqa: E731
        print(f"| {r['scenario']} | {o['in']:,.0f} | {nw['in']:,.0f} | {o['req']:,.1f} | {nw['req']:,.1f} | {red:.0f}% | {fmt_h(o['hours_to_free_limit'])} | {fmt_h(nw['hours_to_free_limit'])} |")


if __name__ == "__main__":
    sys.exit(main())
