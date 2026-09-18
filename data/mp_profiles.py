"""
data/mp_profiles.py – resolve handles to the profiles a requester may see.

The filter that IS the visibility model (`ksp-mp-presence-visibility.md` §6.2),
kept out of `api_server.py` so it is a pure data-layer function with no HTTP or
FastAPI dependency and can be unit-tested against a fake Firestore directly. The
endpoint (`POST /api/v1/mp/profiles`) is a thin wrapper: authenticate the
requester, cap and dedup the batch, call `resolve()`.

The whole point is that "you may not see this" collapses to a single answer —
`None` — for every reason: an unknown handle, a block in either direction, a tier
that excludes the requester, or a read the filter could not complete. They are
deliberately indistinguishable, so the endpoint never reveals that a block exists
or that a hidden player does.

Two invariants this module holds:

  * **Never returns an account id.** A resolved profile is keyed and identified by
    the immutable handle; the internal account id stays in the account layer
    (design §5).
  * **Fails closed.** Any Firestore error on the subject's side (its block state
    or its tier) yields `None` for that subject rather than a leaked profile. The
    one deliberate exception is the *requester's own* block list being unreadable,
    which only risks under-hiding people the requester chose not to see — a minor
    annoyance to them, never a leak of a subject who blocked them.
"""

import logging

from data import accounts
from data import friends as friends_db
from data import blocks as blocks_db
from data import mp_visibility
from data.store import sign_stored, SIGNED_URL_MAX_TTL

log = logging.getLogger(__name__)


def _display(acct: dict) -> str:
    """The name to show, falling through what the account might have. Mirrors
    `api_server._account_display`; kept here so this module has no HTTP-layer
    dependency."""
    for key in ("display_name", "username", "discord_username"):
        value = str((acct or {}).get(key) or "").strip()
        if value:
            return value
    return "Player"


def profile_one(aid, handle, requester, requester_blocked,
                friend_set, outgoing, incoming, friends_ok):
    """Resolve one account to the profile `requester` may see, or `None`."""
    aid = str(aid)
    acct = accounts.get_account(aid)
    if acct is None:
        return None

    is_self = (aid == requester)
    if not is_self:
        # A block in EITHER direction hides both parties. The requester side is
        # the batch set; the subject side is authoritative on the subject's own
        # document, and an unreadable one fails closed.
        if aid in requester_blocked:
            return None
        try:
            if blocks_db.blocks(aid, requester):
                return None
        except blocks_db.BlocksUnavailable:
            return None

        try:
            tier = mp_visibility.get_tier(aid)
        except mp_visibility.VisibilityUnavailable:
            return None

        if tier == mp_visibility.PUBLIC:
            admit = True
        elif tier == mp_visibility.FRIENDS:
            admit = friends_ok and (aid in friend_set)
        else:  # HIDDEN — presence advertises to no one; explicit per-craft grants
               # (server side) are the only way in and are not resolved here.
            admit = False
        if not admit:
            return None

    if is_self:
        rel = "self"
    elif aid in friend_set:
        rel = "friends"
    elif aid in outgoing:
        rel = "outgoing"
    elif aid in incoming:
        rel = "incoming"
    else:
        rel = "none"

    return {
        "handle": str(acct.get("username") or handle),
        # Returned raw (already length-capped at set time); the mod runs it through
        # TextSanitizer before drawing it (design §8, an untrusted harassment
        # surface).
        "display_name": _display(acct),
        # A signed platform URL, never an arbitrary one the mod would fetch blindly.
        "avatar_url": sign_stored(acct.get("avatar_url"), ttl=SIGNED_URL_MAX_TTL) or "",
        "relationship": rel,
        "contactable": (not is_self),
    }


def resolve(requester_id, handles) -> dict:
    """Resolve a batch of handles for one requester -> {handle: profile | None}.

    Reads the requester-side sets (blocks, friends) once for the whole batch, then
    resolves each distinct account once. All Firestore work is synchronous; the
    caller runs this in a worker thread.
    """
    requester = str(requester_id)

    try:
        requester_blocked = blocks_db.blocked_ids(requester)
    except blocks_db.BlocksUnavailable:
        requester_blocked = set()

    try:
        fr = friends_db.get_record(requester)
        friend_set = set(fr["friends"].keys())
        outgoing = set(fr["outgoing"].keys())
        incoming = set(fr["incoming"].keys())
        friends_ok = True
    except friends_db.FriendsUnavailable:
        friend_set = outgoing = incoming = set()
        friends_ok = False   # friends-tier subjects fail closed

    out: dict = {}
    by_account: dict = {}
    for raw in (handles or []):
        h = str(raw or "").strip()
        if not h or h in out:
            continue
        aid = accounts.account_for_username(h)
        if not aid:
            out[h] = None
            continue
        aid = str(aid)
        if aid in by_account:
            out[h] = by_account[aid]
            continue
        result = profile_one(aid, h, requester, requester_blocked,
                             friend_set, outgoing, incoming, friends_ok)
        by_account[aid] = result
        out[h] = result
    return out
