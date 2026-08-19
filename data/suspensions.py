"""
data/suspensions.py – temporary suspension from the mod/website services.

What this is *not*: a Discord ban. Kicks, bans and timeouts already exist in
`cogs/moderation.py` and act on guild membership. This acts on the API surface
instead — the KSP client and the website — which is the only place abuse of the
in-game features (bug-report spam, marketplace reports, contract griefing) can
actually be answered without throwing someone out of the community.

It is temporary by construction. Every record carries an expiry and there is no
"forever" value: a permanent removal is a Discord ban, a deliberate act with its
own audit trail, and dressing one up as a suspension that merely never ends would
hide it from both the player and the mod team. `MAX_HOURS` caps a single
suspension at a year.

Document shape (`suspensions/{user_id}`):

    {
        "user_id":    "123…",
        "reason":     "Filed 40 junk bug reports",   # shown to the player
        "by":         "owner#0",                     # who issued it
        "hours":      72,                            # as issued
        "created_at": "<iso8601>",
        "until":      1755600000.0,                  # epoch seconds — the truth
        "until_iso":  "<iso8601>",                   # display copy
        "lifted":     false, "lifted_at": …, "lifted_by": …
    }

Lifting sets `until` to now rather than deleting the document, so the history
survives and "is this user suspended" stays the single comparison `until > now`
on both the read path and the `list_active` query. Expiry needs no sweeper for
the same reason.

The read path runs on *every authenticated request*, so it is cached in-process
for `_CACHE_TTL` seconds — the same trick `api_auth._get_token_version` uses, and
for the same reason. Writers update the cache in place, so an admin's suspend or
lift takes effect on the next request rather than up to a TTL later. A cache
entry never outlives the suspension it describes: the TTL is clamped to the time
remaining, so a suspension that ends in 4 seconds is not enforced for 30.

A Firestore read failure fails **open** (nobody is suspended). The alternative —
treating an unreachable database as "suspended" — would lock every player out of
the mod during an outage, which is a far worse failure than a suspended player
getting a few extra minutes.
"""

import logging
import time
from datetime import datetime, timezone

from data.store import _db

log = logging.getLogger(__name__)

# A single suspension may not exceed a year. Anything longer is a ban, and a ban
# is a Discord action.
MAX_HOURS = 365 * 24
MIN_HOURS = 1
REASON_MAX = 300

_CACHE_TTL = 30.0
# user_id -> (record or None, cached_at)
_cache: dict[str, tuple[dict | None, float]] = {}


def _col():
    return _db.collection("suspensions")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _active(rec: dict | None, now: float) -> dict | None:
    """The record, but only while it is still running."""
    if not rec:
        return None
    try:
        until = float(rec.get("until") or 0)
    except (TypeError, ValueError):
        return None
    return rec if until > now else None


def _cache_put(user_id: str, rec: dict | None, now: float) -> None:
    _cache[user_id] = (rec, now)


def get_active(user_id: str) -> dict | None:
    """The user's running suspension, or None. Cached; safe to call per request."""
    user_id = str(user_id)
    now = time.time()
    cached = _cache.get(user_id)
    if cached is not None:
        rec, at = cached
        # Clamp the TTL to what is left of the suspension, so the last cache entry
        # of a suspension cannot keep enforcing it past its own expiry.
        ttl = _CACHE_TTL
        active = _active(rec, at)
        if active is not None:
            ttl = min(ttl, max(0.0, float(active["until"]) - at))
        if now - at < ttl:
            return _active(rec, now)

    try:
        snap = _col().document(user_id).get()
        rec = snap.to_dict() if snap.exists else None
    except Exception as exc:
        # Fail open, and do not cache the guess — the next request re-reads.
        log.warning("Could not read suspension for %s: %s", user_id, exc)
        return None

    _cache_put(user_id, rec, now)
    return _active(rec, now)


def get_record(user_id: str) -> dict | None:
    """The stored document whatever its state (expired, lifted). Admin views only
    — the gate asks `get_active`."""
    try:
        snap = _col().document(str(user_id)).get()
        return snap.to_dict() if snap.exists else None
    except Exception as exc:
        log.warning("Could not read suspension record for %s: %s", user_id, exc)
        return None


def suspend(user_id: str, hours: float, reason: str, by: str) -> dict:
    """Suspend a user for `hours` from now. Replaces any existing suspension
    rather than stacking onto it — the console shows what is in force, and a
    second suspension issued while the first runs is a correction of it, not an
    addition to it. Returns the stored record."""
    user_id = str(user_id)
    hours = max(MIN_HOURS, min(float(hours), MAX_HOURS))
    now = time.time()
    until = now + hours * 3600.0
    rec = {
        "user_id": user_id,
        "reason": (reason or "").strip()[:REASON_MAX],
        "by": by,
        "hours": hours,
        "created_at": _iso(now),
        "until": until,
        "until_iso": _iso(until),
        "lifted": False,
        "lifted_at": None,
        "lifted_by": None,
    }
    _col().document(user_id).set(rec)
    _cache_put(user_id, rec, now)
    log.warning("Suspended %s for %.1fh by %s: %s", user_id, hours, by, rec["reason"])
    return rec


def lift(user_id: str, by: str) -> bool:
    """End a suspension early. Returns False if there was nothing running.

    The document is kept (with `until` moved to now) so the record of what
    happened survives being undone."""
    user_id = str(user_id)
    rec = get_record(user_id)
    now = time.time()
    if _active(rec, now) is None:
        # Still drop any cached copy: an expired suspension left in the cache is
        # harmless, but a stale one is not worth keeping either.
        _cache_put(user_id, rec, now)
        return False

    rec.update({
        "until": now,
        "until_iso": _iso(now),
        "lifted": True,
        "lifted_at": _iso(now),
        "lifted_by": by,
    })
    _col().document(user_id).set(rec)
    _cache_put(user_id, rec, now)
    log.warning("Lifted suspension on %s (by %s)", user_id, by)
    return True


def list_active() -> list[dict]:
    """Every suspension currently in force. One query — the console needs this per
    page of users, not per user."""
    now = time.time()
    try:
        docs = _col().where("until", ">", now).stream()
        return [d.to_dict() for d in docs]
    except Exception as exc:
        log.warning("Could not list active suspensions: %s", exc)
        return []


def summary(rec: dict | None) -> dict | None:
    """The public-facing view of a record: what the player is told, and what the
    console shows. `by` is deliberately included for the console only — callers
    that hand this to a player strip it."""
    if not rec:
        return None
    return {
        "reason": rec.get("reason") or "",
        "by": rec.get("by") or "",
        "hours": rec.get("hours") or 0,
        "until": float(rec.get("until") or 0),
        "until_iso": rec.get("until_iso") or "",
        "created_at": rec.get("created_at") or "",
    }
