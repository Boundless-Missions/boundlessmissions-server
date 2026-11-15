"""
data/firstparty.py – the signed first-party release list the BM Launcher installs from.

Our own mods (BoundlessMissions, BoundlessMultiplayer) are published by the owner
from their PC with `BM Launcher/tools/publish_firstparty.py`, which uploads a zip and
then rewrites `releases.json` atomically in `FIRSTPARTY_DIR` (bm-launcher.md §24).
This module only *reads* that directory; nothing in the bot writes to it.

    releases.json = [{"entry": "<canonical JSON text>", "sig": "<base64 Ed25519>"}, ...]

The bot does **not** verify signatures and holds no key: the launcher does, against
the public key compiled into it, so a compromised bot or VPS can at most withhold a
release, never substitute one. Each entry is signed on its own, which is what lets
this module drop `channel: "testing"` rows for ordinary accounts without touching the
signatures of the rest. Rows are passed through byte for byte — `entry` is the exact
signed text and must never be re-serialised here.

Visibility of testing rows is decided by the caller (owner / guild admins / testers —
the same gate as the multiplayer server page). A testing file is likewise only
downloadable by someone who may see its row: the download route requires the name to
be named by a *visible* entry, so guessing a filename gains nothing.

Fails closed: an unreadable or malformed list reads as "no releases".
"""

import json
import logging
import os
import re

log = logging.getLogger(__name__)

# Readable to the bot under ProtectSystem=strict, like MP_RELEASE_DIR.
FIRSTPARTY_DIR = os.getenv("BM_FIRSTPARTY_DIR", "/var/lib/bm-firstparty")

CHANNELS = ("stable", "testing")
# `<Mod>-<version>.zip` — no path separators, no leading dot, bounded. The publish
# tool refuses versions outside the same alphabet.
FILE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,63}-[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}\.zip$")
# A list far larger than this is not something the publish tool would write.
MAX_LIST_BYTES = 2 * 1024 * 1024


def _parse(raw: bytes) -> list:
    """The well-formed rows of a releases.json, each as (row, entry-dict). A row that
    cannot be understood is dropped (and logged) rather than failing the whole list."""
    try:
        rows = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        log.warning("firstparty: releases.json is not JSON")
        return []
    if not isinstance(rows, list):
        log.warning("firstparty: releases.json is not a list")
        return []
    out = []
    for row in rows:
        try:
            text, sig = row["entry"], row["sig"]
            if not isinstance(text, str) or not isinstance(sig, str):
                raise TypeError
            entry = json.loads(text)
            if (not isinstance(entry, dict) or entry.get("channel") not in CHANNELS
                    or not isinstance(entry.get("file"), str)
                    or not FILE_RE.match(entry["file"])):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            log.warning("firstparty: dropping a malformed release row")
            continue
        out.append(({"entry": text, "sig": sig}, entry))
    return out


def load(directory: str = None) -> list:
    """Every well-formed row on disk, as (row, entry) pairs. [] if none / unreadable."""
    path = os.path.join(directory or FIRSTPARTY_DIR, "releases.json")
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_LIST_BYTES + 1)
    except FileNotFoundError:
        return []
    except OSError:
        log.exception("firstparty: cannot read %s", path)
        return []
    if len(raw) > MAX_LIST_BYTES:
        log.warning("firstparty: releases.json exceeds %d bytes; ignored", MAX_LIST_BYTES)
        return []
    return _parse(raw)


def visible(pairs: list, include_testing: bool) -> list:
    """The pairs this caller may see: stable always, testing only when allowed."""
    return [(row, e) for row, e in pairs if include_testing or e["channel"] == "stable"]


def file_path(name: str, pairs: list, directory: str = None):
    """The on-disk path of a release zip, or None. The name must match FILE_RE, be
    named by one of `pairs` (already filtered by `visible`), and resolve to a regular
    file directly inside the release directory (no symlink out of it)."""
    if not isinstance(name, str) or not FILE_RE.match(name):
        return None
    if not any(e["file"] == name for _, e in pairs):
        return None
    base = os.path.realpath(directory or FIRSTPARTY_DIR)
    path = os.path.realpath(os.path.join(base, name))
    if os.path.dirname(path) != base or not os.path.isfile(path):
        return None
    return path
