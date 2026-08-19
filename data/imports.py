"""
data/imports.py – Per-user "craft import" queue.

When a player selects a craft for import in Discord (a completed bot-contract
craft via /library, or a craft they bought on the marketplace), an entry is
written here under that player's account. The KSP mod polls the pending queue
(see api_server.py /api/v1/craft/imports/...) and auto-imports each craft into
the active save, then acks it so the entry is deleted.

Storage layout: guilds/{gid}/ksp_craft_imports/{uid}/items/{import_id}
"""
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from data.store import _db, _storage_bucket, safe_filename, upload_private

log = logging.getLogger(__name__)

ImportEntry = dict[str, Any]


def upload_gift(import_id: str, filename: str, data: bytes) -> str:
    """Upload a quicksent craft/vessel payload to Storage. Returns its bucket PATH
    (not a public URL) — the payload is a PRIVATE object, served to the recipient
    only through a signed URL minted when they poll the gift/import queue (see
    _sign_import_entry in api_server). A quicksent craft is a private hand-off
    between two players, so it must not be world-readable by its URL.
    """
    if _storage_bucket is None:
        raise RuntimeError("Firebase Storage not configured")
    path = f"gifts/{import_id}/{safe_filename(filename, 'craft.craft')}"
    return upload_private(path, data, content_type="text/plain")


def upload_gift_blueprint(import_id: str, data: bytes) -> str:
    """Upload a gift's rendered blueprint PNG — the preview the recipient sees
    before deciding to accept. Returns its public URL."""
    if _storage_bucket is None:
        raise RuntimeError("Firebase Storage not configured")
    path = f"gifts/{import_id}/blueprint.png"
    blob = _storage_bucket.blob(path)
    blob.upload_from_string(data, content_type="image/png")
    blob.make_public()
    return blob.public_url


def delete_gift_files(import_id: str) -> None:
    """Best-effort removal of a gift's Storage files (craft + blueprint) once the
    recipient has declined it — nothing will ever download them again."""
    if _storage_bucket is None:
        return
    try:
        for blob in _storage_bucket.list_blobs(prefix=f"gifts/{import_id}/"):
            blob.delete()
    except Exception as exc:
        log.warning("Could not delete gift files for %s: %s", import_id, exc)


def _col(guild_id: int, user_id: int):
    return (_db.collection("guilds").document(str(guild_id))
            .collection("ksp_craft_imports").document(str(user_id))
            .collection("items"))


def enqueue(
    guild_id: int, user_id: int, source: str, ref_id: str, craft_name: str,
    vessel_node_url: str | None = None,
    craft_url: str | None = None,
    craft_filename: str | None = None,
    loadmeta: str | None = None,
    owner_name: str | None = None,
    flag_url: str | None = None,
    blueprint_url: str | None = None,
    sender_id: int | None = None,
    status: str = "queued",
) -> ImportEntry:
    """Queue a craft for the player's KSP client to auto-import.

    `source` is "contract", "market", "rescue_delivery", or "flag"; `ref_id` is
    the contract_id or listing_id. "contract"/"market" deliver a .craft blueprint
    (installed to the Ships folder); "rescue_delivery" carries a vessel_node_url
    and is imported as a LIVE vessel (the rescued craft, spawned in-save); "flag"
    carries a flag_url (PNG) installed into the KSP Flags dir — never a
    craft/vessel. If an identical entry is already queued (same source + ref_id)
    the existing entry is returned instead of creating a duplicate.

    `status` is "queued" (the client auto-imports it) or "offered" (a friend
    quicksend awaiting the recipient's accept/decline — invisible to the
    auto-import poll until accepted). Offers carry the sender's `sender_id` so a
    decline can notify them, and a `blueprint_url` preview when the sender's
    client managed to render one.
    """
    for doc in _col(guild_id, user_id).stream():
        d = doc.to_dict()
        if d.get("source") == source and d.get("ref_id") == ref_id:
            return d

    iid = uuid.uuid4().hex[:12]
    entry: ImportEntry = {
        "import_id": iid,
        "source": source,
        "ref_id": ref_id,
        "craft_name": craft_name,
        "vessel_node_url": vessel_node_url,
        "craft_url": craft_url,
        "craft_filename": craft_filename,
        "loadmeta": loadmeta,
        "owner_name": owner_name,
        "flag_url": flag_url,
        "blueprint_url": blueprint_url,
        "sender_id": sender_id,
        "status": status,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _col(guild_id, user_id).document(iid).set(entry)
    log.info("Queued craft import %s (%s:%s, %s) for user %d", iid, source, ref_id, status, user_id)
    return entry


def list_pending(guild_id: int, user_id: int) -> list[ImportEntry]:
    return [doc.to_dict() for doc in _col(guild_id, user_id).stream()]


def get(guild_id: int, user_id: int, import_id: str) -> ImportEntry | None:
    doc = _col(guild_id, user_id).document(import_id).get()
    return doc.to_dict() if doc.exists else None


def set_status(guild_id: int, user_id: int, import_id: str, status: str) -> bool:
    ref = _col(guild_id, user_id).document(import_id)
    if not ref.get().exists:
        return False
    ref.update({"status": status})
    return True


def delete(guild_id: int, user_id: int, import_id: str) -> bool:
    ref = _col(guild_id, user_id).document(import_id)
    if not ref.get().exists:
        return False
    ref.delete()
    return True
