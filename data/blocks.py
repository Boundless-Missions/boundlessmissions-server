"""
data/blocks.py – player-to-player blocks and mutes.

This is the service-level block the multiplayer presence layer needs before it can
show one player where another is or let them reach each other. It did not exist:
`data/friends.py` models only friends/incoming/outgoing, and `cogs/moderation.py`
`mute` is a Discord guild timeout, not an account-to-account relationship. A block
here is the account-graph counterpart of a friendship, and its whole job is to be
consulted by the visibility filter (`ksp-mp-presence-visibility.md` §7): a blocked
player sees nothing of the blocker and cannot contact them, and that override beats
every visibility tier and every rendezvous auto-approval.

Two relationships, different shapes because they mean different things:

  * **block** — *symmetric in effect, one-directional in storage*. "A blocks B"
    hides A and B from each other and forbids contact in both directions. But only
    A authored it, so only A's document records it (`blocked`). The symmetry lives
    in the *read*: "is there a block between A and B" checks both A's list and B's
    list. This is deliberately unlike `friends.py`'s both-documents write. A block
    is one person's decision, and the popular-player failure mode is real — if a
    thousand people block one streamer, a denormalised "blocked_by" copy on the
    streamer's document would blow past Firestore's 1 MiB or force a lossy cap that
    silently drops blocks. Storing each block only on its author's document keeps
    every document bounded by that *author's* own block count, which is small, and
    a two-document read at the gate is cheap and never lossy.

  * **mute** — *one-directional in every sense*. "A mutes B" stops B's contact
    requests and notifications reaching A; it does not hide anyone from anyone. It
    lives only on A's document (`muted`) and is never consulted symmetrically.

Document shape (`blocks/{account_id}` — one per player):

    {
      "blocked": { "<other_id>": {"at": <epoch>} },   # people I have blocked
      "muted":   { "<other_id>": {"at": <epoch>} },   # people I have muted
      "updated": <epoch>,
    }

**Keyed on account ids** exactly as `data/accounts.py` and `data/friends.py` define
them (a Discord snowflake, or `a_<firebase uid>` for a website account) — a block
is between two people, not two people in a server, so it is guild-independent.

Reads fail **closed**, the same call the friend graph makes and for the same
reason turned up a notch: this gates *whether a stranger's machine is told where
you are and may contact you*. A Firestore blip that made a block silently
evaporate would expose exactly the person who asked not to be. "Couldn't check,
try again" is recoverable; a leaked position is not. Every read below raises
`BlocksUnavailable` on failure and every caller must refuse rather than assume the
absence of a block.

Writes are a single document's read-modify-write, run in a transaction so a block
and a concurrent unblock cannot lose each other. There is no cross-document
invariant to protect (a block is one document), so the transaction is for
lost-update safety, not atomic-pair safety.
"""

import logging
import time

from firebase_admin import firestore

from data.store import _db

log = logging.getLogger(__name__)

# Caps bound the *author's* own document — the whole record is read at the gate and
# rewritten on every change, against Firestore's 1 MiB limit. These are per-person
# and generous: a real block list is a handful of people, not thousands.
MAX_BLOCKED = 1000
MAX_MUTED = 1000

_EMPTY = {"blocked": {}, "muted": {}}


class BlocksUnavailable(Exception):
    """The record could not be read. Callers must refuse, never assume no block."""


def _col():
    return _db.collection("blocks")


def _now() -> float:
    return time.time()


def _norm(record: dict | None) -> dict:
    """A stored record with its two maps guaranteed present.

    The document is created lazily — the first block a player ever sets is the
    first time their document exists — so every read has to cope with an absent or
    half-shaped one.
    """
    d = dict(record or {})
    for key in ("blocked", "muted"):
        val = d.get(key)
        d[key] = dict(val) if isinstance(val, dict) else {}
    return d


# ── Reads (fail closed) ───────────────────────────────────────────────────────

def get_record(account_id) -> dict:
    """One player's block and mute lists. Raises `BlocksUnavailable` on a failed
    read — never answers "no block" to a Firestore error."""
    aid = str(account_id)
    try:
        snap = _col().document(aid).get()
    except Exception as exc:
        log.warning("Block record read failed for %s: %s", aid, exc)
        raise BlocksUnavailable(str(exc)) from exc
    return _norm(snap.to_dict() if snap.exists else None)


def blocked_ids(account_id) -> set[str]:
    """The set of accounts this player has blocked (one read)."""
    return set(get_record(account_id)["blocked"].keys())


def muted_ids(account_id) -> set[str]:
    """The set of accounts this player has muted (one read)."""
    return set(get_record(account_id)["muted"].keys())


def blocks(a, b) -> bool:
    """Whether `a` has blocked `b` — authoritative, one read of `a`'s document."""
    return str(b) in get_record(a)["blocked"]


def is_muted(a, b) -> bool:
    """Whether `a` has muted `b` — one read of `a`'s document."""
    return str(b) in get_record(a)["muted"]


