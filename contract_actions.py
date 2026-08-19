"""
contract_actions.py — the one implementation of every contract state transition.

Why this file exists
────────────────────
Every transition was written twice: once as an HTTP endpoint in `api_server.py` for
the KSP mod, once as a Discord button in `cogs/contract_views.py`. The two copies had
different authorization stories and had drifted on what the transition actually *does*.

The authorization story on the Discord side was **delivery**: `ContractOfferView` is
attached to a DM, so `interaction.user` had to be the contractor and no explicit check
was needed. That is implicit, and it had already broken — `ContractWorkView`
(Give Up / Submit) is also posted to a *public corp channel* by the weekly-mission flow
(`cogs/weeklymissions.py`), where the Give Up button carried no actor check at all.
`ModReviewView` likewise lands in a dispute ticket that both disputing parties can see
and type in.

The behavioural drift, all verified against the two implementations before this file
was written:

  • **Give Up** — the API charges the agreed fine and pays the issuer fine+payment.
    The Discord button charged nothing and merely refunded escrow, so backing out was
    free on Discord and expensive in game.
  • **Refuse offer / Pay fine** — the Discord buttons had no status check, so pressing
    one a second time on a closed contract re-ran the payout.
  • **Rescue approval** — the API hands the craft and the rescued kerbals back to the
    issuer (`_deliver_rescue_craft`). The Discord review button did not, so approving a
    rescue from Discord left the crew nowhere.
  • **Rescue failure** — the API restores the issuer's stranded vessel on cancel and on
    pay-fine. No Discord path did, so the issuer's ship stayed deleted with nothing
    given back.
  • **Bot-issued contracts** — the API skips crediting a bot issuer; the Discord buttons
    paid coins into the bot's own wallet.
  • **Notifications** — the API tells the other party so their in-game contract list
    refreshes live; the Discord buttons told nobody.

So: one function per transition. It takes the acting user as an **explicit argument**
rather than inferring authority from the transport, performs every side effect
(Firestore, balances, notifications, rescue delivery, the Discord hand-off views), and
returns a plain `Result` the caller renders however it likes. Front ends decide
*presentation*; this file decides *what happens*.

Adding a front end (the public website, in Phase 6a) therefore adds no new copy.

One deliberate behaviour change
───────────────────────────────
`cancel` used to let the **contractor** cancel an *active* contract, which closed it
with no fine — making `give_up`'s fine entirely optional and the penalty decorative.
A contractor may now only cancel while the contract is still `pending` (declining an
offer they never accepted); backing out after accepting goes through `give_up` and
costs the agreed fine. The issuer keeps the old behaviour, since withdrawing an offer
refunds only their own escrow.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import settings
from data import contracts as cdb
from data import guild_config
from data import imports as imp
from data.store import store

log = logging.getLogger(__name__)


def _api():
    """Late import of `api_server`.

    The notification hub, the rescue delivery/restore helpers and the live bot handle
    all live in `api_server`, which imports *this* module — importing it at module
    scope would be a cycle. By the time any function below runs, both modules are
    fully loaded, so the import is a dict lookup.
    """
    import api_server
    return api_server


# ── Result ───────────────────────────────────────────────────────────────────

# Machine-readable codes. Callers map these onto their own vocabulary — the HTTP
# endpoints turn some into 403/404, the Discord buttons turn them into ephemeral
# replies — so no front end has to string-match a human sentence.
NOT_FOUND = "not_found"
FORBIDDEN = "forbidden"
BAD_STATE = "bad_state"
BAD_REQUEST = "bad_request"
NO_FUNDS = "insufficient_funds"
UNAVAILABLE = "unavailable"
USE_GIVE_UP = "use_give_up"


@dataclass
class Result:
    """The outcome of a transition.

    `contract` is the contract dict **after** the action, so a caller that redraws an
    embed does not have to re-read Firestore to see the new status. `data` carries
    action-specific payload (currently only the rescue hand-off on accept).
    """
    ok: bool
    message: str
    code: str = ""
    contract: dict | None = None
    data: dict = field(default_factory=dict)


def _fail(code: str, message: str, contract: dict | None = None) -> Result:
    return Result(ok=False, message=message, code=code, contract=contract)


# ── Shared pieces ────────────────────────────────────────────────────────────

def _load(gid: int, contract_id: str):
    """(contract, None) or (None, failure Result)."""
    c = cdb.get_contract(gid, contract_id)
    if not c:
        return None, _fail(NOT_FOUND, "Contract not found.")
    return c, None


def _is_bot_issued(c: dict) -> bool:
    return str(c.get("issuer_id")) == str(_api()._get_bot_user_id())


def _notify(gid: int, user_id: int, notif_type: str, title: str, message: str,
            contract_id: str) -> None:
    """Best-effort in-game notification. Never let a notification failure roll back a
    transition that has already moved money — the state change is the important half."""
    try:
        _api()._create_notification(gid, int(user_id), notif_type, title, message,
                                    {"contract_id": contract_id})
    except Exception as exc:
        log.warning("Could not notify %s about contract %s: %s", user_id, contract_id, exc)


async def _pay_issuer(gid: int, c: dict, amount: int) -> None:
    """Credit the issuer, unless the issuer is the bot — weekly/AI contracts have no
    wallet to pay into, and crediting one inflates a balance nobody spends."""
    if amount > 0 and not _is_bot_issued(c):
        await store.add_balance(gid, int(c["issuer_id"]), amount)


async def restore_rescue(gid: int, contract_id: str, c: dict) -> None:
    """Rescue contract ending without a rescue → give the issuer their vessel back.

    Safe to call on any contract: it is a no-op unless this is a rescue whose wreck
    was actually removed from the issuer's save.
    """
    if c.get("mission_type") != cdb.RESCUE:
        return
    try:
        await _api()._restore_issuer_vessel(gid, contract_id, c)
    except Exception as exc:
        log.error("Could not restore issuer vessel for rescue %s: %s", contract_id, exc)


# ── Pending requests ─────────────────────────────────────────────────────────
#
# Settle and more-time are *requests*: they change nothing until the issuer answers.
# That answer used to exist only as a Discord DM carrying an approval view, which made
# the DM the sole record that a request was outstanding — nothing on the contract said
# so, so no other front end could show it, and the requested date survived a bot
# restart only by being scraped back out of the embed text.
#
# It lives on the contract now. The Discord views and the in-game buttons are two
# renderings of one piece of state.

REQUEST_SETTLE = "settle"
REQUEST_MORE_TIME = "more_time"


def open_dispute_fields(now: datetime | None = None) -> dict:
    """The fields every path into `disputed` must write.

    There are three of them — an issuer refusing a submission, the AI refusing one on a
    bot contract, and the KSP review endpoint — so this exists to stop them drifting.
    `disputed_at` starts the auto-fine clock; without it a dispute is invisible to the
    sweep and would sit open forever, which is the loophole the clock closes.
    `more_time_requests` resets because the allowance is per dispute, not per contract:
    an extension that was granted and then blown puts you back here with a fresh one.
    """
    now = now or datetime.utcnow()
    return {
        "status": cdb.DISPUTED,
        "disputed_at": now.isoformat(),
        "more_time_requests": 0,
        "pending_request": None,
    }


def auto_fine_at(c: dict) -> datetime | None:
    """When this dispute's fine collects itself, or None if it is not on the clock."""
    stamped = c.get("disputed_at")
    if c.get("status") != cdb.DISPUTED or not stamped:
        return None
    try:
        return (datetime.fromisoformat(stamped)
                + timedelta(days=settings.DISPUTE_AUTO_FINE_DAYS))
    except (ValueError, TypeError):
        return None


