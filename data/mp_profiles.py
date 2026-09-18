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
import time

from data import accounts
from data import friends as friends_db
from data import blocks as blocks_db
from data import mp_visibility
from data.store import sign_stored, SIGNED_URL_MAX_TTL

log = logging.getLogger(__name__)


# ── The resolve cache (design §6.4) ──────────────────────────────────────────
#
# The overlay resolves the visible owner set on a slow cadence, and a name or
# avatar changes rarely, so a short per-(requester, handle) cache turns a screen
# full of the same neighbours into almost no Firestore work — which is what keeps
# this off the cost_guard bill. 120 s sits in the design's 60–300 s band.
#
# **Only a positive profile is cached, never a `None`.** A `None` means one of:
# unknown handle, a block or hidden tier, a non-friend, or a *fail-closed* read
# error — and the last must not be frozen in as "invisible" for two minutes after
# Firestore recovers. Re-resolving the negatives every call is also the safer
# direction for a block that has just been placed: the moment a subject flips to
# blocked/hidden it resolves to `None`, and `None` is never what we serve from
# cache. A positive that a *reverse-direction* block should now hide can still be
# served for up to the TTL; the design accepts that cadence, and `clear_requester`
# collapses it to zero for the side that placed the block.
PROFILE_CACHE_TTL = 120.0          # seconds
_CACHE_MAX = 20000                 # hard ceiling across all requesters
_cache: dict = {}                  # (requester, handle) -> (expiry_monotonic, profile)


def _now() -> float:
    return time.monotonic()


def clear_requester(requester_id) -> None:
    """Drop everything this requester has cached.

    Called when the requester changes what *they* are allowed to see — placing or
    lifting a block — so the change takes effect for them at once rather than
    after the TTL. It cannot clear the *reverse* direction (how others see the
    requester); that relaxes on its own within the TTL, which the design permits.
    """
    r = str(requester_id)
    for key in [k for k in _cache if k[0] == r]:
        _cache.pop(key, None)


def clear_cache() -> None:
    """Empty the whole cache (tests, and any global privacy reset)."""
    _cache.clear()


def _cache_get(requester, handle, now):
    hit = _cache.get((requester, handle))
    if hit is None:
        return False, None
    expiry, profile = hit
    if expiry <= now:
        _cache.pop((requester, handle), None)
        return False, None
    return True, profile


def _cache_put(requester, handle, profile, now) -> None:
    # Only positives are ever stored (see the module note above).
    if profile is None:
        return
    if len(_cache) >= _CACHE_MAX:
        # Reclaim expired entries first; if that is not enough, shed the tranche
        # closest to expiry so a burst of distinct requesters cannot grow the map
        # without bound. Bounded work, and the cache is an optimisation — a
        # dropped entry only costs one re-resolve.
        for k in [k for k, (exp, _) in list(_cache.items()) if exp <= now]:
            _cache.pop(k, None)
        if len(_cache) >= _CACHE_MAX:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:max(1, _CACHE_MAX // 10)]:
                _cache.pop(k, None)
    _cache[(requester, handle)] = (now + PROFILE_CACHE_TTL, profile)


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


def resolve(requester_id, handles, *, use_cache=True, now=None) -> dict:
    """Resolve a batch of handles for one requester -> {handle: profile | None}.

    Serves cached positive profiles first (design §6.4), then reads the
    requester-side sets (blocks, friends) **once** for whatever remains and
    resolves each distinct account once. All Firestore work is synchronous; the
    caller runs this in a worker thread. On a full cache hit no Firestore read
    happens at all — not even the requester's own sets.

    `use_cache=False` and `now=` are for the tests; production passes neither.
    """
    requester = str(requester_id)
    ts = now if now is not None else _now()

    out: dict = {}
    misses: list = []   # distinct handles needing a live resolve, first-seen order
    for raw in (handles or []):
        h = str(raw or "").strip()
        if not h or h in out or h in misses:
            continue
        if use_cache:
            ok, profile = _cache_get(requester, h, ts)
            if ok:
                out[h] = profile
                continue
        misses.append(h)

    if not misses:
        return out

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

    by_account: dict = {}
    for h in misses:
        aid = accounts.account_for_username(h)
        if not aid:
            out[h] = None                       # not cached: an unclaimed handle
            continue                            # may be claimed within the TTL
        aid = str(aid)
        if aid in by_account:
            result = by_account[aid]
        else:
            result = profile_one(aid, h, requester, requester_blocked,
                                 friend_set, outgoing, incoming, friends_ok)
            by_account[aid] = result
        out[h] = result
        if use_cache:
            _cache_put(requester, h, result, ts)   # a no-op when result is None
    return out
