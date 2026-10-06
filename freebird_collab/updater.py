# SPDX-License-Identifier: GPL-2.0-or-later
"""
In-app update: replaces this add-on's folder with the newest GitHub Release, only when the user presses the button.

Flow (everything on a background thread, no bpy here):
  1. look at the newest release again and pick the asset named exactly freebird_collab_addon.zip
  2. download it to a temp folder next to the add-on (same drive, so the swap is a rename)
  3. verify: size, SHA-256 (when GitHub gives one), zip-slip / symlink / entry checks, __init__.py present,
     every .py compiles, bl_info version == the release version
  4. swap:  freebird_collab -> .freebird_collab.old,  new -> freebird_collab.  Any failure puts the old folder back.
  5. tell the user to restart Blender.

No hot reload and no automatic restart: the running (old) code stays in memory until Blender is restarted.
The collab session / relay / undo code is never touched or imported here.
"""

import ast
import hashlib
import os
import shutil
import stat
import threading
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from . import update_check

PKG_NAME = "freebird_collab"
DOWNLOAD_PREFIX = "https://github.com/niwanotorico/FreebirdCollabo/releases/download/"
REDIRECT_HOSTS = ("github.com", "githubusercontent.com")  # host itself or a subdomain (objects., release-assets.)
MAX_ZIP_BYTES = 10 * 1024 * 1024
MAX_UNPACKED_BYTES = 30 * 1024 * 1024
MAX_ENTRIES = 300
TIMEOUT = 20.0
TMP_NAME = ".freebird_collab_update_tmp"
OLD_NAME = ".freebird_collab.old"

# shown by the panel, written by the update thread
state = "idle"  # idle | running | done | error
message = ""
new_tag = ""

_rename = os.replace  # replaced in tests to inject failures


class UpdateError(Exception):
    pass


# ----------------------------------------------------------------------
# where / whether we may update
# ----------------------------------------------------------------------
def package_dir():
    return Path(__file__).resolve().parent


def blocked_reason(pkg=None):
    """Why this install must not be updated in place (None = OK). Dev checkouts and links are never touched."""
    raw = Path(pkg) if pkg else Path(__file__).parent
    pkg = raw.resolve()
    if raw.is_symlink() or _is_junction(raw):
        return "installed as a link (development setup)"
    for parent in (pkg.parent, pkg.parent.parent):
        if (parent / ".git").exists():
            return "inside a git checkout (development setup)"
    if not os.access(pkg.parent, os.W_OK) or not os.access(pkg, os.W_OK):
        return "no write permission to the add-on folder"
    return None


