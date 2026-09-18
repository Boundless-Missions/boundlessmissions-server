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
from datetime import datetime, timedelta, timezone
from typing import Any

from firebase_admin import firestore

from config import cfg
from data.store import (_db, _storage_bucket, safe_filename, upload_private,
                        looks_like_image, safe_content_type)

log = logging.getLogger(__name__)

ImportEntry = dict[str, Any]


def queue_guild(guild_id: int | str, user_id: int | str) -> str:
    """The guild whose queue `user_id` actually polls.

    The queue is keyed by guild, and every caller used to pass the guild of the
    *writer's* token — the sender of a quicksend, the contract's guild for a
    rescue delivery or return — while the recipient reads under the guild of
    *their* token. For a Discord-linked player those agree. For a website-only
    account (an `a_…` id) they need not: it is deliberately listed in every
    guild's player picker, but its token guild is always `HOME_GUILD_ID` (see
    `_account_guild_id` in api_server), so an offer written from any other guild
    landed where nobody would ever look — and for a live vessel the ship had
    already left the sender's save. Resolved here, once, so every writer and
    every reader of the queue agrees without each having to know.

    `HOME_GUILD_ID` unset gives "0", which is not a real guild — the same choice
    `_account_guild_id` makes, so the two sides still meet."""
    uid = str(user_id)
    if uid.startswith("a_"):
        return str(cfg.HOME_GUILD_ID or 0)
    return str(guild_id)


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


def upload_gift_blueprint(import_id: str, data: bytes) -> str | None:
    """Upload a gift's rendered blueprint PNG — the preview the recipient sees
    before deciding to accept. Returns its public URL, or None if the bytes are
    not an image.

    This object is made **public**, and that is the whole reason for the check.
    The marketplace's two public uploaders were fixed to sniff and clamp; this one
    — the quicksend half of the same finding — was not, so `POST /craft/send` with
    an arbitrary `blueprint` stored world-readable attacker-chosen bytes on the
    project's bucket, at a permanent URL (a gift's files are deleted on decline or
    on the ack of the import that consumes it, so an offer nobody answers keeps
    its blueprint forever). The hardcoded `image/png` closed the XSS half; nothing
    closed the file-hosting half.

    Refusing by returning None rather than raising is deliberate: the blueprint is
    the *preview* of a send, not the payload, and a craft that arrives without a
    picture is a far better outcome than a hand-over that 500s. The caller drops
    the field. The API layer still owns the byte cap and the bounded Pillow decode
    (`MAX_BLUEPRINT_BYTES`, `_looks_like_image`) — that is the primary gate and
    this is the last line before `make_public`.
    """
    if _storage_bucket is None:
        raise RuntimeError("Firebase Storage not configured")
    if not looks_like_image(data):
        log.warning("Refusing to publish a non-image gift blueprint for %s (%d bytes)",
                    import_id, len(data or b""))
        return None
    path = f"gifts/{import_id}/blueprint.png"
    blob = _storage_bucket.blob(path)
    blob.upload_from_string(data, content_type=safe_content_type("image/png"))
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
    owner_id: str | None = None,
    flag_url: str | None = None,
    blueprint_url: str | None = None,
    sender_id: int | None = None,
    status: str = "queued",
    vessel_pid: str | None = None,
    homebound: list[str] | None = None,
) -> ImportEntry:
    """Queue a craft for the player's KSP client to auto-import.

    `source` is "contract", "market", "rescue_delivery", "network_delivery", or
    "flag"; `ref_id` is
    the contract_id or listing_id. "contract"/"market" deliver a .craft blueprint
    (installed to the Ships folder); "rescue_delivery" carries a vessel_node_url
    and is imported as a LIVE vessel (the rescued craft, spawned in-save);
    "network_delivery" is the same live-vessel import for a relay network bought
    through a constellation contract, kept separate because rescue_delivery also
    carries rescue semantics on the client that a bought satellite has none of; "flag"
    carries a flag_url (PNG) installed into the KSP Flags dir — never a
    craft/vessel. If an identical entry is already queued (same source + ref_id)
    the existing entry is returned instead of creating a duplicate.

    `status` is "queued" (the client auto-imports it) or "offered" (a friend
    quicksend awaiting the recipient's accept/decline — invisible to the
    auto-import poll until accepted). Offers carry the sender's `sender_id` so a
    decline can notify them, and a `blueprint_url` preview when the sender's
    client managed to render one.

    `vessel_pid` (gift_vessel and the rescue-cancel restore) is the vessel's pid
    in the SENDER's save. A quicksent live vessel is a hand-over — the sender's
    client removes it on send — so the pid rides both the offer (the accept
    notification echoes it, letting the sender's client re-queue a removal a
    quickload rolled back) and the decline-return entry (the sender's client
    uses it to cancel a removal that hasn't run yet instead of spawning a
    duplicate). A cancelled rescue's restore is the same return in contract
    shape, so it carries the issuer-save pid (`rescue_pid`) for the same check.

    `homebound` (gift_vessel only) is the server's attestation that some of the
    crew aboard are the RECIPIENT's own kerbals coming back to them — see
    `data/crew_ledger.py`. It is the quicksend equivalent of a rescue contract's
    `rescue_kerbals`, and the client passes it to
    `VesselTransfer.ApplyIncomingOwnershipTag` as the one list that may strip an
    incoming ownership tag. Absent (not empty) when nothing is attested: a client
    must be able to tell "nobody vouched for these" from "these are vouched for and
    the list is empty", because the first keeps the impersonation refusal and the
    second would be a promise nothing backs.
    """
    guild_id = queue_guild(guild_id, user_id)
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
        # `owner_name` is a DISPLAY name — self-chosen, mutable, not unique — and it
        # is what the client renders ("A's Jeb"). `owner_id` is the immutable account
        # id and is what the client must *decide* with. Keying the decision on the
        # name let anyone set their Discord display name to a victim's, send crew
        # with plain names, and have the victim's own kerbals adopted onto the
        # arriving vessel — deleted on the next hand-over. Two players who merely
        # share a nickname did the same thing by accident.
        "owner_name": owner_name,
        "owner_id": str(owner_id) if owner_id else "",
        "flag_url": flag_url,
        "blueprint_url": blueprint_url,
        "sender_id": sender_id,
        "status": status,
        "vessel_pid": vessel_pid,
        "homebound": list(homebound) if homebound else None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _col(guild_id, user_id).document(iid).set(entry)
    # %s, not %d: a user id is an account id and a website one is not numeric —
    # %d raised inside logging on every quicksend to a web account, dropping the line.
    log.info("Queued craft import %s (%s:%s, %s) for user %s", iid, source, ref_id, status, user_id)
    return entry


