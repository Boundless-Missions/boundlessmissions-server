"""
data/marketplace.py – Firestore + Firebase Storage helpers for the craft marketplace.

A listing is a craft (.craft blueprint) a player put up for sale. Listings are
non-exclusive: buying one transfers KCoins to the seller and delivers the buyer a
copy of the blueprint, but the listing stays active so anyone else can buy it too.
Crafts are listed from the KSP mod's Market panel and bought on the website; Discord
is out of it entirely (see cogs/marketplace.py).

Firestore structure (GLOBAL — the marketplace spans every server):
    marketplace/{listing_id} → { ...listing fields..., guild_id (origin), mirrors }
    marketplace_votes/{user_id} → { votes: { listing_id: 1 | -1 } }
    marketplace_reports/{listing_id}_{reporter_id} → { ...one report... }
"""
import logging
import uuid
from datetime import datetime
from typing import Any

from firebase_admin import firestore

from data.store import _db, _storage_bucket, safe_filename, upload_private

log = logging.getLogger(__name__)

# Status constants
ACTIVE = "active"
DELISTED = "delisted"

# Vote values. A vote is a tri-state, not a toggle: the client always sends the
# state it wants (NONE clears), so a double-click can't flip an unrelated later
# state back on.
VOTE_UP = 1
VOTE_DOWN = -1
VOTE_NONE = 0

ListingData = dict[str, Any]


def _col():
    """The single global marketplace collection (listings are visible/buyable in
    every server). guild_id is recorded on the doc as the origin only."""
    return _db.collection("marketplace")


def create_listing(
    guild_id: int, seller_id: int, seller_name: str,
    craft_name: str, craft_type: str, part_count: int,
    mass: float, cost: float, price: int,
    craft_url: str, craft_filename: str,
    blueprint_url: str = "",
    thumbnail_url: str = "",
    mods: list[str] | None = None,
    parts: list[str] | None = None,
    life_support: str = "none",
    ls_endurance_days: float = 0.0,
    ls_crew_capacity: int = 0,
    custom_textures: bool = False,
) -> ListingData:
    lid = uuid.uuid4().hex[:12]
    now = datetime.utcnow().isoformat()
    doc: ListingData = {
        "listing_id": lid,
        "guild_id": str(guild_id),
        "seller_id": str(seller_id),
        "seller_name": seller_name,
        "craft_name": craft_name,
        "craft_type": craft_type,
        "part_count": part_count,
        "mass": mass,
        "cost": cost,
        "price": price,
        "craft_url": craft_url,
        "craft_filename": craft_filename,
        "blueprint_url": blueprint_url,
        # Square NW-view render shown on the website's listing cards (the full
        # multi-view blueprint_url is reserved for the detail view). Empty for
        # listings made before the thumbnail existed — the site falls back to the
        # blueprint there.
        "thumbnail_url": thumbnail_url,
        # Distinct GameData mod folders the craft uses, sent by the KSP client at
        # list-time. Empty for stock-only crafts or listings made before mod tagging
        # existed. Powers the website's "filter by mod" facet.
        "mods": mods or [],
        # Exact part names the craft uses, sent by the KSP client at list-time. Powers
        # the pre-purchase compatibility check against a buyer's uploaded part catalog,
        # which "mods" cannot do: having the mod is no guarantee of having the part.
        # Empty for listings made before part tagging existed — the check reports
        # "unknown" for those rather than guessing.
        "parts": parts or [],
        # Life-support flag sent by the KSP client: which LS mod the craft is provisioned
        # for ("none"/"usi"/"tac"/"snacks"/"kerbalism"), how many in-game days it lasts per
        # kerbal, and its crew capacity — together these give the min/max endurance range
        # ("X days for 1 kerbal … Y days for a full crew of N") shown on listings.
        "life_support": life_support or "none",
        "ls_endurance_days": float(ls_endurance_days or 0.0),
        "ls_crew_capacity": int(ls_crew_capacity or 0),
        # Whether the craft carries a Textures Unlimited paint job, sent by the KSP
        # client at list-time (TextureTransfer.CraftHasCustomTextures) — the website's
        # "Modded Textures Available" tag. A flag of its own rather than a read of
        # `mods`: a texture set the seller can't resolve either contributes no folder
        # while the paint job is still on the craft. False for listings made before the
        # flag existed; those fall back to the mod row (see _listing_to_model).
        "custom_textures": bool(custom_textures),
        "status": ACTIVE,
        "created_at": now,
        # Vote tallies. These are *derived* counters kept in step with the per-user
        # vote records in `marketplace_votes` (see set_vote) — cheap to read on a
        # 25-card grid, which counting the real votes per listing would not be.
        "likes": 0,
        "dislikes": 0,
        # How many distinct users have reported this listing (one report per user
        # per listing — see record_report). Never shown publicly; it exists so the
        # owner console can sort by "most complained about".
        "report_count": 0,
        # Dead field, kept so a listing written now has the same shape as one written
        # before Discord stopped mirroring listings: [{guild_id, channel_id, message_id}].
        # Nothing writes it and nothing reads it any more.
        "mirrors": [],
        "buyers": [],
        "sales_count": 0,
    }
    _col().document(lid).set(doc)
    log.info("Listing %s created: %s selling %s for %d", lid, seller_name, craft_name, price)
    return doc


