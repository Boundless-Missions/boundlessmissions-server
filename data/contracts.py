"""
data/contracts.py – Firestore + Firebase Storage helpers for contracts.

Firestore structure:
    contracts/{contract_id}          → { ...contract fields... }
    contract_reports/{contract_id}_{reporter_id} → { ...one report... }
"""
import logging
import uuid
from datetime import datetime
from typing import Any

import aiohttp
from firebase_admin import firestore

from data.store import (
    _db, _storage_bucket, safe_filename, safe_content_type,
    upload_private, signed_url, sign_stored, is_storage_path,
    SIGNED_URL_MAX_TTL, display_filename, md_filename,
)

log = logging.getLogger(__name__)

# Status constants
PENDING = "pending"
ACTIVE = "active"
SUBMITTED = "submitted"
COMPLETED = "completed"
DISPUTED = "disputed"
MOD_REVIEW = "mod_review"
CANCELLED = "cancelled"

# Mission-type values (stored in the contract's "mission_type" field)
CRAFT_BUILD = "craft_build"
ACTIVE_VESSEL = "active_vessel"
# A network mission ("deploy a relay network around the Mun"). Submits from flight
# like ACTIVE_VESSEL and is gated on the same body/situation, but is judged on a
# scan of every vessel in the target body's sphere of influence — see
# data/fleet_constraints.py.
CONSTELLATION = "constellation"
RESCUE = "rescue"
FLAG_DESIGN = "flag_design"

ContractData = dict[str, Any]


def _col(guild_id: int = 0):
    """The single GLOBAL contracts collection — a contract can run between users in
    different servers. guild_id is accepted for call-site compatibility but is only
    stored on the doc as the origin guild (used for channel routing)."""
    return _db.collection("contracts")


def create_contract(
    guild_id: int, issuer_id: int, issuer_name: str,
    contractor_id: int, contractor_name: str,
    mission: str, payment: int, fine: int, due_date: str,
    modlist: str | None = None,
    *,
    mission_type: str | None = None,
    rescue_target: dict | None = None,
    rescue_vessel_node_url: str | None = None,
    rescue_kerbals: list | None = None,
    rescue_pid: str | None = None,
    life_support: str | None = None,
    ls_endurance_days: float = 0.0,
    ls_crew_capacity: int = 0,
    constraints: dict | None = None,
) -> ContractData:
    cid = uuid.uuid4().hex[:12]
    now = datetime.utcnow().isoformat()
    doc: ContractData = {
        "contract_id": cid,
        "guild_id": str(guild_id),
        "issuer_id": str(issuer_id),
        "issuer_name": issuer_name,
        "contractor_id": str(contractor_id),
        "contractor_name": contractor_name,
        "mission": mission,
        "payment": payment,
        "fine": fine,
        "due_date": due_date,
        "status": PENDING,
        "created_at": now,
        "submitted_at": None,
        "completed_at": None,
        "submitted_files": [],
        "dm_message_id": None,
        "issuer_review_msg_id": None,
        "modlist": modlist,
    }
    # Rescue-mission fields. The issuer's snapshotted vessel (the wreck the
    # rescuer recovers) is removed from the issuer's save at creation time and
    # restored if the contract never completes. rescue_kerbals are the tagged
    # names ("{issuer}'s {kerbal}") the rescuer must recover.
    if mission_type:
        doc["mission_type"] = mission_type
    if mission_type == RESCUE:
        doc["rescue_target"] = rescue_target
        doc["rescue_vessel_node_url"] = rescue_vessel_node_url
        doc["rescue_kerbals"] = rescue_kerbals or []
        doc["rescue_pid"] = rescue_pid
        doc["issuer_vessel_removed"] = True
        doc["delivered_vessel_node_url"] = None
    # Part/crew limits decided at creation. Stored — even when empty — so every
    # later render reads them off the document instead of re-deriving them from
    # the mission text: a contract is drawn many more times than it is written
    # (offer, dispute, review, ticket), each time on the bot's event loop.
    if constraints is not None:
        doc["constraints"] = constraints
    # Life-support provisioning of the craft this contract is about. On a rescue it is
    # known at creation (the wreck already exists and was scanned); on a normal contract
    # it arrives with the submitted craft instead, and is written then.
    if life_support and life_support.lower() != "none":
        doc["life_support"] = life_support.lower()
        doc["ls_endurance_days"] = float(ls_endurance_days or 0.0)
        doc["ls_crew_capacity"] = int(ls_crew_capacity or 0)
    _col(guild_id).document(cid).set(doc)
    log.info("Contract %s created: %s -> %s (%d coins)", cid, issuer_name, contractor_name, payment)
    return doc


