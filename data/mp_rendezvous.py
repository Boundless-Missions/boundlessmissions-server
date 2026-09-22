"""
data/mp_rendezvous.py – a player's rendezvous / access auto-approval policy.

The bot's durable half of "form the bubble ON CONSENT" (design #7 of
`ksp-mp-presence-visibility.md`). An inbound access-request or rendezvous-request
raises a prompt on the owner's client; this module is the standing answer to
*"which of those may my client approve for me without asking"*. It is deliberately
**bot-side, not client-side**: it must be durable and cross-device (a player who
approved a friend on their laptop should not re-approve them on their desktop), so
it lives in Firestore beside `blocks.py`/`friends.py` and the client fetches it at
join exactly as it fetches its allow-set.

**The policy is advisory, never authoritative.** The game server holds no policy
and still requires an owner-attested resolve for every grant (`server.py`
`_access_resolve` / `_rendezvous_resolve`). This module only changes *whether the
owner's client prompts or auto-answers* — the resolve frame is identical either
way and still originates from the owner's own session. So a stale or over-broad
policy cannot grant anything the owner's client would not; it can only skip a
prompt.

Three modes, one setting per account:

  * **manual**  — prompt for every inbound request. **This is the default**: a
                  player who never touched the setting is asked, never silently
                  joined to a stranger's bubble.
  * **friends** — auto-approve a requester the owner is already friends with; still
                  prompt for everyone else. The friend test is applied *client-side*
                  off the profile the client already resolves for the requester
                  (`mp_profiles.resolve` returns ``relationship``), so this mode
                  costs no friend-list fan-out here.
  * **all**     — auto-approve any requester whose profile resolves (i.e. is not
                  blocked/hidden). The last-resort "I'm hosting, let anyone in"
                  knob.

Plus an **explicit allow list** — specific handles the owner always auto-approves
regardless of mode. Denormalised handle+name in at write time (same trade as
`blocks._entry`, safe for the same reason: the handle is immutable), so the resolve
is one document read, no per-entry account read.

**A hard block overrides everything** (the block docstring says so outright): the
explicit list is block-filtered here, and mode `all`/`friends` are gated
client-side by the requester's profile resolving to `None` when a block exists in
either direction. There is no path by which a blocked account is auto-approved.

Document shape (`mp_rendezvous/{account_id}`):

    {
      "mode": "manual",                                  # one of MODES
      "auto": { "<other_id>": {"at": <epoch>,
                               "handle": "...", "name": "..."} },
      "updated": <epoch>,
    }

Reads **fail closed**, the same posture as `blocks.py`: a Firestore blip yields
`RendezvousPolicyUnavailable`, and the join endpoint turns that into "prompt for
everything" (mode `manual`, empty list) rather than a policy that might
auto-approve. Failing closed here means *asking*, which is always the safe
direction. Writes are a single document's transactional read-modify-write, for
lost-update safety (there is no cross-document invariant — the policy is one
document).
"""

import logging
import time

from firebase_admin import firestore

from data.store import _db
from data import blocks as blocks_db

log = logging.getLogger(__name__)

MANUAL = "manual"
FRIENDS = "friends"
ALL = "all"

MODES = (MANUAL, FRIENDS, ALL)
DEFAULT_MODE = MANUAL

# Bounds the owner's own document — the whole record is read at join and rewritten
# on every change, against Firestore's 1 MiB limit. Generous: a real always-approve
# list is a handful of people.
MAX_AUTO = 1000


class RendezvousPolicyUnavailable(Exception):
    """The policy could not be read. Callers fail closed by prompting, never by
    auto-approving."""


def _col():
    return _db.collection("mp_rendezvous")


def _now() -> float:
    return time.time()


def normalize_mode(mode) -> str | None:
    """The stored form of a mode, or None if it is not one of the three."""
    m = str(mode or "").strip().lower()
    return m if m in MODES else None


def _norm(record: dict | None) -> dict:
    """A stored record with `mode` valid and `auto` a dict, whatever the document.

    The document is created lazily (the first policy edit a player ever makes), so
    every read has to cope with an absent or half-shaped one. A stored mode that is
    somehow not one of the three reads back as the default rather than being
    trusted — a malformed setting must never read as `all`.
    """
    d = dict(record or {})
    d["mode"] = normalize_mode(d.get("mode")) or DEFAULT_MODE
    auto = d.get("auto")
    d["auto"] = dict(auto) if isinstance(auto, dict) else {}
    try:
        d["updated"] = float(d.get("updated") or 0.0)
    except (TypeError, ValueError):
        d["updated"] = 0.0
    return d


# ── Reads (fail closed) ───────────────────────────────────────────────────────

