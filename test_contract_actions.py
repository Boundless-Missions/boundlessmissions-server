"""Offline exercise of contract_actions: authorization, state gates, money flow.

Everything below the service is stubbed — no Firestore, no Discord — so this checks
the decisions the module makes, not the plumbing under it.
"""
import asyncio
import os
from datetime import datetime, timedelta
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import contract_actions as ca
import settings
from data import contracts as cdb
import api_server

BOT = 999
GID = 1
ISSUER = 100
CONTRACTOR = 200
STRANGER = 300

DB = {}
BAL = {}
EVENTS = []


def _mk(status=cdb.PENDING, **over):
    c = {
        "contract_id": "c1", "guild_id": str(GID),
        "issuer_id": str(ISSUER), "issuer_name": "Issuer",
        "contractor_id": str(CONTRACTOR), "contractor_name": "Contractor",
        "mission": "Land on the Mun", "payment": 100, "fine": 40,
        "due_date": "2099-01-01", "status": status,
    }
    c.update(over)
    DB["c1"] = c
    return c


# ── stubs ────────────────────────────────────────────────────────────────────
cdb.get_contract = lambda gid, cid: dict(DB[cid]) if cid in DB else None


def _update(gid, cid, **fields):
    DB[cid].update(fields)


cdb.update_contract = _update


class _Store:
    async def add_balance(self, gid, uid, amt):
        BAL[uid] = BAL.get(uid, 0) + amt
        return BAL[uid]

    async def try_debit(self, gid, uid, amt):
        if BAL.get(uid, 0) < amt:
            return False
        BAL[uid] -= amt
        return True

    async def debit_up_to(self, gid, uid, amt):
        take = min(BAL.get(uid, 0), amt)
        BAL[uid] = BAL.get(uid, 0) - take
        return take

    async def add_rescue(self, gid, uid, amount=1):
        return 1


ca.store = _Store()
api_server._bot_user_id = BOT
api_server._bot_instance = None
api_server._create_notification = lambda *a, **k: EVENTS.append(("notify", a[1], a[2]))


async def _deliver(gid, cid, c):
    EVENTS.append(("deliver_rescue", cid))


async def _restore(gid, cid, c):
    EVENTS.append(("restore_vessel", cid))


api_server._deliver_rescue_craft = _deliver
api_server._restore_issuer_vessel = _restore


# ── assertions ───────────────────────────────────────────────────────────────
FAILED = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL {label} {detail}")