def _open_request(gid: int, contract_id: str, c: dict, kind: str,
                  new_date: str = "") -> dict:
    req = {
        "kind": kind,
        "new_date": new_date or None,
        "requested_at": datetime.utcnow().isoformat(),
        "requested_by": str(c.get("contractor_id")),
    }
    cdb.update_contract(gid, contract_id, pending_request=req)
    c["pending_request"] = req
    return req


def _clear_request(c: dict) -> None:
    """Mark the request resolved. Callers fold this into their own status write —
    `pending_request=None` must land in the *same* update as the status change, or a
    front end can see a closed contract still advertising an answerable request."""
    c["pending_request"] = None


def _other_party(c: dict, actor_id) -> int | None:
    """The party who did *not* act, or None when that party is the bot."""
    actor = str(actor_id)
    other = c.get("issuer_id") if str(c.get("contractor_id")) == actor else c.get("contractor_id")
    if not other or str(other) == str(_api()._get_bot_user_id()):
        return None
    return int(other)


# ── Offer: accept / cancel ───────────────────────────────────────────────────

async def accept(gid: int, contract_id: str, *, actor_id: int, actor_name: str) -> Result:
    """Contractor accepts a pending offer."""
    c, err = _load(gid, contract_id)
    if err:
        return err

    if str(c.get("contractor_id")) != str(actor_id):
        return _fail(FORBIDDEN, "This contract was not offered to you.", c)
    if c.get("status") != cdb.PENDING:
        return _fail(BAD_STATE, "Contract is not pending.", c)

    cdb.update_contract(gid, contract_id, status=cdb.ACTIVE)
    c["status"] = cdb.ACTIVE
    log.info("Contract %s accepted by %s", contract_id, actor_name)

    if not _is_bot_issued(c):
        _notify(gid, int(c["issuer_id"]), "contract_accepted", "🤝 Contract Accepted",
                f"{actor_name} accepted your contract \"{c['mission'][:80]}\".", contract_id)

    # Rescue: hand the rescuer the wreck snapshot and target so their client can spawn
    # the stranded vessel. The issuer's copy was removed at creation, so there is
    # nothing to do on their side here.
    if c.get("mission_type") == cdb.RESCUE:
        return Result(
            ok=True, message="Rescue accepted! Spawning the stranded vessel.", contract=c,
            data={
                "rescue_vessel_node_url": c.get("rescue_vessel_node_url"),
                "rescue_target": c.get("rescue_target") or {},
                "rescue_kerbals": c.get("rescue_kerbals", []),
            },
        )

    return Result(ok=True, message="Contract accepted!", contract=c)


