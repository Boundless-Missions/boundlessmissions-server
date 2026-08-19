"""
data/auctions.py – Firestore helpers for reverse (Dutch) auctions.

An auction is a contract whose price is bid DOWN by contractors. The lowest bid
when the auction closes wins and is converted into an active contract.
Documents live in the GLOBAL auctions/{auction_id} collection (auctions are
mirrored into every server); guild_id on the doc is the origin only.
"""
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from firebase_admin import firestore

from data.store import _db

log = logging.getLogger(__name__)

# Status constants
OPEN = "open"
CLOSED = "closed"        # ended with a winner
CANCELLED = "cancelled"  # ended with no bids (escrow refunded)

AuctionData = dict[str, Any]


def _col():
    return _db.collection("auctions")


def create_auction(
    guild_id: int, issuer_id: int, issuer_name: str,
    mission: str, start_value: int, fine: int, due_date: str,
    ends_at: str, modlist: str | None = None, min_decrement: int = 1,
    mission_type: str | None = None,
) -> AuctionData:
    aid = uuid.uuid4().hex[:12]
    now = datetime.utcnow().isoformat()
    doc: AuctionData = {
        "auction_id": aid,
        "guild_id": str(guild_id),
        "issuer_id": str(issuer_id),
        "issuer_name": issuer_name,
        "mission": mission,
        "start_value": start_value,
        # current_bid starts at the ceiling; bidder is None until someone bids.
        "current_bid": start_value,
        "current_bidder_id": None,
        "current_bidder_name": None,
        "bid_count": 0,
        "fine": fine,
        "due_date": due_date,
        "modlist": modlist,
        "min_decrement": min_decrement,
        "status": OPEN,
        "created_at": now,
        "ends_at": ends_at,
        # Cross-server message mirrors: [{guild_id, channel_id, message_id}, ...]
        "mirrors": [],
        "result_contract_id": None,
    }
    # Mission type (craft_build / active_vessel) the winner's contract inherits.
    if mission_type:
        doc["mission_type"] = mission_type
    _col().document(aid).set(doc)
    log.info("Auction %s created by %s (start %d, ends %s)", aid, issuer_name, start_value, ends_at)
    return doc


def get_auction(guild_id: int, auction_id: str) -> AuctionData | None:
    snap = _col().document(auction_id).get()
    return snap.to_dict() if snap.exists else None


def update_auction(guild_id: int, auction_id: str, **fields) -> None:
    _col().document(auction_id).update(fields)


def try_place_bid(guild_id: int, auction_id: str, bidder_id, bidder_name: str,
                  amount: int, antisnipe_seconds: int = 0) -> dict:
    """Atomically place a reverse-auction bid (lowest bid wins), re-validating
    against the *committed* auction state inside a Firestore transaction.

    A plain read-then-update lets two near-simultaneous bids validate against the
    same `current_bid` and then race to write, so the later write wins even when it
    is a higher (worse) bid — the earlier, genuinely-lower bid is silently clobbered.
    Doing the ceiling check and the write in one transaction closes that: the ceiling
    is measured against whatever bid is actually committed at write time.

    Returns one of:
      {"ok": True,  "auction": <updated dict>}
      {"ok": False, "reason": "missing"}
      {"ok": False, "reason": "closed"}
      {"ok": False, "reason": "own"}
      {"ok": False, "reason": "too_high", "ceiling": <int>}
    """
    ref = _col().document(auction_id)
    transaction = _db.transaction()

    @firestore.transactional
    def _bid(txn) -> dict:
        snap = ref.get(transaction=txn)
        if not snap.exists:
            return {"ok": False, "reason": "missing"}
        a = snap.to_dict() or {}

        now = datetime.utcnow()
        if a.get("status") != OPEN or a.get("ends_at", "") <= now.isoformat():
            return {"ok": False, "reason": "closed"}
        if str(bidder_id) == str(a.get("issuer_id")):
            return {"ok": False, "reason": "own"}

        step = a.get("min_decrement", 1)
        ceiling = a["current_bid"] - step
        if amount > ceiling:
            return {"ok": False, "reason": "too_high", "ceiling": ceiling, "step": step}

        fields = {
            "current_bid": amount,
            "current_bidder_id": str(bidder_id),
            "current_bidder_name": bidder_name,
            "bid_count": a.get("bid_count", 0) + 1,
        }
        # Anti-snipe: a late bid pushes the end back so others can respond. Evaluated
        # against the committed end time, same as the ceiling.
        if antisnipe_seconds > 0:
            end_dt = datetime.fromisoformat(a["ends_at"])
            if (end_dt - now).total_seconds() < antisnipe_seconds:
                fields["ends_at"] = (now + timedelta(seconds=antisnipe_seconds)).isoformat()

        txn.update(ref, fields)
        a.update(fields)
        return {"ok": True, "auction": a}

    return _bid(transaction)


def list_open(guild_id: int) -> list[AuctionData]:
    """All open auctions, globally (guild_id ignored; used by the close loop)."""
    return [d.to_dict() for d in _col().where("status", "==", OPEN).stream()]