def either_blocks(a, b) -> bool:
    """Whether a block exists between `a` and `b` in *either* direction.

    This is the gate the visibility filter and the contact surface call: a block is
    symmetric in effect, so either party having blocked the other hides both and
    forbids contact. Two reads, both authoritative, neither lossy — see the module
    docstring on why the block is not denormalised to answer this in one.
    """
    a, b = str(a), str(b)
    return blocks(a, b) or blocks(b, a)


# ── Writes ────────────────────────────────────────────────────────────────────

def _doc(rec: dict, now: float) -> dict:
    # A whole-document `set` (no merge): merge would deep-merge the maps, so a key
    # removed from `blocked` in memory would survive the write and an unblock would
    # do nothing. Both maps were just read in this transaction, so replacing the
    # document wholesale is both correct and the only shape that expresses a
    # deletion. It also creates the document lazily.
    return {"blocked": rec["blocked"], "muted": rec["muted"], "updated": now}


def _mutate(account_id, apply) -> tuple[bool, str]:
    """Read-modify-write one player's document in a transaction.

    `apply(rec)` edits the normalised record in place and returns `(ok, message)`;
    the document is written only when `ok` is true, so a no-op refusal costs no
    write.
    """
    aid = str(account_id)
    ref = _col().document(aid)
    transaction = _db.transaction()

    @firestore.transactional
    def _run(txn) -> tuple[bool, str]:
        snap = ref.get(transaction=txn)
        rec = _norm(snap.to_dict() if snap.exists else None)
        ok, msg = apply(rec)
        if ok:
            txn.set(ref, _doc(rec, _now()))
        return ok, msg

    try:
        return _run(transaction)
    except Exception as exc:
        log.warning("Block mutation for %s failed: %s", aid, exc)
        raise BlocksUnavailable(str(exc)) from exc


def _entry(handle: str, name: str) -> dict:
    """A stored map entry. The handle is denormalised in so the list endpoint can
    render names from one document read instead of one account read per entry —
    the same trade `cogs/corps.py` makes with `owner_username`, and safe for the
    same reason: the handle is immutable. The display name is a best-effort copy
    that may go stale, which is acceptable for a "who have I blocked" list.
    """
    e = {"at": _now()}
    if handle:
        e["handle"] = str(handle)
    if name:
        e["name"] = str(name)
    return e


def block(from_id, to_id, *, handle: str = "", name: str = "") -> tuple[bool, str]:
    """`from_id` blocks `to_id`. Idempotent; refuses self and a full list."""
    a, b = str(from_id), str(to_id)
    if a == b:
        return False, "You can't block yourself."

    def _apply(rec) -> tuple[bool, str]:
        if b in rec["blocked"]:
            return False, "You've already blocked that player."
        if len(rec["blocked"]) >= MAX_BLOCKED:
            return False, f"Your block list is full ({MAX_BLOCKED})."
        rec["blocked"][b] = _entry(handle, name)
        # A block ends a mute of the same person — the block is the stronger,
        # supersetting relationship, and leaving a mute behind would be dead state.
        rec["muted"].pop(b, None)
        return True, "Blocked."

    return _mutate(a, _apply)


def unblock(from_id, to_id) -> tuple[bool, str]:
    """`from_id` removes its block of `to_id`."""
    a, b = str(from_id), str(to_id)

    def _apply(rec) -> tuple[bool, str]:
        if b not in rec["blocked"]:
            return False, "You haven't blocked that player."
        rec["blocked"].pop(b, None)
        return True, "Unblocked."

    return _mutate(a, _apply)


def mute(from_id, to_id, *, handle: str = "", name: str = "") -> tuple[bool, str]:
    """`from_id` mutes `to_id` (one-sided: stops their contact reaching me)."""
    a, b = str(from_id), str(to_id)
    if a == b:
        return False, "You can't mute yourself."

    def _apply(rec) -> tuple[bool, str]:
        if b in rec["blocked"]:
            return False, "You've already blocked that player."
        if b in rec["muted"]:
            return False, "You've already muted that player."
        if len(rec["muted"]) >= MAX_MUTED:
            return False, f"Your mute list is full ({MAX_MUTED})."
        rec["muted"][b] = _entry(handle, name)
        return True, "Muted."

    return _mutate(a, _apply)


def unmute(from_id, to_id) -> tuple[bool, str]:
    """`from_id` removes its mute of `to_id`."""
    a, b = str(from_id), str(to_id)

    def _apply(rec) -> tuple[bool, str]:
        if b not in rec["muted"]:
            return False, "You haven't muted that player."
        rec["muted"].pop(b, None)
        return True, "Unmuted."

    return _mutate(a, _apply)


def forget_account(account_id) -> None:
    """Erase this account's own block/mute document — the data-purge path.

    Only the author's document is deleted. A block this account *received* lives on
    the *other* player's document (blocks are stored one-directionally), and that
    is the other player's own record to keep or clear — this purge has no business
    editing it, and could not find every such document without a collection scan
    anyway. Best-effort: it runs after the account is gone, so there is nothing
    left to keep consistent, only this one document to sweep.
    """
    aid = str(account_id)
    try:
        _col().document(aid).delete()
    except Exception as exc:                       # pragma: no cover - defensive
        log.warning("Block purge: could not delete %s: %s", aid, exc)