async def cancel(gid: int, contract_id: str, *, actor_id: int, actor_name: str) -> Result:
    """Withdraw (issuer) or decline (contractor) a contract that is not yet finished.

    See the module docstring: a contractor may only cancel while the contract is still
    pending. Once accepted, backing out is `give_up` and costs the fine.
    """
    c, err = _load(gid, contract_id)
    if err:
        return err

    is_issuer = str(c.get("issuer_id")) == str(actor_id)
    is_contractor = str(c.get("contractor_id")) == str(actor_id)
    if not is_issuer and not is_contractor:
        return _fail(FORBIDDEN, "Not your contract.", c)

    status = c.get("status")
    if status not in (cdb.PENDING, cdb.ACTIVE):
        return _fail(BAD_STATE, f"Cannot cancel a {status} contract.", c)
    if status == cdb.ACTIVE and is_contractor and not is_issuer:
        return _fail(USE_GIVE_UP,
                     "You already accepted this contract. Use Give Up instead; it costs the "
                     f"agreed {c.get('fine', 0)} {settings.CURRENCY_SYMBOL} fine.", c)

    cdb.update_contract(gid, contract_id, status=cdb.CANCELLED)
    c["status"] = cdb.CANCELLED

    await _pay_issuer(gid, c, c.get("payment", 0))
    log.info("Contract %s cancelled by %s (escrow %s refunded to issuer %s)",
             contract_id, actor_name, c.get("payment"), c.get("issuer_id"))

    other = _other_party(c, actor_id)
    if other:
        _notify(gid, other, "contract_cancelled", "🚫 Contract Cancelled",
                f"{actor_name} cancelled \"{c['mission'][:80]}\".", contract_id)

    await restore_rescue(gid, contract_id, c)
    return Result(ok=True, message="Contract cancelled. Escrow refunded.", contract=c)


async def give_up(gid: int, contract_id: str, *, actor_id: int, actor_name: str) -> Result:
    """Contractor backs out of a contract they accepted, paying the agreed fine.

    The proactive counterpart to the dispute's `pay_fine`: it lets a contractor stop
    *before* submitting, at the cost of the penalty they agreed to.
    """
    c, err = _load(gid, contract_id)
    if err:
        return err

    if str(c.get("contractor_id")) != str(actor_id):
        return _fail(FORBIDDEN, "Only the contractor can give up a contract.", c)
    if c.get("status") != cdb.ACTIVE:
        return _fail(BAD_STATE, f"Cannot give up a {c.get('status')} contract.", c)

    fine = c.get("fine", 0)
    sym = settings.CURRENCY_SYMBOL

    # Atomic check-and-deduct: a concurrent spend must not slip the give-up through on
    # funds that are no longer there. The fine is charged regardless of who issued the
    # contract — it is the agreed penalty, not a transfer.
    if fine and not await store.try_debit(gid, int(actor_id), fine):
        return _fail(NO_FUNDS, f"You need {fine} {sym} to pay the fine and give up.", c)

    await _pay_issuer(gid, c, fine + c.get("payment", 0))
    cdb.update_contract(gid, contract_id, status=cdb.CANCELLED,
                        completed_at=datetime.utcnow().isoformat())
    c["status"] = cdb.CANCELLED
    log.info("Contract %s given up by %s (fine %d to issuer %s)",
             contract_id, actor_name, fine, c.get("issuer_id"))

    if not _is_bot_issued(c):
        _notify(gid, int(c["issuer_id"]), "contract_cancelled", "🏳️ Contract Given Up",
                f"{c.get('contractor_name', actor_name)} gave up on \"{c['mission'][:80]}\""
                + (f" and paid the {fine} {sym} fine." if fine else "."), contract_id)

    await restore_rescue(gid, contract_id, c)

    msg = f"Contract given up. You paid the {fine} {sym} fine." if fine else "Contract given up."
    return Result(ok=True, message=msg, contract=c)


# ── Review ───────────────────────────────────────────────────────────────────