def _is_junction(p):
    try:
        return bool(os.path.isjunction(p))  # Python 3.12+ (Blender 4.2+ ships 3.11 -> falls through)
    except AttributeError:
        try:
            return bool(os.lstat(p).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        except (AttributeError, OSError):
            return False


# ----------------------------------------------------------------------
# download
# ----------------------------------------------------------------------
def _host_ok(url):
    u = urllib.parse.urlsplit(url)
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host == h or host.endswith("." + h) for h in REDIRECT_HOSTS)


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _host_ok(newurl):
            raise UpdateError(f"redirect to an untrusted host: {urllib.parse.urlsplit(newurl).hostname}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url):
    opener = urllib.request.build_opener(_SafeRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": "FreebirdCollabo"})
    return opener.open(req, timeout=TIMEOUT)


def download(url, dest, expected_size=0, sha256="", opener=None):
    """Stream url -> dest (a .part file renamed at the end). Enforces URL prefix, size cap and SHA-256."""
    if not url.startswith(DOWNLOAD_PREFIX):
        raise UpdateError("download URL is not the official GitHub Release location")
    h = hashlib.sha256()
    total = 0
    part = Path(str(dest) + ".part")
    with (opener or _open)(url) as r, open(part, "wb") as f:
        while True:
            chunk = r.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ZIP_BYTES:
                raise UpdateError("download is larger than expected")
            h.update(chunk)
            f.write(chunk)
    if expected_size and total != expected_size:
        raise UpdateError(f"size mismatch ({total} != {expected_size})")
    if sha256 and h.hexdigest() != sha256.lower():
        raise UpdateError("SHA-256 mismatch")
    _rename(part, dest)


# ----------------------------------------------------------------------
# verify + extract
# ----------------------------------------------------------------------
def _bl_info_version(source):
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "bl_info" for t in node.targets):
            info = ast.literal_eval(node.value)
            return tuple(info["version"])
    raise UpdateError("bl_info not found in the downloaded add-on")


def safe_entries(zf):
    """Validated [(ZipInfo, relative path inside freebird_collab/)]. Raises UpdateError for anything suspicious."""
    infos = zf.infolist()
    if not infos or len(infos) > MAX_ENTRIES:
        raise UpdateError("unexpected number of files in the zip")
    if sum(i.file_size for i in infos) > MAX_UNPACKED_BYTES:
        raise UpdateError("zip unpacks to an unexpected size")
    out = []
    for i in infos:
        name = i.filename.replace("\\", "/")
        parts = name.split("/")
        if name.startswith("/") or (len(parts[0]) >= 2 and parts[0][1] == ":") or ".." in parts or "\x00" in name:
            raise UpdateError(f"unsafe path in zip: {i.filename}")
        if parts[0] != PKG_NAME:
            raise UpdateError(f"file outside {PKG_NAME}/ in zip: {i.filename}")
        if stat.S_ISLNK(i.external_attr >> 16):
            raise UpdateError(f"symbolic link in zip: {i.filename}")
        if i.flag_bits & 0x1:
            raise UpdateError("encrypted zip")
        rel = "/".join(p for p in parts[1:] if p)
        if rel:
            out.append((i, rel, name.endswith("/")))
    if not any(rel == "__init__.py" for _, rel, _d in out):
        raise UpdateError(f"{PKG_NAME}/__init__.py missing in zip")
    return out


def extract_verified(zip_path, dest_pkg, expected_version):
    """Extract into dest_pkg (must not exist) and verify. Raises UpdateError; the caller removes the temp tree."""
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as e:
        raise UpdateError(f"not a valid zip: {e}")
    with zf:
        if zf.testzip() is not None:
            raise UpdateError("zip is corrupted")
        entries = safe_entries(zf)
        root = Path(dest_pkg).resolve()
        root.mkdir(parents=True)
        for info, rel, is_dir in entries:
            target = (root / rel).resolve()
            if root != target and root not in target.parents:
                raise UpdateError(f"unsafe path in zip: {info.filename}")
            if is_dir:
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
    for py in root.rglob("*.py"):
        try:
            compile(py.read_bytes(), str(py), "exec")
        except (SyntaxError, ValueError) as e:
            raise UpdateError(f"{py.name} does not compile: {e}")
    found = _bl_info_version((root / "__init__.py").read_text(encoding="utf-8"))
    if found != tuple(expected_version):
        raise UpdateError(f"version in zip {found} != release {tuple(expected_version)}")
    return found


# ----------------------------------------------------------------------
# swap with rollback
# ----------------------------------------------------------------------
def swap(pkg, new_pkg, expected_version):
    """pkg -> .old, new_pkg -> pkg. On any failure the old folder is put back. Returns nothing; raises UpdateError."""
    pkg, new_pkg = Path(pkg), Path(new_pkg)
    old = pkg.parent / OLD_NAME
    if old.exists():
        shutil.rmtree(old)
    try:
        _rename(pkg, old)
    except OSError as e:
        raise UpdateError(f"could not move the current add-on aside (files in use?): {e}")
    try:
        _rename(new_pkg, pkg)
        if _bl_info_version((pkg / "__init__.py").read_text(encoding="utf-8")) != tuple(expected_version):
            raise UpdateError("installed version check failed")
    except Exception as e:
        # roll back: drop whatever landed at pkg, restore the old folder
        try:
            if pkg.exists():
                shutil.rmtree(pkg)
            _rename(old, pkg)
        except Exception as e2:
            raise UpdateError(f"update failed ({e}) and restoring failed: {e2}. Old version is in {old}")
        raise UpdateError(f"update failed, previous version kept ({e})")
    shutil.rmtree(old, ignore_errors=True)


# ----------------------------------------------------------------------
# orchestration
# ----------------------------------------------------------------------
def _set(st, msg=""):
    global state, message
    state, message = st, msg


def run_update(current, pkg=None, resolve=None, opener=None):
    """Blocking. Returns True on success. Never raises; progress / result in state + message."""
    global new_tag
    pkg = Path(pkg) if pkg else package_dir()
    tmp = pkg.parent / TMP_NAME
    try:
        why = blocked_reason(pkg)
        if why:
            raise UpdateError(f"update not possible: {why}")
        _set("running", "Checking the latest release...")
        found = (resolve or update_check.resolve_latest)(current)
        if not found:
            raise UpdateError("already up to date")
        asset = found["asset"]
        if not asset:
            raise UpdateError(f"{update_check.ASSET_NAME} not found in the release")
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir()
        _set("running", "Downloading...")
        zip_path = tmp / update_check.ASSET_NAME
        download(asset["url"], zip_path, asset["size"], asset["sha256"], opener)
        _set("running", "Verifying...")
        extract_verified(zip_path, tmp / "new" / PKG_NAME, found["version"])
        _set("running", "Installing...")
        swap(pkg, tmp / "new" / PKG_NAME, found["version"])
        new_tag = found["tag"]
        _set("done", f"Updated to {found['tag']}. Please restart Blender.")
        return True
    except UpdateError as e:
        _set("error", str(e))
    except Exception as e:  # network errors etc.
        _set("error", f"update failed: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return False


def start(current):
    """Non-blocking; the panel polls `state`. Does nothing if an update is already running / finished."""
    if state in ("running", "done"):
        return False
    _set("running", "Starting...")
    threading.Thread(target=run_update, args=(current,), name="collab-update", daemon=True).start()
    return True