def get_listing(guild_id: int, listing_id: str) -> ListingData | None:
    snap = _col().document(listing_id).get()
    return snap.to_dict() if snap.exists else None


def update_listing(guild_id: int, listing_id: str, **fields) -> None:
    _col().document(listing_id).update(fields)


def list_active(guild_id: int) -> list[ListingData]:
    """All active listings, globally (guild_id ignored — one shared market)."""
    return [
        doc.to_dict()
        for doc in _col().where("status", "==", ACTIVE).stream()
    ]


def list_by_seller(seller_id: int) -> list[ListingData]:
    """Every listing a user created (active AND delisted) — the website's
    "My Uploads" view, where the seller can still see and delist their crafts."""
    return [
        doc.to_dict()
        for doc in _col().where("seller_id", "==", str(seller_id)).stream()
    ]


def list_by_buyer(buyer_id: int) -> list[ListingData]:
    """Every listing a user has bought (so the website can offer a free
    re-download under "My Purchases"). Firestore has no "array contains" index
    requirement issue here — it's a single array-contains on `buyers`."""
    return [
        doc.to_dict()
        for doc in _col().where("buyers", "array_contains", str(buyer_id)).stream()
    ]


def list_all() -> list[ListingData]:
    """Every listing regardless of status or seller — the owner admin console's
    view (it must see delisted crafts to be able to moderate them)."""
    return [doc.to_dict() for doc in _col().stream()]


def delete_listing(listing_id: str) -> None:
    """Permanently remove a listing: its Storage files (craft + blueprint, the whole
    marketplace/{id}/ prefix) and the Firestore document. Best-effort on Storage so a
    missing bucket/file never blocks deleting the record. Irreversible."""
    if _storage_bucket is not None:
        try:
            for blob in _storage_bucket.list_blobs(prefix=f"marketplace/{listing_id}/"):
                try:
                    blob.delete()
                except Exception as exc:
                    log.warning("Could not delete blob %s: %s", blob.name, exc)
        except Exception as exc:
            log.warning("Could not list Storage blobs for listing %s: %s", listing_id, exc)
    _col().document(listing_id).delete()
    log.info("Listing %s permanently deleted", listing_id)


def record_purchase(guild_id: int, listing_id: str, buyer_id: int) -> None:
    """Append a buyer and bump the sales counter."""
    doc_ref = _col().document(listing_id)
    snap = doc_ref.get()
    if not snap.exists:
        return
    data = snap.to_dict()
    buyers = data.get("buyers", [])
    if str(buyer_id) not in buyers:
        buyers.append(str(buyer_id))
    doc_ref.update({
        "buyers": buyers,
        "sales_count": data.get("sales_count", 0) + 1,
    })


# ── Votes ────────────────────────────────────────────────────────────────────
#
# Who voted what lives in ONE document per user (`marketplace_votes/{user_id}`),
# not one per (user, listing). The website needs "which of these 25 crafts have I
# voted on?" on every grid load, and a per-pair layout answers that with either 25
# reads or a collection-group query behind a composite index; a per-user map
# answers it with a single read. The tallies on the listing are the mirror image
# of the same trade-off: a count that is read on every card is stored, not summed.
#
# The two are kept in step by set_vote, which is the only writer of either.

def _votes_doc(user_id: int | str):
    return _db.collection("marketplace_votes").document(str(user_id))


def get_user_votes(user_id: int | str) -> dict[str, int]:
    """Every vote this user has cast: {listing_id: 1 | -1}. Empty for a user who
    has never voted (no document), which is the common case."""
    snap = _votes_doc(user_id).get()
    if not snap.exists:
        return {}
    raw = (snap.to_dict() or {}).get("votes") or {}
    return {k: int(v) for k, v in raw.items() if int(v) in (VOTE_UP, VOTE_DOWN)}


