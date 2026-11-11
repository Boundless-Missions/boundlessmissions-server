"""
data/policy.py – Privacy Policy / Terms of Service version registry (Firestore).

A single config document (`config/policy`) holds the current policy version. The
KSP client records which version the player accepted (consent.cfg); the server
advertises the current version on /version/check, and when the server's version
is higher the client re-prompts the opt-in gate and stops transmitting until the
player re-accepts. Bumping the version here is the only step needed to force a
fleet-wide re-consent — no mod rebuild.

Document shape:
    config/policy
    {
        "version":     2,                       # monotonically increasing int
        "summary":     "Clarified telemetry retention.",
        "privacy_url": "https://...",           # optional display overrides
        "terms_url":   "https://...",
        "updated_at":  "<iso8601>",
        "updated_by":  "<discord user>"
    }
"""

import logging
import threading
import time
from datetime import datetime, timezone

from data.store import _db

log = logging.getLogger(__name__)

# Memoised for the same reason as data/mod_version's registry: `config/policy` is
# one rarely-changing document read on the anonymous `/version/check`, so an
# uncached read here is a metered Firestore operation per unauthenticated request.
# Short TTL as a backstop for an edit made outside this process; `invalidate()` is
# what makes a bump through the console take effect immediately. Failures are not
# cached — the fallback below already answers safely without help from a cache.
_CACHE_TTL = 60.0
_cache_lock = threading.Lock()
_cache: dict = {"at": 0.0, "doc": None}

# Baseline policy version. Shipped clients accept version 1, so the re-consent
# gate stays dormant until an admin publishes a higher version.
DEFAULT_VERSION = 1


def _doc():
    return _db.collection("config").document("policy")


def get_config(*, fresh: bool = False) -> dict:
    """Current policy document (empty dict if nothing has been published yet).

    Answered from a 60-second in-process cache; `invalidate()` clears it, and
    `set_version` refreshes it. `fresh=True` forces a read.
    """
    if not fresh:
        with _cache_lock:
            doc, at = _cache["doc"], float(_cache["at"])
        if doc is not None and (time.time() - at) < _CACHE_TTL:
            return dict(doc)
    snap = _doc().get()
    data = snap.to_dict() if snap.exists else {}
    with _cache_lock:
        _cache["doc"] = dict(data)
        _cache["at"] = time.time()
    return dict(data)


def invalidate() -> None:
    """Drop the memoised policy document. Called by the console after a bump, so
    a forced re-consent reaches clients on their next check rather than after the
    TTL."""
    with _cache_lock:
        _cache["doc"] = None
        _cache["at"] = 0.0


def get_version() -> int:
    """The policy version clients must have accepted. Falls back to the baseline
    if unset or malformed (so a missing/corrupt doc never forces a re-consent)."""
    try:
        return int(get_config().get("version") or DEFAULT_VERSION)
    except (TypeError, ValueError):
        return DEFAULT_VERSION


def get_effective_wc() -> int | None:
    """Unix seconds when the current version took effect, or None when unknown.

    `version_since` when stamped; a document published before that field existed
    falls back to `updated_at` — which is never EARLIER than the version's real
    start (it only moves later on a re-save), so the fallback can only ask for
    consent again, never skip it."""
    cfg = get_config()
    for key in ("version_since", "updated_at"):
        raw = cfg.get(key)
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except (TypeError, ValueError):
            continue
    return None


def set_version(version: int, updated_by: str, summary: str | None = None,
                privacy_url: str | None = None, terms_url: str | None = None) -> dict:
    """Publish a new policy version. Bumping this forces every client that
    accepted an older version to re-accept before it transmits again. Returns the
    stored document."""
    ref = _doc()
    snap = ref.get()
    data = snap.to_dict() if snap.exists else {}

    # When this version first took effect — stamped only when the number goes up, so
    # editing the summary or a URL of the current version never moves it. A client
    # consented before any server contact (it transmits nothing until it has) can
    # only know the baseline number, but it showed the live policy link, so consent
    # given on or after this moment counts for this version (get_effective_wc).
    if int(version) > int(data.get("version") or 0) or not data.get("version_since"):
        data["version_since"] = datetime.now(timezone.utc).isoformat()
    data["version"] = int(version)
    if summary is not None:
        data["summary"] = summary
    if privacy_url is not None:
        data["privacy_url"] = privacy_url
    if terms_url is not None:
        data["terms_url"] = terms_url
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    data["updated_by"] = updated_by
    ref.set(data)
    with _cache_lock:                 # the writer knows the new document
        _cache["doc"] = dict(data)
        _cache["at"] = time.time()
    return data
