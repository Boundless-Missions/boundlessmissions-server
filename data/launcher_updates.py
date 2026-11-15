"""
data/launcher_updates.py – the BM Launcher's own updates (bm-launcher.md §25).

The owner builds and signs the launcher on their PC (`BM Launcher/tools/
publish_launcher.py`, minisign key never leaves it) and uploads the installer plus
`releases.json` into `LAUNCHER_DIR`. This module only reads that directory.

    releases.json = [{
        "version": "0.1.1", "channel": "stable" | "testing", "notes": "…",
        "pub_date": "<RFC 3339>",
        "platforms": {"linux-x86_64-appimage": {"file": "<name>", "signature": "<minisign .sig text>"}}
    }, …]

The bot answers the Tauri updater's question "is there something newer than X for
this platform?" in the plugin's own format, or "no" (204). It verifies nothing: the
launcher's updater plugin checks the minisign signature against the public key
compiled into it, so a compromised bot can withhold an update but not substitute one.
Testing builds reach only the owner / testers / guild admins (the caller decides).

Fails closed: an unreadable or malformed list means "no update".
"""

import json
import logging
import os
import re

log = logging.getLogger(__name__)

LAUNCHER_DIR = os.getenv("BM_LAUNCHER_RELEASE_DIR", "/var/lib/bm-launcher-releases")
CHANNELS = ("stable", "testing")
# A plain file name: no path, no leading dot, bounded.
FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
VERSION_RE = re.compile(r"^(\d{1,6})\.(\d{1,6})\.(\d{1,6})(?:-([0-9A-Za-z.-]{1,32}))?$")
# Tauri's `{{target}}-{{arch}}-{{bundle_type}}`. The bundle type is part of the key on
# purpose: the plugin installs by how the launcher was installed (an AppImage is
# swapped, a .deb goes through `pkexec dpkg -i`), so a .deb install must never be
# handed an AppImage, and vice versa.
PLATFORM_RE = re.compile(r"^(linux|windows|darwin)-(x86_64|aarch64|i686|armv7)-(appimage|deb|rpm|nsis|msi|app)$")
MAX_LIST_BYTES = 1024 * 1024


def version_key(v: str):
    """Semver order (numeric core; a pre-release sorts below its release). None if
    not a version this module understands."""
    m = VERSION_RE.match(v or "")
    if not m:
        return None
    pre = m.group(4)
    pre_key = (1,) if pre is None else (0,) + tuple(
        (0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split("."))
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), pre_key)


def _valid(row) -> bool:
    try:
        if row["channel"] not in CHANNELS or version_key(row["version"]) is None:
            return False
        if not isinstance(row.get("notes", ""), str) or not isinstance(row.get("pub_date", ""), str):
            return False
        plats = row["platforms"]
        if not isinstance(plats, dict):
            return False
        for name, p in plats.items():
            if not PLATFORM_RE.match(name) or not FILE_RE.match(p["file"]) or \
                    not isinstance(p["signature"], str) or not p["signature"]:
                return False
        return True
    except (KeyError, TypeError):
        return False


def load(directory: str = None) -> list:
    path = os.path.join(directory or LAUNCHER_DIR, "releases.json")
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_LIST_BYTES + 1)
    except FileNotFoundError:
        return []
    except OSError:
        log.exception("launcher updates: cannot read %s", path)
        return []
    if len(raw) > MAX_LIST_BYTES:
        log.warning("launcher updates: releases.json too large; ignored")
        return []
    try:
        rows = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        log.warning("launcher updates: releases.json is not JSON")
        return []
    if not isinstance(rows, list):
        return []
    out = [r for r in rows if isinstance(r, dict) and _valid(r)]
    if len(out) != len(rows):
        log.warning("launcher updates: dropped %d malformed row(s)", len(rows) - len(out))
    return out


def newest_for(rows: list, platform: str, current: str, include_testing: bool):
    """The newest release for `platform` strictly newer than `current`, or None."""
    cur = version_key(current)
    if cur is None or not PLATFORM_RE.match(platform or ""):
        return None
    best = None
    for r in rows:
        if r["channel"] != "stable" and not include_testing:
            continue
        if platform not in r["platforms"]:
            continue
        k = version_key(r["version"])
        if k > cur and (best is None or k > version_key(best["version"])):
            best = r
    return best


def file_path(name: str, rows: list, include_testing: bool, directory: str = None):
    """The on-disk path of an installer named by a release this caller may see, or None."""
    if not isinstance(name, str) or not FILE_RE.match(name):
        return None
    if not any((include_testing or r["channel"] == "stable")
               and any(p["file"] == name for p in r["platforms"].values()) for r in rows):
        return None
    base = os.path.realpath(directory or LAUNCHER_DIR)
    path = os.path.realpath(os.path.join(base, name))
    if os.path.dirname(path) != base or not os.path.isfile(path):
        return None
    return path
