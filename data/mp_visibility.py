"""
data/mp_visibility.py – a player's multiplayer visibility tier.

The discovery axis of `ksp-mp-presence-visibility.md` §2, promoted from the
existing boolean opt-out to a grade. One setting per account, three tiers:

  * **public**  — any player in the universe may discover, locate and identify
                  this player's visible craft.
  * **friends** — only accounts in the player's friends list get discovery,
                  position and identity; everyone else sees nothing (the craft is
                  *absent* from their overlay, not shown anonymised). **This is the
                  default.**
  * **hidden**  — presence advertises this player to no one. Explicit per-craft
                  subscription grants (server side) still work; presence just does
                  not surface them to the crowd.

Why `friends` is the default and not `public` or `hidden` is argued in the design
doc §3.1: `public` would broadcast location + identity the instant a player joins;
`hidden` would make the overlay empty for everyone and train players to flip to
`public` to make the feature work at all. `friends` exposes a player only to
accounts they have already chosen a relationship with, which is conservative but
usable out of the box.

This is the *server-side* tier, read by the bot profile-resolve endpoint to filter
what identity each requester may see. It is a distinct axis from the client-side
`StreamerMode` redaction (what is drawn on the player's *own* screen) — the two are
kept separate on purpose (design §3.3), and the legacy client `hidePlayerDetails`
setting is a client concern that the client migrates into this tier when it first
sets one; there is no server-side legacy value to migrate from.

Document shape (`mp_visibility/{account_id}`):

    { "tier": "friends", "updated": <epoch> }

Reads distinguish "not set" from "could not read": an absent document is the
default tier (a player who never touched the setting is `friends`), while a
Firestore error raises `VisibilityUnavailable`. The resolve endpoint treats that
raise as fail-closed — an identity it cannot confirm you may see is not served —
so an outage withholds names rather than leaking them.
"""

import logging
import time

from data.store import _db

log = logging.getLogger(__name__)

PUBLIC = "public"
FRIENDS = "friends"
HIDDEN = "hidden"

TIERS = (PUBLIC, FRIENDS, HIDDEN)
DEFAULT_TIER = FRIENDS


class VisibilityUnavailable(Exception):
    """The tier could not be read. Callers must fail closed, never assume public."""


def _col():
    return _db.collection("mp_visibility")


def _now() -> float:
    return time.time()


def normalize_tier(tier) -> str | None:
    """The stored form of a tier, or None if it is not one of the three."""
    t = str(tier or "").strip().lower()
    return t if t in TIERS else None


def get_tier(account_id) -> str:
    """This account's visibility tier.

    An absent document is the default (`friends`); a read error raises
    `VisibilityUnavailable`. A stored value that is somehow not one of the three
    tiers is treated as the default rather than trusted — a malformed setting must
    not read as `public`.
    """
    aid = str(account_id)
    try:
        snap = _col().document(aid).get()
    except Exception as exc:
        log.warning("Visibility tier read failed for %s: %s", aid, exc)
        raise VisibilityUnavailable(str(exc)) from exc
    if not snap.exists:
        return DEFAULT_TIER
    return normalize_tier((snap.to_dict() or {}).get("tier")) or DEFAULT_TIER


def set_tier(account_id, tier) -> tuple[bool, str]:
    """Set this account's visibility tier. Returns (ok, message)."""
    aid = str(account_id)
    t = normalize_tier(tier)
    if t is None:
        return False, "That is not a valid visibility setting."
    try:
        _col().document(aid).set({"tier": t, "updated": _now()})
    except Exception as exc:
        log.warning("Visibility tier write failed for %s: %s", aid, exc)
        return False, "Couldn't save that setting just now. Try again."
    return True, "Saved."


def forget_account(account_id) -> None:
    """Delete this account's tier document — the data-purge path. Best-effort."""
    aid = str(account_id)
    try:
        _col().document(aid).delete()
    except Exception as exc:                       # pragma: no cover - defensive
        log.warning("Visibility purge: could not delete %s: %s", aid, exc)