async def review(gid: int, contract_id: str, *, actor_id: int, actor_name: str,
                 approve: bool) -> Result:
    """Issuer approves a submission (→ completed, contractor paid) or refuses it
    (→ disputed, and the contractor is handed the dispute options)."""
    c, err = _load(gid, contract_id)
    if err:
        return err

    if str(c.get("issuer_id")) != str(actor_id):
        return _fail(FORBIDDEN, "Only the contract issuer can review submissions.", c)

    status = c.get("status")
    # Approving is also allowed on a *disputed* contract — the issuer changing their
    # mind and taking the work after all. Nothing else in the dispute closes it in the
    # contractor's favour: settle needs the issuer anyway, sue costs the moderators'
    # time, and the auto-fine clock only ever ends it against them. It is the same
    # transition with the same effects, so it is this function rather than a second
    # copy of the payment, rescue-delivery and flag-delivery paths.
    #
    # Refusing is not, because a disputed contract has already been refused.
    #
    # `mod_review` is excluded on purpose: once it has been escalated, resolving it is
    # the moderators' call, and an issuer closing it underneath them would leave their
    # ticket holding buttons that no longer do anything.
    was_disputed = status == cdb.DISPUTED
    if approve and status not in (cdb.SUBMITTED, cdb.DISPUTED):
        return _fail(BAD_STATE, "Contract is not awaiting review.", c)
    if not approve and status != cdb.SUBMITTED:
        return _fail(BAD_STATE, "Contract is not awaiting review.", c)

    contractor_id = int(c["contractor_id"])
    sym = settings.CURRENCY_SYMBOL

    if not approve:
        fields = open_dispute_fields()
        cdb.update_contract(gid, contract_id, **fields)
        c.update(fields)
        days = settings.DISPUTE_AUTO_FINE_DAYS
        _notify(gid, contractor_id, "review_result", "⚠️ Submission Refused",
                f"Your submission for \"{c['mission'][:80]}\" was refused. Settle it, "
                f"ask for more time, pay the fine or sue. If nothing happens within "
                f"{days} days, the {c.get('fine', 0)} {sym} fine is collected "
                "automatically.", contract_id)
        await _dm_dispute_options(gid, contract_id, contractor_id)
        log.info("Contract %s submission refused by %s", contract_id, actor_name)
        return Result(ok=True, message="Submission refused. Dispute opened on Discord.",
                      contract=c)

    # pending_request goes in the same write as the status: a settle or extension the
    # contractor was still waiting on is moot once the work has been accepted, and a
    # closed contract must never advertise an answerable request. This also takes the
    # contract off the auto-fine clock, since that only looks at disputed ones.
    cdb.update_contract(gid, contract_id, status=cdb.COMPLETED,
                        completed_at=datetime.utcnow().isoformat(),
                        pending_request=None)
    c["status"] = cdb.COMPLETED
    _clear_request(c)
    await store.add_balance(gid, contractor_id, c["payment"])
    _notify(gid, contractor_id, "review_result", "✅ Mission Approved!",
            (f"{c.get('issuer_name', 'The issuer')} dropped the dispute on "
             f"\"{c['mission'][:80]}\" and accepted your work. "
             if was_disputed else
             f"Your submission for \"{c['mission'][:80]}\" was approved. ")
            + f"+{c['payment']} {sym} paid.", contract_id)
    await _dm_review_approved(gid, contractor_id, c)

    # Rescue: return the kerbals and the craft carrying them to the issuer. This also
    # credits the rescuer's completed-rescue stat, so it must not be done twice.
    if c.get("mission_type") == cdb.RESCUE:
        try:
            await _api()._deliver_rescue_craft(gid, contract_id, c)
        except Exception as exc:
            log.error("Rescue %s approved but delivery failed: %s", contract_id, exc)
        _notify(gid, contractor_id, "rescue_craft_removed", "🚀 Rescue Craft Transferred",
                "Your rescue craft and the rescued kerbals were delivered to the issuer.",
                contract_id)

    # Flag design: the full-res flag was gated behind approval; queue it for the
    # issuer's in-game flag picker now that it is paid for.
    if c.get("mission_type") == cdb.FLAG_DESIGN and c.get("flag_fullres_url"):
        imp.enqueue(gid, int(c["issuer_id"]), source="flag", ref_id=contract_id,
                    craft_name=c["mission"], flag_url=c["flag_fullres_url"],
                    craft_filename=c.get("flag_filename") or "flag.png")
        _notify(gid, int(c["issuer_id"]), "flag_delivered", "🚩 Flag Delivered",
                "Your custom flag is queued. Open KSP at the Space Center to install it "
                "into your flag picker.", contract_id)

    log.info("Contract %s submission approved by %s%s", contract_id, actor_name,
             " (dispute dropped)" if was_disputed else "")
    return Result(
        ok=True, contract=c,
        message=("Dispute dropped and submission accepted. Payment released."
                 if was_disputed else "Submission approved! Payment released."))


# ── Dispute ──────────────────────────────────────────────────────────────────

DISPUTE_ACTIONS = ("pay_fine", "sue", "settle", "more_time")