def get_record(account_id) -> dict:
    """One player's raw policy record. Raises `RendezvousPolicyUnavailable` on a
    failed read — never answers "manual" to a Firestore error, so a caller can tell
    "no policy set" (an absent document, the default) from "couldn't read"."""
    aid = str(account_id)
    try:
        snap = _col().document(aid).get()
    except Exception as exc:
        log.warning("Rendezvous policy read failed for %s: %s", aid, exc)
        raise RendezvousPolicyUnavailable(str(exc)) from exc
    return _norm(snap.to_dict() if snap.exists else None)


def resolve(account_id) -> dict:
    """The client-facing policy for one player, resolved for the join fetch.

    Returns ``{"mode": <str>, "auto_handles": [<handle>, ...], "updated": <float>}``.
    The explicit list is rendered to immutable handles and **block-filtered** (a
    hard block on an account drops it from auto-approval, matching
    `mp_allow_set.compute`'s ``allow_accounts - block_accounts``). No account ids
    leave here — a peer is referenced only by handle, exactly as the profile and
    allow-set endpoints keep it. Raises `RendezvousPolicyUnavailable` if the policy
    or the block list cannot be read, so the endpoint fails closed to `manual`.
    """
    aid = str(account_id)
    rec = get_record(aid)
    try:
        blocked = blocks_db.blocked_ids(aid)
    except blocks_db.BlocksUnavailable as exc:
        raise RendezvousPolicyUnavailable(f"blocks unavailable: {exc}") from exc

    handles: list[str] = []
    seen: set[str] = set()
    for other_id, entry in rec["auto"].items():
        if other_id in blocked:
            continue
        h = str((entry or {}).get("handle") or "").strip()
        if not h:
            continue
        key = h.lower()
        if key in seen:
            continue
        seen.add(key)
        handles.append(h)

    return {"mode": rec["mode"], "auto_handles": handles, "updated": rec["updated"]}


# ── Writes ────────────────────────────────────────────────────────────────────

def _doc(rec: dict, now: float) -> dict:
    # A whole-document `set` (no merge): merge would deep-merge `auto`, so a key
    # removed in memory would survive the write and a removal would do nothing. The
    # record was just read in this transaction, so replacing it wholesale is the
    # only shape that expresses a deletion, and it creates the document lazily.
    return {"mode": rec["mode"], "auto": rec["auto"], "updated": now}


def _mutate(account_id, apply) -> tuple[bool, str]:
    """Read-modify-write one player's document in a transaction. `apply(rec)` edits
    the normalised record in place and returns `(ok, message)`; the document is
    written only when `ok` is true, so a no-op refusal costs no write."""
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
        log.warning("Rendezvous policy mutation for %s failed: %s", aid, exc)
        raise RendezvousPolicyUnavailable(str(exc)) from exc


def set_mode(account_id, mode) -> tuple[bool, str]:
    """Set this account's auto-approval mode. Returns (ok, message)."""
    m = normalize_mode(mode)
    if m is None:
        return False, "That is not a valid auto-approval setting."

    def _apply(rec) -> tuple[bool, str]:
        if rec["mode"] == m:
            return False, "That is already your setting."
        rec["mode"] = m
        return True, "Saved."

    return _mutate(account_id, _apply)


def _entry(handle: str, name: str) -> dict:
    """A stored allow-list entry. The handle is denormalised in so `resolve` renders
    it from one document read (same trade as `blocks._entry`, safe because the
    handle is immutable); the display name is a best-effort copy that may go
    stale."""
    e = {"at": _now()}
    if handle:
        e["handle"] = str(handle)
    if name:
        e["name"] = str(name)
    return e


def allow(from_id, to_id, *, handle: str = "", name: str = "") -> tuple[bool, str]:
    """`from_id` always auto-approves `to_id`. Idempotent; refuses self and a full
    list."""
    a, b = str(from_id), str(to_id)
    if a == b:
        return False, "You can't add yourself."

    def _apply(rec) -> tuple[bool, str]:
        if b in rec["auto"]:
            return False, "They're already on your auto-approve list."
        if len(rec["auto"]) >= MAX_AUTO:
            return False, f"Your auto-approve list is full ({MAX_AUTO})."
        rec["auto"][b] = _entry(handle, name)
        return True, "Added."

    return _mutate(a, _apply)


def disallow(from_id, to_id) -> tuple[bool, str]:
    """`from_id` removes `to_id` from its always-auto-approve list."""
    a, b = str(from_id), str(to_id)

    def _apply(rec) -> tuple[bool, str]:
        if b not in rec["auto"]:
            return False, "They aren't on your auto-approve list."
        rec["auto"].pop(b, None)
        return True, "Removed."

    return _mutate(a, _apply)


def forget_account(account_id) -> None:
    """Delete this account's policy document — the data-purge path. Best-effort.

    Only the author's own document is removed; an entry naming this account on
    *another* player's list is that player's record to keep or clear (the same
    posture as `blocks.forget_account`)."""
    aid = str(account_id)
    try:
        _col().document(aid).delete()
    except Exception as exc:                       # pragma: no cover - defensive
        log.warning("Rendezvous policy purge: could not delete %s: %s", aid, exc)
