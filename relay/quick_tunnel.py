#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""
One-shot "relay on this PC + public URL" for testing with a friend, no account needed.

    python quick_tunnel.py               (or double-click quick_tunnel.bat on Windows)

Starts the Python relay on localhost:7788 and a Cloudflare *quick tunnel*
(cloudflared, no login) in front of it, then prints the URL to paste into the
add-on preferences as   Relay URL:  wss://xxxx-xxxx.trycloudflare.com
The URL changes every time this script starts. Keep the window open while testing.

cloudflared: put cloudflared.exe (Windows) / cloudflared (mac/linux) next to this
script or on PATH.  https://github.com/cloudflare/cloudflared/releases
"""

import os
import re
import shutil
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
PORT = int(os.environ.get("PORT", "7788"))


def find_cloudflared():
    names = ["cloudflared.exe", "cloudflared-windows-amd64.exe", "cloudflared"]
    for n in names:
        p = os.path.join(HERE, n)
        if os.path.isfile(p):
            return p
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


def main():
    from _netlib import hub as hubmod  # plain Python, no bpy

    Hub = hubmod.Hub

    cf = find_cloudflared()
    if not cf:
        print("cloudflared not found. Download it from https://github.com/cloudflare/cloudflared/releases")
        print(f"and put it in: {HERE}")
        input("Press Enter to exit")
        return 1

    hub = Hub("127.0.0.1", PORT, log=lambda m: print(m, flush=True))
    hub.start()

    print(f"starting cloudflared quick tunnel -> http://localhost:{PORT} ...", flush=True)
    proc = subprocess.Popen(
        [cf, "tunnel", "--url", f"http://localhost:{PORT}", "--no-autoupdate"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    url = None
    t0 = time.time()
    for line in proc.stdout:
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
        if m:
            url = m.group(0)
            break
        if time.time() - t0 > 60:
            break
    if not url:
        print("could not get a tunnel URL (network blocked?). cloudflared output above.")
        proc.terminate()
        return 1

    wss = url.replace("https://", "wss://")
    banner = f"""
==========================================================
  Relay URL (paste into Blender > Preferences > COLLAB):

      {wss}

  Send this URL to your friend too. Then:
    HOST : Create Room -> send the 6-letter code
    JOIN : type the code -> Join Room
  Keep this window open. Ctrl+C to stop.
==========================================================
"""
    print(banner, flush=True)
    try:
        if sys.platform == "win32":
            subprocess.run("clip", input=wss, text=True, check=False)
            print("(copied to clipboard)", flush=True)
    except Exception:
        pass

    def _drain():
        for _ in proc.stdout:
            pass

    threading.Thread(target=_drain, daemon=True).start()
    try:
        while proc.poll() is None:
            time.sleep(1)
        print("cloudflared exited.")
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
        hub.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