async def dispute(gid: int, contract_id: str, *, actor_id: int, actor_name: str,
                  action: str, new_date: str = "") -> Result:
    """Contractor resolves a refused submission: settle / more_time / pay_fine / sue.

    The two actions that need the other party's consent (settle, and more_time on a
    human-issued contract) are *requests* — they hand off to a Discord approval view
    and change nothing until the issuer answers.
    """
    c, err = _load(gid, contract_id)
    if err:
        return err

    if str(c.get("contractor_id")) != str(actor_id):
        return _fail(FORBIDDEN, "Only the contractor can resolve this dispute.", c)
    if c.get("status") != cdb.DISPUTED:
        return _fail(BAD_STATE, "Contract is not in dispute.", c)

    action = (action or "").lower().strip()
    if action not in DISPUTE_ACTIONS:
        return _fail(BAD_REQUEST, f"Unknown dispute action: {action}", c)

    sym = settings.CURRENCY_SYMBOL
    contractor_id = int(c["contractor_id"])

    # ── Pay Fine ── the contractor concedes: fine to the issuer, escrow released.
    if action == "pay_fine":
        fine = c.get("fine", 0)
        if fine and not await store.try_debit(gid, contractor_id, fine):
            return _fail(NO_FUNDS, "Insufficient balance to pay the fine.", c)
        await _pay_issuer(gid, c, fine + c.get("payment", 0))
        cdb.update_contract(gid, contract_id, status=cdb.COMPLETED,
                            completed_at=datetime.utcnow().isoformat(),
                            pending_request=None)
        c["status"] = cdb.COMPLETED
        _clear_request(c)
        if not _is_bot_issued(c):
            _notify(gid, int(c["issuer_id"]), "review_result", "💰 Fine Paid",
                    f"{c.get('contractor_name', actor_name)} paid the fine for "
                    f"\"{c['mission'][:80]}\". +{fine + c.get('payment', 0)} {sym}.",
                    contract_id)
        # The kerbals were never brought home — hand the issuer their wreck back.
        await restore_rescue(gid, contract_id, c)
        log.info("Contract %s: %s paid the fine", contract_id, actor_name)
        return Result(ok=True, message="Fine paid. Contract closed.", contract=c)

    # ── Sue ── escalate to moderators.
    if action == "sue":
        posted = await _escalate_to_mods(gid, contract_id, c, opener_id=contractor_id)
        if not posted:
            return _fail(UNAVAILABLE, "Moderator review is not configured.", c)
        cdb.update_contract(gid, contract_id, status=cdb.MOD_REVIEW, pending_request=None)
        c["status"] = cdb.MOD_REVIEW
        _clear_request(c)
        log.info("Contract %s escalated to moderators by %s", contract_id, actor_name)
        return Result(ok=True, message="Case escalated to moderators.", contract=c)

    # ── Settle ── ask the issuer to drop the contract with no exchange.
    if action == "settle":
        if _is_bot_issued(c):
            return _fail(BAD_REQUEST, "AI contracts cannot be settled.", c)
        _open_request(gid, contract_id, c, REQUEST_SETTLE)
        _notify(gid, int(c["issuer_id"]), "dispute_request", "🤝 Settlement Requested",
                f"{c.get('contractor_name', actor_name)} asks to settle "
                f"\"{c['mission'][:80]}\" with no exchange.", contract_id)
        # Best-effort: the request already exists on the contract, so a failed DM costs
        # the issuer a notification, not the ability to answer.
        await _dm_settle_request(gid, contract_id, c)
        log.info("Contract %s: %s requested a settlement", contract_id, actor_name)
        return Result(ok=True, message="Settlement request sent to the issuer.", contract=c)

    # ── More Time ── bot contracts extend themselves; human ones need approval.
    #
    # Capped per dispute. Uncapped, a refused extension could be followed by another
    # with a slightly different date, forever — stalling by another name, and the exact
    # behaviour the auto-fine clock exists to prevent. Getting refused again after an
    # extension was granted opens a fresh dispute, which restores the allowance.
    used = int(c.get("more_time_requests") or 0)
    if used >= settings.DISPUTE_MAX_MORE_TIME_REQUESTS:
        return _fail(BAD_STATE,
                     "You have already asked for more time on this dispute. Settle, "
                     "pay the fine or sue.", c)

    if _is_bot_issued(c):
        end_of_week = _end_of_week()
        cdb.update_contract(gid, contract_id, due_date=end_of_week, status=cdb.ACTIVE,
                            pending_request=None, more_time_requests=used + 1)
        c["status"] = cdb.ACTIVE
        c["due_date"] = end_of_week
        c["more_time_requests"] = used + 1
        _clear_request(c)
        log.info("Contract %s auto-extended to %s for %s", contract_id, end_of_week, actor_name)
        return Result(ok=True, message=f"Deadline extended to {end_of_week}. Submit again!",
                      contract=c, data={"new_date": end_of_week})

    new_date = (new_date or "").strip()
    if not _valid_future_date(new_date):
        return _fail(BAD_REQUEST, "A valid future date (YYYY-MM-DD) is required.", c)
    # Counted on the *ask*, not on the answer: a refused request has still been used up,
    # which is the whole point of the cap.
    cdb.update_contract(gid, contract_id, more_time_requests=used + 1)
    c["more_time_requests"] = used + 1
    _open_request(gid, contract_id, c, REQUEST_MORE_TIME, new_date)
    _notify(gid, int(c["issuer_id"]), "dispute_request", "⏰ Extension Requested",
            f"{c.get('contractor_name', actor_name)} asks to move the deadline of "
            f"\"{c['mission'][:80]}\" to {new_date}.", contract_id)
    await _dm_more_time_request(gid, contract_id, c, new_date)
    log.info("Contract %s: %s requested more time (%s)", contract_id, actor_name, new_date)
    return Result(ok=True, message="Time extension request sent to the issuer.", contract=c,
                  data={"new_date": new_date})


# ── Issuer answers to a contractor's request ─────────────────────────────────
#
# Both require an *open request of the matching kind*, not merely a disputed contract.
# Without that check the issuer could unilaterally cancel a contract nobody offered to
# settle, or extend a deadline nobody asked to move — neither is theirs to do alone.

def _open_request_of(c: dict, kind: str) -> dict | None:
    req = c.get("pending_request") or None
    return req if req and req.get("kind") == kind else None


async def settle_response(gid: int, contract_id: str, *, actor_id: int, actor_name: str,
                          approve: bool) -> Result:
    """Issuer answers a settlement request: drop the contract with no exchange."""
    c, err = _load(gid, contract_id)
    if err:
        return err

    if str(c.get("issuer_id")) != str(actor_id):
        return _fail(FORBIDDEN, "Only the contract issuer can answer a settlement request.", c)
    if c.get("status") != cdb.DISPUTED:
        return _fail(BAD_STATE, "This contract is no longer in dispute.", c)
    if _open_request_of(c, REQUEST_SETTLE) is None:
        return _fail(BAD_STATE, "There is no open settlement request on this contract.", c)

    if not approve:
        cdb.update_contract(gid, contract_id, pending_request=None)
        _clear_request(c)
        _notify(gid, int(c["contractor_id"]), "review_result", "❌ Settlement Refused",
                f"The issuer refused to settle \"{c['mission'][:80]}\".", contract_id)
        log.info("Contract %s: settlement refused by %s", contract_id, actor_name)
        return Result(ok=True, message="Settlement refused.", contract=c)

    cdb.update_contract(gid, contract_id, status=cdb.CANCELLED, pending_request=None)
    c["status"] = cdb.CANCELLED
    _clear_request(c)
    await _pay_issuer(gid, c, c.get("payment", 0))
    _notify(gid, int(c["contractor_id"]), "review_result", "🤝 Settled",
            f"\"{c['mission'][:80]}\" was settled. No fine, no payment.", contract_id)
    # Settled means the rescue never happened — return the issuer's stranded vessel.
    await restore_rescue(gid, contract_id, c)
    log.info("Contract %s settled by %s", contract_id, actor_name)
    return Result(ok=True, message="Settled. Escrow refunded.", contract=c)


