# SPDX-License-Identifier: GPL-2.0-or-later
"""
"Update available" notice: asks GitHub Releases once (on add-on enable) whether a newer version exists.

Notify only. Nothing is downloaded or installed here; the actual in-app update lives in updater.py and starts
only when the user presses the button. This module also picks the official asset (freebird_collab_addon.zip) out of the release.
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
ASSET_NAME = "freebird_collab_addon.zip"
TIMEOUT = 5.0

_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?$")

# set by the background thread, read by the panel
latest_version = None  # (0, 13, 0) when a newer release exists, else None
latest_tag = ""
latest_url = RELEASES_PAGE
latest_asset = None  # {"url", "size", "sha256"} of the official zip, or None (then the panel only opens the Release page)
done = False


def parse_version(tag):
    """'v0.12.0' / '0.13' -> (0, 12, 0) / (0, 13, 0). None for anything else (vr-studio-v0.3.0, v1.0-beta...)."""
    m = _TAG_RE.match(str(tag or "").strip())
    if not m:
        return None
    return tuple(int(g or 0) for g in m.groups())


def is_newer(remote, current):
    return remote is not None and tuple(remote) > tuple(current)


def find_asset(release):
    """The official zip of a release: exactly one asset named freebird_collab_addon.zip, fully uploaded. Else None."""
    hits = [
        a for a in (release.get("assets") or [])
        if isinstance(a, dict) and a.get("name") == ASSET_NAME and a.get("state", "uploaded") == "uploaded"
    ]
    if len(hits) != 1:
        return None
    url = str(hits[0].get("browser_download_url") or "")
    if not url.startswith("https://"):
        return None
    digest = str(hits[0].get("digest") or "")
    sha = digest[len("sha256:"):].lower() if digest.startswith("sha256:") else ""
    size = hits[0].get("size")
    return {"url": url, "size": size if isinstance(size, int) else 0, "sha256": sha}


def _newest(releases):
    """(version, release dict) of the highest add-on release in a GitHub /releases list, or None."""
    if isinstance(releases, dict):  # a single release object (/releases/latest or a debug file)
        releases = [releases]
    best = None
    for r in releases:
        if not isinstance(r, dict) or r.get("draft"):
            continue
        v = parse_version(r.get("tag_name"))
        if v and (best is None or v > best[0]):
            best = (v, r)
    return best


def newest_release(releases):
    """(version, tag, html_url) of the highest add-on release in a GitHub /releases list, or None."""
    best = _newest(releases)
    if best is None:
        return None
    return (best[0], best[1]["tag_name"].strip(), str(best[1].get("html_url") or ""))


def resolve_latest(current, url=None, fetch=None):
    """Blocking, raises on network errors. Fresh look at the newest release when it is newer than `current`:
    {"version", "tag", "url", "asset"} or None. The updater calls this again on click instead of trusting old state."""
    best = _newest((fetch or _fetch)(url or os.environ.get("FREEBIRD_COLLAB_UPDATE_URL") or RELEASES_API))
    if best is None or not is_newer(best[0], current):
        return None
    page = str(best[1].get("html_url") or "")
    return {
        "version": best[0],
        "tag": best[1]["tag_name"].strip(),
        "url": page if page.startswith("https://") else RELEASES_PAGE,
        "asset": find_asset(best[1]),
    }


def _fetch(url):
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "FreebirdCollabo"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def check(current, url=None, fetch=_fetch):
    """Blocking check. Updates the module state; never raises."""
    global latest_version, latest_tag, latest_url, latest_asset, done
    try:
        found = resolve_latest(current, url, fetch)
        if found:
            latest_version, latest_tag, latest_url, latest_asset = found["version"], found["tag"], found["url"], found["asset"]
        else:
            latest_version, latest_tag, latest_url, latest_asset = None, "", RELEASES_PAGE, None
    except Exception:
        pass  # offline / blocked / rate-limited: just no notice
    done = True


def start(current):
    """Non-blocking check on a daemon thread; poll `done` from the main thread."""
    global done
    done = False
    threading.Thread(target=check, args=(current,), name="collab-update-check", daemon=True).start()