def set_vote(listing_id: str, user_id: int | str, vote: int) -> tuple[int, int] | None:
    """Record `vote` (VOTE_UP / VOTE_DOWN / VOTE_NONE) by `user_id` on a listing.

    Returns the listing's (likes, dislikes) after the change, or None if the
    listing is gone. Idempotent: re-sending the same vote changes nothing.

    The tally is moved with a Firestore Increment (atomic — two users voting at
    once can't clobber each other) and only then is the user's own record written;
    if that second write fails the increment is undone, so the counter never keeps
    a vote nobody is recorded as having cast.
    """
    vote = VOTE_UP if vote > 0 else (VOTE_DOWN if vote < 0 else VOTE_NONE)
    ref = _col().document(listing_id)
    snap = ref.get()
    if not snap.exists:
        return None
    data = snap.to_dict() or {}

    old = int(get_user_votes(user_id).get(listing_id, VOTE_NONE))
    likes = max(0, int(data.get("likes", 0) or 0))
    dislikes = max(0, int(data.get("dislikes", 0) or 0))
    if old == vote:
        return likes, dislikes

    d_like = (1 if vote == VOTE_UP else 0) - (1 if old == VOTE_UP else 0)
    d_dislike = (1 if vote == VOTE_DOWN else 0) - (1 if old == VOTE_DOWN else 0)

    ref.update({"likes": firestore.Increment(d_like),
                "dislikes": firestore.Increment(d_dislike)})
    try:
        # DELETE_FIELD rather than a stored 0: the map is the list of votes actually
        # cast, so a cleared vote must leave nothing behind. set(merge=True) is used
        # for both cases because update() would fail for a user voting for the first
        # time, whose document does not exist yet.
        _votes_doc(user_id).set(
            {"votes": {listing_id: firestore.DELETE_FIELD if vote == VOTE_NONE else vote}},
            merge=True,
        )
    except Exception:
        ref.update({"likes": firestore.Increment(-d_like),
                    "dislikes": firestore.Increment(-d_dislike)})
        raise

    return max(0, likes + d_like), max(0, dislikes + d_dislike)


# ── Reports ──────────────────────────────────────────────────────────────────

def _report_id(listing_id: str, reporter_id: int | str) -> str:
    return f"{listing_id}_{reporter_id}"


def get_report(listing_id: str, reporter_id: int | str) -> dict[str, Any] | None:
    """This user's existing report against this listing, if any. The document id
    is the (listing, reporter) pair, so "have I already reported this?" is a
    single keyed read — no composite index, and no way to file the same complaint
    twice to make it look louder."""
    snap = _db.collection("marketplace_reports").document(
        _report_id(listing_id, reporter_id)).get()
    return snap.to_dict() if snap.exists else None


def record_report(listing: ListingData, reporter_id: int | str, reporter_name: str,
                  reason: str, guild_id: int | str = "",
                  ticket_channel_id: int | str = "") -> None:
    """Store a report and bump the listing's report_count.

    The Discord ticket is where a report is actually *handled*; this record exists
    so the count survives the ticket being closed, and so a second report from the
    same user overwrites rather than accumulates."""
    listing_id = listing["listing_id"]
    first_time = get_report(listing_id, reporter_id) is None
    _db.collection("marketplace_reports").document(_report_id(listing_id, reporter_id)).set({
        "listing_id": listing_id,
        "craft_name": listing.get("craft_name", ""),
        "seller_id": str(listing.get("seller_id", "")),
        "seller_name": listing.get("seller_name", ""),
        "reporter_id": str(reporter_id),
        "reporter_name": reporter_name,
        "reason": reason,
        "guild_id": str(guild_id),
        "ticket_channel_id": str(ticket_channel_id),
        "created_at": datetime.utcnow().isoformat(),
    })
    if first_time:
        try:
            _col().document(listing_id).update({"report_count": firestore.Increment(1)})
        except Exception as exc:  # a missing listing must not lose the report
            log.warning("Could not bump report_count for listing %s: %s", listing_id, exc)
    log.info("Listing %s reported by %s (%s)", listing_id, reporter_name, reporter_id)


async def upload_craft(listing_id: str, filename: str, data: bytes) -> str:
    """Upload a raw (decompressed) .craft file to Storage as a PRIVATE object and
    return its bucket path (not a public URL).

    A listing's craft is behind the paywall: the download URL is minted (signed) only
    for the buyer / owner surfaces (buy result, My Purchases, My Uploads) and is
    withheld from the public grid. The browser download still forces a save because
    the website's /api/marketplace/download proxy streams it with
    Content-Disposition: attachment — so no per-object content_disposition is needed."""
    if _storage_bucket is None:
        raise RuntimeError("Firebase Storage not configured")
    name = safe_filename(filename, 'craft.craft')
    path = f"marketplace/{listing_id}/{name}"
    return upload_private(path, data, content_type="text/plain")


async def upload_blueprint(listing_id: str, data: bytes, content_type: str = "image/png") -> str:
    """Upload a rendered blueprint image for a listing. Returns public URL."""
    if _storage_bucket is None:
        raise RuntimeError("Firebase Storage not configured")
    path = f"marketplace/{listing_id}/blueprint.png"
    blob = _storage_bucket.blob(path)
    blob.upload_from_string(data, content_type=content_type)
    blob.make_public()
    log.info("Uploaded %s to Storage", path)
    return blob.public_url


async def upload_thumbnail(listing_id: str, data: bytes, content_type: str = "image/png") -> str:
    """Upload the square NW-view thumbnail for a listing (website card). Returns public URL."""
    if _storage_bucket is None:
        raise RuntimeError("Firebase Storage not configured")
    path = f"marketplace/{listing_id}/thumbnail.png"
    blob = _storage_bucket.blob(path)
    blob.upload_from_string(data, content_type=content_type)
    blob.make_public()
    log.info("Uploaded %s to Storage", path)
    return blob.public_url