async def more_time_response(gid: int, contract_id: str, *, actor_id: int, actor_name: str,
                             approve: bool, new_date: str = "") -> Result:
    """Issuer answers a deadline-extension request.

    The date comes from the stored request, **not** from the caller: approving has to
    mean approving what was asked for, and a front end that supplied its own date could
    grant an extension the contractor never requested (or a shorter one). `new_date` is
    only consulted for contracts whose request predates this field, where the Discord
    button recovers it from the embed.
    """
    c, err = _load(gid, contract_id)
    if err:
        return err

    if str(c.get("issuer_id")) != str(actor_id):
        return _fail(FORBIDDEN, "Only the contract issuer can answer an extension request.", c)
    if c.get("status") != cdb.DISPUTED:
        return _fail(BAD_STATE, "This contract is no longer in dispute.", c)

    req = _open_request_of(c, REQUEST_MORE_TIME)
    if req is None and not new_date:
        return _fail(BAD_STATE, "There is no open extension request on this contract.", c)

    if not approve:
        cdb.update_contract(gid, contract_id, pending_request=None)
        _clear_request(c)
        _notify(gid, int(c["contractor_id"]), "review_result", "❌ Extension Refused",
                f"Your extension request for \"{c['mission'][:80]}\" was refused.", contract_id)
        log.info("Contract %s: extension refused by %s", contract_id, actor_name)
        return Result(ok=True, message="Extension refused.", contract=c)

    granted = ((req or {}).get("new_date") or new_date or "").strip()
    if not _valid_future_date(granted):
        return _fail(BAD_REQUEST, "A valid future date (YYYY-MM-DD) is required.", c)

    cdb.update_contract(gid, contract_id, due_date=granted, status=cdb.ACTIVE,
                        pending_request=None)
    c["status"] = cdb.ACTIVE
    c["due_date"] = granted
    _clear_request(c)
    _notify(gid, int(c["contractor_id"]), "review_result", "⏰ Deadline Extended",
            f"\"{c['mission'][:80]}\" is active again, due {granted}.", contract_id)
    log.info("Contract %s extended to %s by %s", contract_id, granted, actor_name)
    return Result(ok=True, message=f"Deadline extended to {granted}.", contract=c,
                  data={"new_date": granted})


# ── The dispute clock ────────────────────────────────────────────────────────

async def expire_dispute(gid: int, contract_id: str) -> Result:
    """Collect the fine on a dispute nobody resolved in time.

    Called by the sweep in `cogs/contracts.py`, never by a player, so it takes no actor
    — the *absence* of an action is what triggers it.

    Uses `debit_up_to` rather than `try_debit`, deliberately. A contractor who cannot
    cover the fine would otherwise fail this check forever and keep the contract parked
    in dispute, which is precisely the stall being closed. Taking what they have and
    closing it matches what a moderator does with Enforce Fine.
    """
    c, err = _load(gid, contract_id)
    if err:
        return err
    if c.get("status") != cdb.DISPUTED:
        return _fail(BAD_STATE, "Contract is not in dispute.", c)

    deadline = auto_fine_at(c)
    if deadline is None:
        # No stamp — a dispute opened before the clock existed. Start it now rather than
        # fining instantly for time that was never counted.
        now = datetime.utcnow().isoformat()
        cdb.update_contract(gid, contract_id, disputed_at=now)
        c["disputed_at"] = now
        log.info("Contract %s had no disputed_at; clock started now.", contract_id)
        return _fail(BAD_STATE, "Dispute clock started.", c)
    if datetime.utcnow() < deadline:
        return _fail(BAD_STATE, "Dispute has not timed out yet.", c)

    sym = settings.CURRENCY_SYMBOL
    fine = c.get("fine", 0)
    collected = await store.debit_up_to(gid, int(c["contractor_id"]), fine) if fine else 0
    await _pay_issuer(gid, c, collected + c.get("payment", 0))
    cdb.update_contract(gid, contract_id, status=cdb.COMPLETED,
                        completed_at=datetime.utcnow().isoformat(),
                        pending_request=None, closed_by="dispute_timeout")
    c["status"] = cdb.COMPLETED
    _clear_request(c)

    _notify(gid, int(c["contractor_id"]), "review_result", "⏱️ Dispute Timed Out",
            f"\"{c['mission'][:80]}\" sat in dispute for "
            f"{settings.DISPUTE_AUTO_FINE_DAYS} days, so the fine was collected "
            f"automatically. -{collected} {sym}.", contract_id)
    if not _is_bot_issued(c):
        _notify(gid, int(c["issuer_id"]), "review_result", "⏱️ Dispute Timed Out",
                f"The dispute on \"{c['mission'][:80]}\" expired. You received "
                f"{collected + c.get('payment', 0)} {sym}.", contract_id)

    await restore_rescue(gid, contract_id, c)
    log.info("Contract %s: dispute timed out, collected %d of %d fine",
             contract_id, collected, fine)
    return Result(ok=True, message=f"Dispute timed out. Fine collected ({collected}).",
                  contract=c, data={"fine_collected": collected})