def get_contract(guild_id: int, contract_id: str) -> ContractData | None:
    snap = _col(guild_id).document(contract_id).get()
    return snap.to_dict() if snap.exists else None


def update_contract(guild_id: int, contract_id: str, **fields) -> None:
    _col(guild_id).document(contract_id).update(fields)


def claim_submission(guild_id: int, contract_id: str, fields: dict[str, Any]) -> bool:
    """Atomically move a contract ACTIVE -> SUBMITTED, writing `fields` with it.

    Returns True only for the call that did the flip. `submit_contract` awaits real
    I/O (file reads, the ban check, Storage uploads) between reading `status ==
    ACTIVE` and writing SUBMITTED, and the transitions in `contract_actions`
    (cancel, give_up, ...) can run in that window from another request. A plain
    `update()` would then clobber a CANCELLED contract — whose escrow was already
    refunded — back to SUBMITTED, and the review that followed would pay the
    contractor from nothing. Deciding the flip inside a Firestore transaction
    makes the status check and the write one step, which holds even with no
    shared in-process lock and across workers (the `try_claim_purchase` pattern).
    """
    ref = _col(guild_id).document(contract_id)
    transaction = _db.transaction()
    fields = dict(fields)
    fields["status"] = SUBMITTED

    @firestore.transactional
    def _claim(txn) -> bool:
        snap = ref.get(transaction=txn)
        if not snap.exists or (snap.to_dict() or {}).get("status") != ACTIVE:
            return False
        txn.update(ref, fields)
        return True

    return _claim(transaction)


def delete_stored_file(path: str) -> bool:
    """Best-effort delete of a Storage object stored on a contract by bucket path
    (the shape `upload_private_to_storage` returns). False if nothing was deleted."""
    if _storage_bucket is None or not path or "://" in str(path):
        return False
    try:
        _storage_bucket.blob(str(path)).delete()
        return True
    except Exception as exc:
        log.warning("Could not delete stored file %s: %s", path, exc)
        return False


# The non-terminal statuses an escrow figure has to look at: every contract in
# one of these still has the issuer's payment locked up (see api_server's
# `_escrow_held`). Named separately from ACTIVE_STATUSES even though the members
# are the same today, because the two answer different questions — "is this
# contract still running" and "is this money still held" — and a status that
# stops counting against the activity cap need not stop holding coins.
ESCROW_STATUSES: set[str] = {PENDING, ACTIVE, SUBMITTED, DISPUTED, MOD_REVIEW}


