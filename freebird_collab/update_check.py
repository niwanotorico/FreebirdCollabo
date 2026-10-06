# SPDX-License-Identifier: GPL-2.0-or-later
"""
"Update available" notice: asks GitHub Releases once (on add-on enable) whether a newer version exists.

Notify only. Nothing is downloaded or installed; the button in the COLLAB panel just opens the Release page.
Runs on a background thread with a short timeout, and every failure (offline, rate limit, bad JSON) is
silently ignored, so the collab session / relay / sync code is never affected.

Which releases count: tags shaped exactly like v0.13.0 / 0.13 (other tags in this repo such as
vr-studio-v0.3.0 are ignored), drafts skipped, pre-releases included (the add-on is published as one).

Debug: set FREEBIRD_COLLAB_UPDATE_URL to another JSON source (e.g. file:///C:/tmp/fake_release.json with
[{"tag_name": "v9.9.9", "html_url": "https://..."}]) before starting Blender to see the notice without a real release.
"""

import json
import os
import re
import threading
import urllib.request

RELEASES_API = "https://api.github.com/repos/niwanotorico/FreebirdCollabo/releases?per_page=30"
RELEASES_PAGE = "https://github.com/niwanotorico/FreebirdCollabo/releases"
TIMEOUT = 5.0

_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?$")

# set by the background thread, read by the panel
latest_version = None  # (0, 13, 0) when a newer release exists, else None
latest_tag = ""
latest_url = RELEASES_PAGE
done = False


def parse_version(tag):
    """'v0.12.0' / '0.13' -> (0, 12, 0) / (0, 13, 0). None for anything else (vr-studio-v0.3.0, v1.0-beta...)."""
    m = _TAG_RE.match(str(tag or "").strip())
    if not m:
        return None
    return tuple(int(g or 0) for g in m.groups())


def is_newer(remote, current):
    return remote is not None and tuple(remote) > tuple(current)


def newest_release(releases):
    """(version, tag, html_url) of the highest add-on release in a GitHub /releases list, or None."""
    if isinstance(releases, dict):  # a single release object (/releases/latest or a debug file)
        releases = [releases]
    best = None
    for r in releases:
        if not isinstance(r, dict) or r.get("draft"):
            continue
        v = parse_version(r.get("tag_name"))
        if v and (best is None or v > best[0]):
            best = (v, r["tag_name"].strip(), str(r.get("html_url") or ""))
    return best


def _fetch(url):
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "FreebirdCollabo"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def check(current, url=None, fetch=_fetch):
    """Blocking check. Updates the module state; never raises."""
    global latest_version, latest_tag, latest_url, done
    try:
        best = newest_release(fetch(url or os.environ.get("FREEBIRD_COLLAB_UPDATE_URL") or RELEASES_API))
        if best and is_newer(best[0], current):
            latest_version, latest_tag = best[0], best[1]
            latest_url = best[2] if best[2].startswith("https://") else RELEASES_PAGE
        else:
            latest_version, latest_tag, latest_url = None, "", RELEASES_PAGE
    except Exception:
        pass  # offline / blocked / rate-limited: just no notice
    done = True


def start(current):
    """Non-blocking check on a daemon thread; poll `done` from the main thread."""
    global done
    done = False
    threading.Thread(target=check, args=(current,), name="collab-update-check", daemon=True).start()