# ── Moderator resolution ─────────────────────────────────────────────────────

async def mod_resolve(gid: int, contract_id: str, *, actor_id: int, actor_name: str,
                      enforce: bool) -> Result:
    """Moderator closes an escalated dispute.

    **Authorization is the caller's job here** — being a moderator is a Discord role
    fact this module cannot see, and unlike every other function the actor is not a
    party to the contract. `cogs.contract_views` gates it with `perms.is_mod_user`;
    any future front end must gate it too. The `actor_id`/`actor_name` here are for
    the audit log, not a check.
    """
    c, err = _load(gid, contract_id)
    if err:
        return err
    if c.get("status") != cdb.MOD_REVIEW:
        return _fail(BAD_STATE, "This contract is not under moderator review.", c)

    payment = c.get("payment", 0)

    if not enforce:
        cdb.update_contract(gid, contract_id, status=cdb.CANCELLED)
        c["status"] = cdb.CANCELLED
        await _pay_issuer(gid, c, payment)
        _notify(gid, int(c["contractor_id"]), "review_result", "⚖️ Fine Cancelled",
                f"Moderators cancelled the fine for \"{c['mission'][:80]}\".", contract_id)
        await restore_rescue(gid, contract_id, c)
        log.info("Contract %s: %s cancelled the fine", contract_id, actor_name)
        return Result(ok=True, message="Fine cancelled. Escrow refunded.", contract=c,
                      data={"fine_collected": 0})

    # Take whatever the contractor can actually pay and pass exactly that on, so the
    # issuer is never credited coins that were never debited.
    collected = await store.debit_up_to(gid, int(c["contractor_id"]), c.get("fine", 0))
    await _pay_issuer(gid, c, collected + payment)
    cdb.update_contract(gid, contract_id, status=cdb.COMPLETED,
                        completed_at=datetime.utcnow().isoformat())
    c["status"] = cdb.COMPLETED
    _notify(gid, int(c["contractor_id"]), "review_result", "⚖️ Fine Enforced",
            f"Moderators enforced the fine for \"{c['mission'][:80]}\". "
            f"-{collected} {settings.CURRENCY_SYMBOL}.", contract_id)
    await restore_rescue(gid, contract_id, c)
    log.info("Contract %s: %s enforced the fine (%d collected)", contract_id, actor_name, collected)
    return Result(ok=True, message=f"Fine enforced ({collected}). Escrow refunded.",
                  contract=c, data={"fine_collected": collected})


# ── Date helpers ─────────────────────────────────────────────────────────────

def _valid_future_date(s: str) -> bool:
    """YYYY-MM-DD and strictly after today. The classic Discord modal already checked
    the second half; the API only checked the format, so a contractor could 'extend' a
    deadline into the past."""
    try:
        d = datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False
    return d > datetime.now(timezone(timedelta(hours=3))).date()


def _end_of_week() -> str:
    """Next Sunday in UTC+3 — the timezone the weekly mission cycle runs on."""
    now = datetime.now(timezone(timedelta(hours=3)))
    days_to_sunday = 6 - now.weekday()
    if days_to_sunday <= 0:
        days_to_sunday = 7
    return (now + timedelta(days=days_to_sunday)).strftime("%Y-%m-%d")


# ── Discord hand-offs ────────────────────────────────────────────────────────
#
# Some outcomes need the *other* party to answer, and the only place every player is
# reachable is Discord. These are best-effort by design: a message that cannot be
# delivered must not roll back a state change that already happened, so each returns a
# bool and the callers above decide whether that mattered. `discord` is imported inside
# them because this module is also imported by the API server, which must keep working
# if the bot handle is not up yet.

def _bot():
    return _api()._bot_instance


async def deliver_to_player(gid: int, user_id: int, *, content: str | None = None,
                            embed=None, view=None):
    """Deliver a contract message to a player: their corp channel first, DM fallback.

    Corp channels are private (owner + members + mods), so this is no less
    confidential than a DM — but unlike a DM it works for players who block DMs,
    and it keeps a player's contract paperwork in one place. A channel post does
    not notify by itself, so the player is mentioned; the DM fallback drops the
    mention, which is redundant there. Returns the sent message, or None when
    neither surface worked.
    """
    bot = _bot()
    if bot is None:
        return None
    from cogs.corps import find_user_corp
    mention = f"<@{int(user_id)}>"
    try:
        corp = find_user_corp(gid, int(user_id))
        ch_id = int(corp.get("channel_id") or 0) if corp else 0
        if ch_id:
            # The corp may live in another guild — resolve at the bot level.
            channel = bot.get_channel(ch_id)
            if channel is None:
                channel = await bot.fetch_channel(ch_id)
            return await channel.send(content=f"{mention} {content}" if content else mention,
                                      embed=embed, view=view)
    except Exception as exc:
        log.warning("Corp-channel delivery to %s failed, trying DM: %s", user_id, exc)
    try:
        u = bot.get_user(int(user_id)) or await bot.fetch_user(int(user_id))
        return await u.send(content=content, embed=embed, view=view)
    except Exception as exc:
        log.warning("Could not DM %s: %s", user_id, exc)
        return None