def iter_user_contracts(guild_id: int, user_id: int, *,
                        statuses: set[str] | None = None,
                        limit: int | None = None,
                        roles: tuple[str, ...] = ("contractor_id", "issuer_id"),
                        ) -> list[ContractData]:
    """All contracts where the user is issuer or contractor, deduped by id.

    Uses two single-field-equality queries (each served by Firestore's automatic
    single-field index — no composite index required) instead of streaming every
    contract in the guild and OR-filtering in Python. The returned set is
    identical to the old `where("status","in",...).stream()` + Python filter,
    minus the status filter, which callers apply in-memory.

    Both keyword arguments default to the old behaviour — every contract the
    account has ever been party to — because the history view genuinely wants
    that. They exist for the callers that do not: a contract history only grows
    (a completed or cancelled contract is kept), so an unfiltered read is one
    metered Firestore read per contract the account has EVER had, on every call,
    and an endpoint that makes one per request turns a few thousand cheap
    create→cancel cycles into a walk up the shared budget to `cost_guard`'s
    FROZEN — which stops the bot for everybody, not for the caller. This is the
    same fix `count_active` already carries, and for the same reason.

      * `statuses` filters in the *query* (`status in [...]`), so the documents
        never leave Firestore and are never billed.
      * `limit` caps each of the two queries, so the read is bounded at 2×limit
        documents and the returned list at `limit`. It is a cost ceiling, not a
        page: there is no cursor, and a caller that hits it is seeing an
        arbitrary subset. Only pass it where a partial answer is better than an
        unbounded read (a summary line), never where it would silently change a
        total the player is shown as exact.
      * `roles` picks which side(s) to query. It exists because `limit` applies to
        EACH query and the merge then truncates to `limit` overall, keeping the
        first side's rows — so a caller that only cares about one side was paying
        its whole budget for rows it discards. `_escrow_held` wants issuer rows
        only; asking for both meant a player with a few hundred PENDING offers
        *made to them* (deliberately uncapped) filled the 500 with contractor rows
        and had their escrow reported low, or as zero.
    """
    uid = str(user_id)
    col = _col(guild_id)
    want = set(statuses) if statuses else None
    by_id: dict[str, ContractData] = {}
    for field in roles:
        q = col.where(field, "==", uid)
        try:
            if want:
                # A disjunction of equalities, exactly as count_active does it —
                # Firestore merges single-field indexes for this and needs no
                # composite one. Sorted for a stable query shape.
                q = q.where("status", "in", sorted(want))
            if limit is not None and limit > 0:
                q = q.limit(int(limit))
            docs = list(q.stream())
        except Exception as exc:
            # A deployment that refuses the `in` clause must still get an answer:
            # fall back to the plain single-field query and filter in Python. The
            # limit is kept, so the fallback is still bounded — it is the cost
            # ceiling that must never be the thing that gets dropped.
            log.warning("iter_user_contracts fell back to an unfiltered query for "
                        "%s (%s): %s", uid, field, exc)
            q = col.where(field, "==", uid)
            if limit is not None and limit > 0:
                q = q.limit(int(limit))
            docs = [d for d in q.stream()
                    if not want or (d.to_dict() or {}).get("status") in want]
        for doc in docs:
            by_id[doc.id] = doc.to_dict()
    rows = list(by_id.values())
    if limit is not None and limit > 0:
        del rows[int(limit):]
    return rows


def list_by_status(status: str) -> list[ContractData]:
    """Every contract in a given status, across all guilds.

    Served by Firestore's automatic single-field index, so this is a bounded query
    rather than a full-collection scan — which matters because the dispute-timeout
    sweep runs on a timer forever. Contracts are global (see _col), hence no guild_id.
    """
    return [doc.to_dict() for doc in _col().where("status", "==", status).stream()]


ACTIVE_STATUSES = (PENDING, ACTIVE, SUBMITTED, DISPUTED, MOD_REVIEW)


def list_by_issuer(issuer_id: str) -> list[ContractData]:
    """Every contract issued by one id, in any status.

    Single-field equality, so Firestore's automatic index serves it and no
    composite index is needed. Written for the `issuer_id == "0"` repair (see
    `cogs.contracts.repair_bot_issuer`), which is why it does not filter status:
    the repair has to see the terminal ones to report them, even though it only
    rewrites the rest."""
    return [doc.to_dict() for doc in _col().where("issuer_id", "==", str(issuer_id)).stream()]


def count_active(guild_id: int, user_id: int) -> int:
    """How many non-terminal contracts this account is a party to.

    Filtered in the *query* rather than in Python. It used to call
    `iter_user_contracts`, which streams a user's whole history — and a history
    only grows, since a cancelled or completed contract is kept. Every create paid
    that cost (the cap is checked before writing), so the read cost of the Nth
    contract was O(N) and of accumulating N was O(N²): a few thousand
    create→cancel cycles were enough to walk the shared Firestore budget up to the
    cost guard's FROZEN level, which stops the whole bot.

    Both filters are equalities (`in` is a disjunction of them), so Firestore
    serves this by merging single-field indexes and no composite index is needed.
    If a deployment nonetheless refuses it, fall back to the old full read rather
    than fail a contract action — the cap is worth more than the saving.
    """
    uid = str(user_id)
    col = _col(guild_id)
    # A PENDING contract is an *offer*. For the issuer it is a real obligation —
    # their payment is escrowed — but for the person it was offered TO it is
    # something a stranger did to them, and it used to fill their allowance:
    # anyone could spend ten coins offering a victim ten contracts and lock them
    # out of weekly missions, auction bids and issuing work of their own, with the
    # auction close-time re-check even cancelling a win they had already made. So
    # the offeree's side counts only what they actually accepted.
    per_field = {
        "issuer_id": list(ACTIVE_STATUSES),
        "contractor_id": [s for s in ACTIVE_STATUSES if s != PENDING],
    }
    try:
        by_id: set[str] = set()
        for field, statuses in per_field.items():
            for doc in (col.where(field, "==", uid)
                           .where("status", "in", statuses).stream()):
                by_id.add(doc.id)
        return len(by_id)
    except Exception as exc:
        log.warning("count_active fell back to a full history read for %s: %s", uid, exc)
        return sum(1 for c in iter_user_contracts(guild_id, user_id)
                   if c.get("status") in set(ACTIVE_STATUSES)
                   and not (c.get("status") == PENDING
                            and str(c.get("contractor_id")) == uid))