def list_pending(guild_id: int, user_id: int) -> list[ImportEntry]:
    guild_id = queue_guild(guild_id, user_id)
    return [doc.to_dict() for doc in _col(guild_id, user_id).stream()]


def get(guild_id: int, user_id: int, import_id: str) -> ImportEntry | None:
    doc = _col(queue_guild(guild_id, user_id), user_id).document(import_id).get()
    return doc.to_dict() if doc.exists else None


def claim_offer(guild_id: int, user_id: int, import_id: str, new_status: str) -> ImportEntry | None:
    """Atomically settle an OFFERED gift: flip it to `new_status` ("queued" on
    accept, "rejected" on decline) only if it is still offered, and return the
    entry as it was. None when it is gone or already settled — so of an accept and
    a reject racing on one offer exactly one wins, which is what stops a live
    vessel ending up in both the recipient's and the sender's save. Runs in a
    Firestore transaction rather than relying on the handlers not yielding
    between check and write, so it holds across workers too."""
    ref = _col(queue_guild(guild_id, user_id), user_id).document(import_id)
    transaction = _db.transaction()

    @firestore.transactional
    def _claim(txn) -> ImportEntry | None:
        snap = ref.get(transaction=txn)
        if not snap.exists:
            return None
        d = snap.to_dict() or {}
        if (d.get("status") or "queued") != "offered":
            return None
        txn.update(ref, {"status": new_status})
        return d

    return _claim(transaction)


def sweep_stale_gift_files(max_age_days: int) -> int:
    """Delete gift payloads older than `max_age_days` from Storage.

    A gift's files are otherwise removed only when the recipient acts on the
    offer (accept → import ack, or decline). An offer nobody ever answers — a
    quicksend to an account that never polls — keeps its files forever, and the
    upload quota is a daily *rate*, so that was unbounded cumulative storage.
    Anything this old is abandoned: an accepted gift is imported within minutes,
    and a declined vessel's return is fetched the next time the sender plays.
    Storage-side rather than a Firestore query because the entries live under
    every guild's every user (a collection-group query would need an index) and
    the object's own creation time is the fact being tested. One list operation
    per thousand objects, once a day."""
    if _storage_bucket is None:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(max_age_days)))
    removed = 0
    try:
        for blob in _storage_bucket.list_blobs(prefix="gifts/"):
            created = getattr(blob, "time_created", None)
            if created is None or created > cutoff:
                continue
            try:
                blob.delete()
                removed += 1
            except Exception as exc:
                log.warning("Could not delete stale gift file %s: %s", blob.name, exc)
    except Exception as exc:
        log.warning("Stale gift sweep failed: %s", exc)
    if removed:
        log.info("Stale gift sweep removed %d file(s) older than %d days", removed, max_age_days)
    return removed


def set_status(guild_id: int, user_id: int, import_id: str, status: str) -> bool:
    ref = _col(queue_guild(guild_id, user_id), user_id).document(import_id)
    if not ref.get().exists:
        return False
    ref.update({"status": status})
    return True


def delete(guild_id: int, user_id: int, import_id: str) -> bool:
    ref = _col(queue_guild(guild_id, user_id), user_id).document(import_id)
    if not ref.get().exists:
        return False
    ref.delete()
    return True