async def _dm_dispute_options(gid: int, contract_id: str, contractor_id: int) -> bool:
    try:
        import discord
        from i18n import t
        from cogs.contract_views import DisputeView
        e = discord.Embed(title=f"⚠️ {t(gid, 'ct.disputed')}",
                          description=t(gid, 'ct.disputed_desc'),
                          color=discord.Color.orange())
        msg = await deliver_to_player(gid, contractor_id, embed=e,
                                      view=DisputeView(contract_id, gid))
        return msg is not None
    except Exception as exc:
        log.warning("Could not deliver dispute options for %s: %s", contract_id, exc)
        return False


async def _dm_review_approved(gid: int, contractor_id: int, c: dict) -> bool:
    try:
        import discord
        from i18n import t
        e = discord.Embed(title=f"✅ {t(gid, 'ct.accepted')}",
                          description=t(gid, 'ct.accepted_desc', payment=c['payment'],
                                        sym=settings.CURRENCY_SYMBOL),
                          color=discord.Color.green())
        msg = await deliver_to_player(gid, contractor_id, embed=e)
        return msg is not None
    except Exception as exc:
        log.warning("Could not deliver approval for %s: %s", c.get("contract_id"), exc)
        return False


async def _dm_settle_request(gid: int, contract_id: str, c: dict) -> bool:
    try:
        import discord
        from i18n import t
        from cogs.contract_views import SettleApprovalView
        e = discord.Embed(title=f"🤝 {t(gid, 'ct.settle_request')}",
                          description=t(gid, 'ct.settle_desc', name=c['contractor_name']),
                          color=discord.Color.light_grey())
        msg = await deliver_to_player(gid, int(c["issuer_id"]), embed=e,
                                      view=SettleApprovalView(contract_id, gid))
        return msg is not None
    except Exception as exc:
        log.warning("Could not send settle request for %s: %s", contract_id, exc)
        return False


async def _dm_more_time_request(gid: int, contract_id: str, c: dict, new_date: str) -> bool:
    try:
        import discord
        from i18n import t
        from cogs.contract_views import MoreTimeApprovalView
        e = discord.Embed(title=f"⏰ {t(gid, 'ct.moretime_request')}",
                          description=t(gid, 'ct.moretime_desc', name=c['contractor_name'],
                                        old=c['due_date'], new=new_date),
                          color=discord.Color.blue())
        msg = await deliver_to_player(gid, int(c["issuer_id"]), embed=e,
                                      view=MoreTimeApprovalView(contract_id, gid, new_date))
        return msg is not None
    except Exception as exc:
        log.warning("Could not send more-time request for %s: %s", contract_id, exc)
        return False


async def _escalate_to_mods(gid: int, contract_id: str, c: dict, *, opener_id: int) -> bool:
    """Post the case for moderator review, preferring a private ticket (both parties
    plus mods) and falling back to the shared mod channel.

    Returns False when neither is configured — the caller then leaves the contract in
    dispute rather than parking it in `mod_review` where nobody would ever see it.
    """
    bot = _bot()
    if bot is None:
        return False
    try:
        import discord
        from i18n import t
        from cogs.contract_views import ModReviewView, _embed

        e = _embed(c, gid)
        e.title = f"⚖️ {t(gid, 'ct.mod_review')}"
        e.color = discord.Color.purple()
        # Why the submission was refused, so mods can judge whether the refusal was wrong.
        reason = c.get("review_reason")
        if reason:
            e.add_field(name="Refusal Reason", value=str(reason)[:1024], inline=False)
        files = c.get("submitted_files", [])
        if files:
            e.add_field(name="📁 Submitted Files",
                        value="\n".join(f"📎 [{f['filename']}]({f['url']})" for f in files),
                        inline=False)

        if guild_config.get_channel_id(gid, "ticket_category"):
            try:
                from cogs.tickets import create_ticket
                guild = bot.get_guild(gid)
                if guild is not None:
                    other_id = (int(c["issuer_id"]) if str(opener_id) == str(c.get("contractor_id"))
                                else int(c["contractor_id"]))
                    ch = await create_ticket(
                        bot, guild, opener_id=opener_id, kind="other",
                        title="Contract dispute (escalated)",
                        description=f"<@{opener_id}> escalated contract `{contract_id}` "
                                    "for moderator review.",
                        color=discord.Color.purple(),
                        extra_user_ids=[other_id],
                        extra_embeds=[e],
                        extra_view=ModReviewView(contract_id, gid),
                    )
                    if ch is not None:
                        return True
            except Exception as exc:
                log.warning("Could not open sue ticket for %s: %s", contract_id, exc)

        ch = guild_config.resolve_channel(bot, gid, "contract_mod")
        if ch is None:
            return False
        await ch.send(embed=e, view=ModReviewView(contract_id, gid))
        return True
    except Exception as exc:
        log.warning("Could not escalate contract %s to mods: %s", contract_id, exc)
        return False