# ── Reports ──────────────────────────────────────────────────────────────────
#
# A contract report is the marketplace's report system pointed at the other side of
# a deal (see data/marketplace.py). The shape is deliberately identical — a keyed
# (subject, reporter) document plus a counter on the subject — because the question
# a moderator asks is the same one: has this person been reported before, and by how
# many different people?
#
# What differs is who may file one. A listing is public and anyone browsing can
# report it; a contract is private to its two parties, so the only people who can
# see one to complain about it are the issuer and the contractor. The endpoint
# enforces that; this module only stores what it is given.


def _report_id(contract_id: str, reporter_id: int | str) -> str:
    return f"{contract_id}_{reporter_id}"


def get_report(contract_id: str, reporter_id: int | str) -> dict[str, Any] | None:
    """This user's existing report against this contract, if any.

    The document id is the (contract, reporter) pair, so "have I already reported
    this?" is one keyed read — no composite index, and no way to file the same
    complaint twice to make it look louder."""
    snap = _db.collection("contract_reports").document(
        _report_id(contract_id, reporter_id)).get()
    return snap.to_dict() if snap.exists else None


def record_report(contract: ContractData, reporter_id: int | str, reporter_name: str,
                  reason: str, guild_id: int | str = "",
                  ticket_channel_id: int | str = "") -> None:
    """Store a report and bump the contract's report_count.

    The Discord ticket is where a report is actually *handled*; this record exists so
    the count survives the ticket being closed, and so a second report from the same
    user overwrites rather than accumulates.

    The contract's status at the time is stored with it, because a report is about a
    moment — "they refused after I delivered" reads very differently once the same
    contract has been settled — and the live document will have moved on by the time
    a moderator opens the ticket.
    """
    contract_id = contract["contract_id"]
    reporter = str(reporter_id)
    issuer_id = str(contract.get("issuer_id", ""))
    # Whoever the reporter is not. A report is always *about* the counterparty, so
    # storing them saves every reader re-deriving it from two ids and a role.
    subject_id = (str(contract.get("contractor_id", "")) if reporter == issuer_id
                  else issuer_id)
    subject_name = (contract.get("contractor_name", "") if reporter == issuer_id
                    else contract.get("issuer_name", ""))
    first_time = get_report(contract_id, reporter_id) is None
    _db.collection("contract_reports").document(_report_id(contract_id, reporter_id)).set({
        "contract_id": contract_id,
        "mission": (contract.get("mission", "") or "")[:500],
        "status": contract.get("status", ""),
        "issuer_id": issuer_id,
        "issuer_name": contract.get("issuer_name", ""),
        "contractor_id": str(contract.get("contractor_id", "")),
        "contractor_name": contract.get("contractor_name", ""),
        "subject_id": subject_id,
        "subject_name": subject_name,
        "reporter_id": reporter,
        "reporter_name": reporter_name,
        "reason": reason,
        "guild_id": str(guild_id),
        "ticket_channel_id": str(ticket_channel_id),
        "created_at": datetime.utcnow().isoformat(),
    })
    if first_time:
        try:
            _col().document(contract_id).update({"report_count": firestore.Increment(1)})
        except Exception as exc:  # a vanished contract must not lose the report
            log.warning("Could not bump report_count for contract %s: %s", contract_id, exc)
    log.info("Contract %s reported by %s (%s)", contract_id, reporter_name, reporter_id)


