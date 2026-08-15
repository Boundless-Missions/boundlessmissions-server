"""
data/contracts.py – Firestore + Firebase Storage helpers for contracts.
"""
import logging
import uuid
from datetime import datetime
from typing import Any

import aiohttp

from data.store import _db, _storage_bucket, safe_filename, safe_content_type

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


def iter_user_contracts(guild_id: int, user_id: int) -> list[ContractData]:
    """All contracts where the user is issuer or contractor, deduped by id.

    Uses two single-field-equality queries (each served by Firestore's automatic
    single-field index — no composite index required) instead of streaming every
    contract in the guild and OR-filtering in Python. The returned set is
    identical to the old `where("status","in",...).stream()` + Python filter,
    minus the status filter, which callers apply in-memory.
    """
    uid = str(user_id)
    col = _col(guild_id)
    by_id: dict[str, ContractData] = {}
    for field in ("contractor_id", "issuer_id"):
        for doc in col.where(field, "==", uid).stream():
            by_id[doc.id] = doc.to_dict()
    return list(by_id.values())


def list_by_status(status: str) -> list[ContractData]:
    """Every contract in a given status, across all guilds.

    Served by Firestore's automatic single-field index, so this is a bounded query
    rather than a full-collection scan — which matters because the dispute-timeout
    sweep runs on a timer forever. Contracts are global (see _col), hence no guild_id.
    """
    return [doc.to_dict() for doc in _col().where("status", "==", status).stream()]


def count_active(guild_id: int, user_id: int) -> int:
    active_statuses = {PENDING, ACTIVE, SUBMITTED, DISPUTED, MOD_REVIEW}
    return sum(
        1 for c in iter_user_contracts(guild_id, user_id)
        if c.get("status") in active_statuses
    )


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


async def download_url(url: str) -> bytes:
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r:
            return await r.read()
