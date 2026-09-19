// SPDX-License-Identifier: GPL-2.0-or-later
// Freebird Collab relay on Cloudflare Workers + Durable Objects (free plan, no card).
//
// Same wire protocol as freebird_collab/hub.py: JSON messages over WebSocket,
// "hello" first, then everything is forwarded to the other peers of the room
// (or only to msg.to). The hub stamps "from".  One Durable Object ("hub") holds
// every room; sockets hibernate between messages so idle rooms cost nothing.
//
// Limits to keep in mind: WebSocket messages <= 1 MiB (the add-on splits bigger
// ones into "chunk" messages, which are just forwarded), free plan 100k
// requests/day where 20 incoming WS messages = 1 request.

const ROOM_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";

// Peer colour: exactly 3 or 4 finite numbers clamped to 0..1, anything else -> white.
// Keeps the socket attachment small (serializeAttachment is capped at 2048 bytes).
function cleanColor(c) {
  if (!Array.isArray(c) || (c.length !== 3 && c.length !== 4)) return [1, 1, 1];
  const out = [];
  for (const v of c) {
    if (typeof v !== "number" || !Number.isFinite(v)) return [1, 1, 1];
    out.push(Math.round(Math.min(1, Math.max(0, v)) * 10000) / 10000);
  }
  return out;
}

function newRoomCode(n = 6) {
  const bytes = new Uint8Array(n);
  crypto.getRandomValues(bytes);
  let s = "";
  for (const b of bytes) s += ROOM_ALPHABET[b % ROOM_ALPHABET.length];
  return s;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.headers.get("Upgrade")?.toLowerCase() === "websocket") {
      const id = env.HUB.idFromName("hub");
      return env.HUB.get(id).fetch(request);
    }
    if (url.pathname === "/stats") {
      const id = env.HUB.idFromName("hub");
      return env.HUB.get(id).fetch(request);
    }
    return new Response("freebird-collab relay ok\n", {
      headers: { "content-type": "text/plain; charset=utf-8" },
    });
  },
};

export class Hub {
  constructor(ctx, env) {
    this.ctx = ctx;
    this.env = env;
  }

  async fetch(request) {
    const url = new URL(request.url);
    if (url.pathname === "/stats") {
      // Room codes are the only access control, so never expose them here: totals only.
      const sockets = this.ctx.getWebSockets();
      const rooms = new Set();
      for (const ws of sockets) {
        const a = ws.deserializeAttachment();
        if (a?.room) rooms.add(a.room);
      }
      return Response.json({ sockets: sockets.length, rooms: rooms.size });
    }
    if (request.headers.get("Upgrade")?.toLowerCase() !== "websocket") {
      return new Response("expected websocket", { status: 426 });
    }
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    this.ctx.acceptWebSocket(server); // hibernatable
    server.serializeAttachment({ uid: "u" + crypto.randomUUID().slice(0, 8), room: null });
    return new Response(null, { status: 101, webSocket: client });
  }

  // ---- helpers ------------------------------------------------------
  _peers(room, except) {
    const out = [];
    for (const ws of this.ctx.getWebSockets()) {
      if (ws === except) continue;
      const a = ws.deserializeAttachment();
      if (a?.room === room) out.push([ws, a]);
    }
    return out;
  }

  _send(ws, msg) {
    try {
      ws.send(JSON.stringify(msg));
    } catch (e) {
      /* socket already gone */
    }
  }

  // ---- hibernation API callbacks -----------------------------------
  async webSocketMessage(ws, data) {
    let msg;
    try {
      msg = JSON.parse(typeof data === "string" ? data : new TextDecoder().decode(data));
    } catch (e) {
      return this._send(ws, { t: "error", msg: "bad json" });
    }
    const me = ws.deserializeAttachment();
    const t = msg.t;

    if (t === "hello") return this._hello(ws, me, msg);
    if (t === "ping") return this._send(ws, { t: "pong" });
    if (!me.room) return this._send(ws, { t: "error", msg: "not in a room" });

    msg.from = me.uid;
    const to = msg.to;
    for (const [peer, a] of this._peers(me.room, ws)) {
      if (to && a.uid !== to) continue;
      this._send(peer, msg);
    }
  }

  _hello(ws, me, msg) {
    const name = String(msg.name || "user").slice(0, 32);
    const role = msg.role === "host" ? "host" : "guest";
    const color = cleanColor(msg.color);
    let code = String(msg.room || "").trim().toUpperCase();

    if (role === "host") {
      code = code || newRoomCode();
      if (this._peers(code, ws).some(([, a]) => a.role === "host")) {
        return this._send(ws, { t: "error", msg: `room ${code} already has a host` });
      }
    } else if (this._peers(code, ws).length === 0) {
      return this._send(ws, { t: "error", msg: `room ${code} not found` });
    }

    const attach = { uid: me.uid, name, role, color, room: code };
    ws.serializeAttachment(attach);

    const peers = this._peers(code, ws).map(([, a]) => ({ uid: a.uid, name: a.name, role: a.role, color: a.color }));
    const host = peers.find((p) => p.role === "host");
    this._send(ws, { t: "welcome", uid: me.uid, room: code, peers, host_uid: host ? host.uid : role === "host" ? me.uid : null, version: 1 });
    for (const [peer] of this._peers(code, ws)) {
      this._send(peer, { t: "peer_join", uid: me.uid, name, role, color });
    }
  }

  async webSocketClose(ws, code, reason, wasClean) {
    this._leave(ws);
    try {
      ws.close(1000, "bye");
    } catch (e) {}
  }

  async webSocketError(ws, err) {
    this._leave(ws);
  }

  _leave(ws) {
    const a = ws.deserializeAttachment();
    if (!a?.room) return;
    ws.serializeAttachment({ uid: a.uid, room: null });
    for (const [peer] of this._peers(a.room, ws)) {
      this._send(peer, { t: "peer_leave", uid: a.uid });
    }
  }
}
