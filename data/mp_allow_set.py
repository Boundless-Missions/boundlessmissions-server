"""
data/mp_allow_set.py – compute and mint a player's presence allow-set.

The bot's half of the allow-set contract (`ksp-mp-presence-visibility.md` §6.4).
The game server enforces the presence tiers but must stay Firestore-free, so it
holds, per session, a signed **allow-set** — "which accounts may see me" — that
this module resolves from the subject's tier + friends + blocks, hashes, and hands
to `mp_keys.mint_allow_set_token` to sign. The client carries it; the game server
verifies it OFFLINE against the same JWKS as a player token (its verifier is
`common/tokens.py::verify_allow_set_token` in the Multiplayer-Server repo).

Kept out of `api_server.py` for the same reason `data/mp_profiles.py` is: a pure
data-layer function with no HTTP dependency, unit-testable against a fake
Firestore directly. The endpoint (`GET /api/v1/mp/allow-set`) is a thin wrapper —
authenticate the subject, call `mint()`.

Two properties this module holds:

  * **Fails closed.** Any unreadable input — tier, blocks or friends — raises
    `AllowSetUnavailable` rather than minting a set that over-exposes (a dropped
    block) or mis-tiers. An outage withholds a token; it never issues a permissive
    one. This is the *subject's own* exposure, so the failure the caller sees is
    "couldn't refresh your visibility", never a leak.

  * **The set_version is edit-driven and needs no counter.** Every mutation that
    can narrow visibility already stamps `updated` on the subject's own document —
    `mp_visibility.set_tier`, `blocks._doc`, `friends._doc` all do — so the newest
    of those three stamps IS a monotonic, durable version that rises on exactly
    the edits that matter, with no separate counter to keep and no write-hook to
    forget. The game server keeps the highest version it has seen per account and
    refuses a lower one, which is what closes downgrade-replay (§6.4 property 2).
    Milliseconds, so two edits a second apart still order.

Note on blocks: `block_accounts` is the **forward** direction only — accounts the
subject blocked (`blocks.blocked_ids`), which is bounded by the subject's own
document. The reverse (accounts that blocked the subject) is deliberately not
enumerable — `data/blocks.py` refuses to denormalise a `blocked_by` list — so it
cannot go in this token. The reverse direction is enforced elsewhere: the profile
resolve (`data/mp_profiles.py`) already checks it, and the presence-marker gate is
the game server's to close from the *viewer's* own allow-set. See the handoff.
"""

import logging

from data import mp_keys
from data import mp_visibility
from data import friends as friends_db
from data import blocks as blocks_db

log = logging.getLogger(__name__)


class AllowSetUnavailable(Exception):
    """The allow-set could not be computed from account state. Callers fail closed."""


def _stamp(rec: dict) -> float:
    """The `updated` epoch on a friends/blocks record, or 0.0 if absent/malformed."""
    try:
        return float((rec or {}).get("updated") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def compute(account_id) -> dict:
    """Resolve the subject's tier + friends + blocks into the token inputs.

    Returns ``{tier, allow_accounts (set), block_accounts (set), set_version (int)}``.
    Raises `AllowSetUnavailable` if any input cannot be read — the friends and
    blocks reads already fail closed by raising, and the tier read does too.
    """
    aid = str(account_id)

    try:
        tier, vis_updated = mp_visibility.get_setting(aid)
    except mp_visibility.VisibilityUnavailable as exc:
        raise AllowSetUnavailable(f"visibility unavailable: {exc}") from exc

    try:
        brec = blocks_db.get_record(aid)
    except blocks_db.BlocksUnavailable as exc:
        raise AllowSetUnavailable(f"blocks unavailable: {exc}") from exc
    block_accounts = set(brec["blocked"].keys())
    blocks_updated = _stamp(brec)

    # Read the friends record even when the tier does not use it (public/hidden):
    # a friend edit must still bump set_version, so a player who edits their
    # friends list while public and only later flips to friends gets a version
    # that already reflects the edit rather than a stale one.
    try:
        frec = friends_db.get_record(aid)
    except friends_db.FriendsUnavailable as exc:
        raise AllowSetUnavailable(f"friends unavailable: {exc}") from exc
    friend_accounts = set(frec["friends"].keys())
    friends_updated = _stamp(frec)

    # The friends set is the allow list ONLY at the friends tier. At `public`
    # everyone is admitted (minus blocks) so the list is unused, and at `hidden`
    # no one is — passing it only when it is meaningful keeps the token minimal and
    # avoids publishing a hidden/public player's friend-list *size* (the hashes
    # hide who, never how many) to the game-server operator.
    allow_accounts = (friend_accounts - block_accounts) if tier == mp_visibility.FRIENDS else set()

    set_version = int(max(vis_updated, blocks_updated, friends_updated) * 1000)

    return {
        "tier": tier,
        "allow_accounts": allow_accounts,
        "block_accounts": block_accounts,
        "set_version": set_version,
    }


def mint(account_id, universe_id, *, at_wc: float | None = None) -> dict:
    """Compute and sign the subject's allow-set token for one universe.

    Returns the endpoint response: ``{token, expires_wc, universe_id, tier,
    set_version}``. No account ids and no raw friend/block ids leave here — the
    token carries only salted hashes, and the response names the subject only by
    the universe it already asked for.
    """
    aid = str(account_id)
    universe_id = str(universe_id or "").strip()
    if not universe_id:
        raise mp_keys.MpKeyError("cannot mint an allow-set with no universe")

    spec = compute(aid)
    token, expires = mp_keys.mint_allow_set_token(
        account_id=aid,
        universe_id=universe_id,
        tier=spec["tier"],
        set_version=spec["set_version"],
        allow_accounts=spec["allow_accounts"],
        block_accounts=spec["block_accounts"],
        at_wc=at_wc,
    )
    return {
        "token": token,
        "expires_wc": expires,
        "universe_id": universe_id,
        "tier": spec["tier"],
        "set_version": spec["set_version"],
    }