async def upload_to_storage(contract_id: str, filename: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    """Upload a file to Firebase Storage under contracts/{contract_id}/. Returns public URL."""
    if _storage_bucket is None:
        raise RuntimeError("Firebase Storage not configured")
    # Sanitize the client-supplied filename + content type before they reach the
    # public object path (no prefix escape / sibling shadowing / active-content).
    path = f"contracts/{contract_id}/{safe_filename(filename, 'file')}"
    blob = _storage_bucket.blob(path)
    blob.upload_from_string(data, content_type=safe_content_type(content_type))
    blob.make_public()
    log.info("Uploaded %s to Storage", path)
    return blob.public_url


async def upload_private_to_storage(contract_id: str, filename: str, data: bytes,
                                    content_type: str = "application/octet-stream") -> str:
    """Upload a file under contracts/{contract_id}/ as a PRIVATE object and return
    its bucket path (not a URL). Same path convention and sanitization as
    upload_to_storage, but never made public — the craft/vessel file "private to the
    two parties" is served only through a signed URL minted at request time. Callers
    store the returned path on the contract; serve points run it through
    store.sign_stored()."""
    if _storage_bucket is None:
        raise RuntimeError("Firebase Storage not configured")
    path = f"contracts/{contract_id}/{safe_filename(filename, 'file')}"
    return upload_private(path, data, content_type)


async def upload_submission_file(contract_id: str, party_id: str, filename: str,
                                 data: bytes, content_type: str = "application/octet-stream",
                                 public: bool = False,
                                 path_out: list[str] | None = None) -> str:
    """Store a file a *client* named — a submitted craft, a screenshot, an
    uploaded flag — under a slot the server chooses.

    `contracts/{cid}/` is shared by both parties and by the server's own objects
    (`rescue_vessel.cfg`, the issuer's stored wreck; `vessel_node.cfg`; the orbit
    diagrams), and `safe_filename` keeps any plain basename — so a rescuer whose
    "screenshot" was called `rescue_vessel.cfg` used to replace the wreck that
    `_restore_issuer_vessel` later hands back to the issuer, and make it public
    on the way. The path here is `contracts/{cid}/submitted/{party}/{uuid}_{name}`:
    the party segment keeps the two sides apart, the uuid keeps one party's files
    apart from each other, and `submitted/` keeps all of it out of the server's
    namespace. `if_generation_match=0` is the belt to those braces — it makes the
    write itself refuse to replace an object that exists, whatever the path.

    Returns the public URL for `public=True` (screenshots, shown in embeds and the
    web review), else the bare bucket path of a private object (crafts, flags),
    which readers resolve through `sign_stored`."""
    if _storage_bucket is None:
        raise RuntimeError("Firebase Storage not configured")
    party = safe_filename(str(party_id), "party")
    path = (f"contracts/{contract_id}/submitted/{party}/"
            f"{uuid.uuid4().hex[:8]}_{safe_filename(filename, 'file')}")
    if path_out is not None:
        # The caller needs the PATH to be able to delete this again: a public object
        # is returned as a URL, and `delete_stored_file` refuses anything carrying a
        # scheme, so the return value of a public upload cannot remove it. The uuid
        # in the path is minted here, so only this function can hand it back.
        path_out.append(path)
    blob = _storage_bucket.blob(path)
    blob.upload_from_string(data, content_type=safe_content_type(content_type),
                            if_generation_match=0)
    if not public:
        log.info("Uploaded private submission object %s (%d bytes)", path, len(data) if data else 0)
        return path
    blob.make_public()
    log.info("Uploaded %s to Storage", path)
    return blob.public_url


def file_link(f: dict, icon: str = "📎", url: str | None = None) -> str:
    """One `icon [name](url)` line for a stored submission file in a Discord
    embed. The name goes through `md_filename` so it can neither close the link
    text nor run past an embed field: entries written before the display name
    was sanitised at upload still carry the client's raw string."""
    return f"{icon} [{md_filename(f.get('filename') or '')}]({url or f.get('url') or ''})"


async def download_url(url: str) -> bytes:
    # A stored reference may be a bare bucket path (a private object) rather than a
    # URL — resolve it to a signed URL first, so every internal reader works
    # regardless of which storage scheme produced the reference.
    if is_storage_path(url):
        url = signed_url(url)
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r:
            return await r.read()