async def main():
    global BAL, EVENTS

    print("\naccept")
    _mk()
    r = await ca.accept(GID, "c1", actor_id=STRANGER, actor_name="Nosy")
    check("a stranger cannot accept", not r.ok and r.code == ca.FORBIDDEN, r.code)
    check("status untouched after refusal", DB["c1"]["status"] == cdb.PENDING)
    r = await ca.accept(GID, "c1", actor_id=ISSUER, actor_name="Issuer")
    check("the issuer cannot accept their own offer", not r.ok and r.code == ca.FORBIDDEN)
    r = await ca.accept(GID, "c1", actor_id=CONTRACTOR, actor_name="Contractor")
    check("the contractor can accept", r.ok and DB["c1"]["status"] == cdb.ACTIVE)
    r = await ca.accept(GID, "c1", actor_id=CONTRACTOR, actor_name="Contractor")
    check("accepting twice is refused", not r.ok and r.code == ca.BAD_STATE)
    r = await ca.accept(GID, "nope", actor_id=CONTRACTOR, actor_name="Contractor")
    check("missing contract is not_found", not r.ok and r.code == ca.NOT_FOUND)

    print("\ncancel")
    _mk(status=cdb.ACTIVE)
    BAL = {}
    r = await ca.cancel(GID, "c1", actor_id=CONTRACTOR, actor_name="Contractor")
    check("contractor cannot cancel an active contract for free",
          not r.ok and r.code == ca.USE_GIVE_UP, r.code)
    check("no refund was paid on that refusal", BAL.get(ISSUER, 0) == 0)
    r = await ca.cancel(GID, "c1", actor_id=STRANGER, actor_name="Nosy")
    check("a stranger cannot cancel", not r.ok and r.code == ca.FORBIDDEN)
    r = await ca.cancel(GID, "c1", actor_id=ISSUER, actor_name="Issuer")
    check("issuer can withdraw an active contract", r.ok and DB["c1"]["status"] == cdb.CANCELLED)
    check("escrow refunded once", BAL.get(ISSUER) == 100, BAL)
    r = await ca.cancel(GID, "c1", actor_id=ISSUER, actor_name="Issuer")
    check("cancelling twice is refused", not r.ok and r.code == ca.BAD_STATE)
    check("no second refund", BAL.get(ISSUER) == 100, BAL)

    _mk(status=cdb.PENDING)
    BAL = {}
    r = await ca.cancel(GID, "c1", actor_id=CONTRACTOR, actor_name="Contractor")
    check("contractor may decline a pending offer", r.ok and DB["c1"]["status"] == cdb.CANCELLED)

    print("\ncancel — bot issuer")
    _mk(status=cdb.ACTIVE, issuer_id=str(BOT))
    BAL = {}
    await ca.cancel(GID, "c1", actor_id=BOT, actor_name="Bot")
    check("a bot issuer is never credited", BAL.get(BOT, 0) == 0, BAL)

    print("\ngive_up")
    _mk(status=cdb.ACTIVE)
    BAL = {CONTRACTOR: 10}
    r = await ca.give_up(GID, "c1", actor_id=CONTRACTOR, actor_name="Contractor")
    check("give up is refused without the fine", not r.ok and r.code == ca.NO_FUNDS)
    check("state untouched", DB["c1"]["status"] == cdb.ACTIVE)
    BAL = {CONTRACTOR: 50}
    r = await ca.give_up(GID, "c1", actor_id=ISSUER, actor_name="Issuer")
    check("the issuer cannot give up", not r.ok and r.code == ca.FORBIDDEN)
    r = await ca.give_up(GID, "c1", actor_id=CONTRACTOR, actor_name="Contractor")
    check("contractor gives up", r.ok and DB["c1"]["status"] == cdb.CANCELLED)
    check("fine debited", BAL[CONTRACTOR] == 10, BAL)
    check("issuer paid fine + escrow", BAL[ISSUER] == 140, BAL)

    print("\nreview")
    _mk(status=cdb.SUBMITTED)
    BAL = {}
    r = await ca.review(GID, "c1", actor_id=CONTRACTOR, actor_name="C", approve=True)
    check("the contractor cannot approve their own submission",
          not r.ok and r.code == ca.FORBIDDEN)
    check("nothing was paid", BAL == {}, BAL)
    r = await ca.review(GID, "c1", actor_id=ISSUER, actor_name="Issuer", approve=True)
    check("issuer approves", r.ok and DB["c1"]["status"] == cdb.COMPLETED)
    check("contractor paid", BAL[CONTRACTOR] == 100, BAL)
    r = await ca.review(GID, "c1", actor_id=ISSUER, actor_name="Issuer", approve=True)
    check("approving twice is refused", not r.ok and r.code == ca.BAD_STATE)
    check("no double payment", BAL[CONTRACTOR] == 100, BAL)

    print("\nreview — rescue delivery")
    _mk(status=cdb.SUBMITTED, mission_type=cdb.RESCUE, issuer_vessel_removed=True)
    BAL, EVENTS = {}, []
    await ca.review(GID, "c1", actor_id=ISSUER, actor_name="Issuer", approve=True)
    check("approving a rescue delivers the craft",
          ("deliver_rescue", "c1") in EVENTS, EVENTS)

    print("\ndispute")
    _mk(status=cdb.DISPUTED)
    BAL = {CONTRACTOR: 500}
    r = await ca.dispute(GID, "c1", actor_id=ISSUER, actor_name="I", action="pay_fine")
    check("the issuer cannot drive the dispute", not r.ok and r.code == ca.FORBIDDEN)
    r = await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C", action="nonsense")
    check("an unknown action is refused", not r.ok and r.code == ca.BAD_REQUEST)
    r = await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C", action="pay_fine")
    check("pay_fine closes the contract", r.ok and DB["c1"]["status"] == cdb.COMPLETED)
    check("fine + escrow to issuer", BAL[ISSUER] == 140 and BAL[CONTRACTOR] == 460, BAL)
    r = await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C", action="pay_fine")
    check("paying the fine twice is refused", not r.ok and r.code == ca.BAD_STATE)
    check("balance unchanged", BAL[CONTRACTOR] == 460, BAL)

    print("\ndispute — rescue is handed back on pay_fine")
    _mk(status=cdb.DISPUTED, mission_type=cdb.RESCUE, issuer_vessel_removed=True)
    BAL, EVENTS = {CONTRACTOR: 100}, []
    await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C", action="pay_fine")
    check("issuer's vessel restored", ("restore_vessel", "c1") in EVENTS, EVENTS)

    print("\ndispute — more_time")
    _mk(status=cdb.DISPUTED)
    r = await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C",
                         action="more_time", new_date="not-a-date")
    check("a malformed date is refused", not r.ok and r.code == ca.BAD_REQUEST)
    r = await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C",
                         action="more_time", new_date="2020-01-01")
    check("a date in the past is refused", not r.ok and r.code == ca.BAD_REQUEST)
    r = await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C",
                         action="more_time", new_date="**2099-01-02**")
    check("a markdown-wrapped date is refused", not r.ok and r.code == ca.BAD_REQUEST)
    r = await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C",
                         action="more_time", new_date="2099-01-02")
    check("a valid future date opens a request", r.ok, f"{r.code}: {r.message}")
    check("status still disputed until the issuer answers",
          DB["c1"]["status"] == cdb.DISPUTED)
    check("the request is recorded on the contract",
          (DB["c1"].get("pending_request") or {}).get("new_date") == "2099-01-02",
          DB["c1"].get("pending_request"))

    print("\ndispute — more_time on a bot contract self-approves")
    _mk(status=cdb.DISPUTED, issuer_id=str(BOT))
    r = await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C", action="more_time")
    check("bot contract extends itself", r.ok and DB["c1"]["status"] == cdb.ACTIVE, r.message)
    check("a new due date was set", DB["c1"]["due_date"] == r.data["new_date"])

    print("\ndispute — settle on a bot contract")
    _mk(status=cdb.DISPUTED, issuer_id=str(BOT))
    r = await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C", action="settle")
    check("AI contracts cannot be settled", not r.ok and r.code == ca.BAD_REQUEST)

    print("\nsettle_response")
    _mk(status=cdb.DISPUTED)
    BAL = {}
    r = await ca.settle_response(GID, "c1", actor_id=ISSUER, actor_name="I", approve=True)
    check("the issuer cannot settle unilaterally", not r.ok and r.code == ca.BAD_STATE, r.code)
    check("nothing was refunded", BAL == {}, BAL)
    await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C", action="settle")
    check("a settle request is recorded",
          (DB["c1"].get("pending_request") or {}).get("kind") == "settle")
    r = await ca.settle_response(GID, "c1", actor_id=CONTRACTOR, actor_name="C", approve=True)
    check("the contractor cannot approve their own settlement",
          not r.ok and r.code == ca.FORBIDDEN)
    r = await ca.more_time_response(GID, "c1", actor_id=ISSUER, actor_name="I", approve=True)
    check("a settle request cannot be answered as an extension",
          not r.ok and r.code == ca.BAD_STATE, r.code)
    r = await ca.settle_response(GID, "c1", actor_id=ISSUER, actor_name="I", approve=True)
    check("issuer settles", r.ok and DB["c1"]["status"] == cdb.CANCELLED)
    check("escrow returned", BAL[ISSUER] == 100, BAL)
    check("the request is cleared", DB["c1"].get("pending_request") is None)
    r = await ca.settle_response(GID, "c1", actor_id=ISSUER, actor_name="I", approve=True)
    check("settling twice is refused", not r.ok and r.code == ca.BAD_STATE)
    check("no second refund", BAL[ISSUER] == 100, BAL)

    print("\nsettle_response — refusing leaves it disputed")
    _mk(status=cdb.DISPUTED)
    BAL = {}
    await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C", action="settle")
    r = await ca.settle_response(GID, "c1", actor_id=ISSUER, actor_name="I", approve=False)
    check("refused", r.ok and DB["c1"]["status"] == cdb.DISPUTED)
    check("request cleared so the buttons come back",
          DB["c1"].get("pending_request") is None)
    check("no money moved", BAL == {}, BAL)

    print("\nmore_time_response")
    _mk(status=cdb.DISPUTED)
    await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C",
                     action="more_time", new_date="2099-05-05")
    r = await ca.more_time_response(GID, "c1", actor_id=CONTRACTOR, actor_name="C",
                                    approve=True)
    check("the contractor cannot approve their own extension",
          not r.ok and r.code == ca.FORBIDDEN)
    r = await ca.more_time_response(GID, "c1", actor_id=ISSUER, actor_name="I",
                                    approve=True, new_date="2099-12-31")
    check("issuer approves the extension",
          r.ok and DB["c1"]["status"] == cdb.ACTIVE)
    check("the date granted is the one requested, not the one passed in",
          DB["c1"]["due_date"] == "2099-05-05", DB["c1"]["due_date"])
    check("the request is cleared", DB["c1"].get("pending_request") is None)

    print("\nreview — issuer drops the dispute and accepts after all")
    _mk(status=cdb.SUBMITTED)
    BAL = {}
    await ca.review(GID, "c1", actor_id=ISSUER, actor_name="I", approve=False)
    check("disputed", DB["c1"]["status"] == cdb.DISPUTED)
    await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C", action="settle")
    r = await ca.review(GID, "c1", actor_id=CONTRACTOR, actor_name="C", approve=True)
    check("the contractor cannot accept their own work",
          not r.ok and r.code == ca.FORBIDDEN)
    r = await ca.review(GID, "c1", actor_id=ISSUER, actor_name="I", approve=False)
    check("refusing an already-disputed contract is refused",
          not r.ok and r.code == ca.BAD_STATE, r.code)
    r = await ca.review(GID, "c1", actor_id=ISSUER, actor_name="I", approve=True)
    check("issuer accepts after all", r.ok and DB["c1"]["status"] == cdb.COMPLETED, r.message)
    check("contractor paid in full", BAL[CONTRACTOR] == 100, BAL)
    check("no fine was taken", CONTRACTOR not in BAL or BAL[CONTRACTOR] == 100)
    check("the open settle request is cleared",
          DB["c1"].get("pending_request") is None)
    check("and it is off the auto-fine clock", ca.auto_fine_at(DB["c1"]) is None)
    r = await ca.review(GID, "c1", actor_id=ISSUER, actor_name="I", approve=True)
    check("accepting twice is refused", not r.ok and r.code == ca.BAD_STATE)
    check("no double payment", BAL[CONTRACTOR] == 100, BAL)

    print("\nreview — an escalated case stays with the moderators")
    _mk(status=cdb.MOD_REVIEW)
    BAL = {}
    r = await ca.review(GID, "c1", actor_id=ISSUER, actor_name="I", approve=True)
    check("the issuer cannot accept out from under a mod review",
          not r.ok and r.code == ca.BAD_STATE, r.code)
    check("nothing was paid", BAL == {}, BAL)

    print("\ndispute clock — one extension request per dispute")
    _mk(status=cdb.SUBMITTED)
    BAL = {}
    await ca.review(GID, "c1", actor_id=ISSUER, actor_name="I", approve=False)
    check("refusing stamps the dispute clock", bool(DB["c1"].get("disputed_at")))
    check("the extension allowance is reset", DB["c1"].get("more_time_requests") == 0)
    r = await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C",
                         action="more_time", new_date="2099-03-03")
    check("first extension request is allowed", r.ok, r.message)
    await ca.more_time_response(GID, "c1", actor_id=ISSUER, actor_name="I", approve=False)
    check("still disputed after a refusal", DB["c1"]["status"] == cdb.DISPUTED)
    r = await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C",
                         action="more_time", new_date="2099-04-04")
    check("a second request is refused even after the first was denied",
          not r.ok and r.code == ca.BAD_STATE, r.message)
    r = await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C", action="settle")
    check("settle is still available", r.ok, r.message)

    print("\ndispute clock — a granted extension restores the allowance")
    _mk(status=cdb.SUBMITTED)
    await ca.review(GID, "c1", actor_id=ISSUER, actor_name="I", approve=False)
    await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C",
                     action="more_time", new_date="2099-03-03")
    await ca.more_time_response(GID, "c1", actor_id=ISSUER, actor_name="I", approve=True)
    check("granted, back to active", DB["c1"]["status"] == cdb.ACTIVE)
    DB["c1"]["status"] = cdb.SUBMITTED           # contractor submits again
    await ca.review(GID, "c1", actor_id=ISSUER, actor_name="I", approve=False)
    check("a fresh dispute resets the allowance",
          DB["c1"].get("more_time_requests") == 0)
    r = await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C",
                         action="more_time", new_date="2099-06-06")
    check("so another request is allowed", r.ok, r.message)

    print("\ndispute timeout")
    _mk(status=cdb.DISPUTED)
    BAL = {CONTRACTOR: 500}
    r = await ca.expire_dispute(GID, "c1")
    check("a dispute with no clock is stamped, not fined",
          not r.ok and DB["c1"].get("disputed_at"), r.message)
    check("no money moved", BAL[CONTRACTOR] == 500, BAL)
    r = await ca.expire_dispute(GID, "c1")
    check("and it is not yet due", not r.ok and r.code == ca.BAD_STATE, r.message)

    fresh = datetime.utcnow() - timedelta(days=settings.DISPUTE_AUTO_FINE_DAYS - 1)
    DB["c1"]["disputed_at"] = fresh.isoformat()
    r = await ca.expire_dispute(GID, "c1")
    check("inside the window it is left alone", not r.ok and r.code == ca.BAD_STATE)
    check("still disputed", DB["c1"]["status"] == cdb.DISPUTED)

    stale = datetime.utcnow() - timedelta(days=settings.DISPUTE_AUTO_FINE_DAYS, hours=1)
    DB["c1"]["disputed_at"] = stale.isoformat()
    r = await ca.expire_dispute(GID, "c1")
    check("past the window the fine is collected", r.ok and DB["c1"]["status"] == cdb.COMPLETED)
    check("contractor charged the fine", BAL[CONTRACTOR] == 460, BAL)
    check("issuer paid fine + escrow", BAL[ISSUER] == 140, BAL)
    r = await ca.expire_dispute(GID, "c1")
    check("expiring twice is refused", not r.ok and r.code == ca.BAD_STATE)
    check("no second collection", BAL[CONTRACTOR] == 460, BAL)

    print("\ndispute timeout — a broke contractor cannot stall forever")
    _mk(status=cdb.DISPUTED,
        disputed_at=(datetime.utcnow() - timedelta(days=30)).isoformat())
    BAL = {CONTRACTOR: 5}
    r = await ca.expire_dispute(GID, "c1")
    check("closed anyway", r.ok and DB["c1"]["status"] == cdb.COMPLETED, r.message)
    check("took what they had", BAL[CONTRACTOR] == 0, BAL)
    check("issuer credited only what was actually collected",
          BAL[ISSUER] == 105, BAL)

    print("\ndispute timeout — a pending request does not pause the clock")
    _mk(status=cdb.DISPUTED,
        disputed_at=(datetime.utcnow() - timedelta(days=30)).isoformat())
    BAL = {CONTRACTOR: 500}
    await ca.dispute(GID, "c1", actor_id=CONTRACTOR, actor_name="C", action="settle")
    check("a settle request is open", DB["c1"].get("pending_request") is not None)
    r = await ca.expire_dispute(GID, "c1")
    check("the fine still lands", r.ok and DB["c1"]["status"] == cdb.COMPLETED, r.message)
    check("the request is cleared", DB["c1"].get("pending_request") is None)

    print("\nmod_resolve")
    _mk(status=cdb.MOD_REVIEW)
    BAL = {CONTRACTOR: 25}
    r = await ca.mod_resolve(GID, "c1", actor_id=STRANGER, actor_name="Mod", enforce=True)
    check("enforced", r.ok and DB["c1"]["status"] == cdb.COMPLETED)
    check("only what the contractor had was taken", BAL[CONTRACTOR] == 0, BAL)
    check("issuer credited exactly that plus escrow", BAL[ISSUER] == 125, BAL)
    r = await ca.mod_resolve(GID, "c1", actor_id=STRANGER, actor_name="Mod", enforce=True)
    check("resolving twice is refused", not r.ok and r.code == ca.BAD_STATE)
    check("no second payout", BAL[ISSUER] == 125, BAL)

    _mk(status=cdb.MOD_REVIEW)
    BAL = {CONTRACTOR: 500}
    r = await ca.mod_resolve(GID, "c1", actor_id=STRANGER, actor_name="Mod", enforce=False)
    check("cancelling the fine takes nothing", BAL[CONTRACTOR] == 500, BAL)
    check("escrow refunded", BAL[ISSUER] == 100, BAL)

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: {FAILED}")
        return 1
    print("all checks passed")
    return 0


sys.exit(asyncio.run(main()))
