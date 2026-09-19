#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""
Freebird Collab relay server.  Pure stdlib, no dependencies.
Speaks raw TCP *and* WebSocket on the same port (auto-detected), and answers
plain HTTP GET with 200 so PaaS health checks pass.

    python3 collab_relay.py            # port 7788 (or $PORT if set, e.g. 7860 on HF Spaces)
    python3 collab_relay.py 9000       # custom port

Users only ever type the 6-letter room code; the relay URL lives in the
add-on preferences and is set once.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _netlib import hub  # noqa: E402  (no bpy needed)

serve_forever = hub.serve_forever

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", "7788"))
    print(f"Freebird Collab relay on port {port} (tcp + websocket)  Ctrl+C to stop", flush=True)
    serve_forever("0.0.0.0", port)
