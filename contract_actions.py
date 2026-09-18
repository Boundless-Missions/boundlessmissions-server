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

import asyncio
import functools
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import settings
from data import contracts as cdb
from data import guild_config
from data import imports as imp
from data.store import store
import rewards

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
UNAVAILABLE = "unavailable"
USE_GIVE_UP = "use_give_up"
DEBT_LIMIT = "debt_limit"


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


# ── One transition at a time per contract ────────────────────────────────────
#
# Several transitions await a wallet operation between reading the status and
# writing the new one (give_up, pay_fine, the dispute clock, a moderator's
# enforce). Today those awaits never actually yield — `store`'s lock is never
# contended at an await point and firebase-admin calls block the loop — so two
# copies could not interleave and double-pay. That is an accident of the current
# code, not a property anyone chose: the first real await added before a status
# write (a Discord DM, `asyncio.to_thread` around a Firestore call) reopens the
# double payout that `api_server`'s submit lock closed. So every transition holds
# a per-contract lock, the same shape as `_submit_locks`. In-process only, like
# that one: the API is a single process.

_locks: dict[str, asyncio.Lock] = {}


class contract_lock:
    """`async with contract_lock(contract_id):` — the per-contract lock every
    transition runs under. Exposed so `api_server.submit_contract` holds the SAME
    lock rather than a private one keyed on the same id: two lock namespaces that
    never coordinate let a submit and a cancel of one contract interleave, which
    resurrected a refunded contract as SUBMITTED and paid it a second time.
    Non-reentrant — nothing under it may call a `@serialized` function."""

    def __init__(self, contract_id):
        self.key = str(contract_id)
        self.lock: asyncio.Lock | None = None

    async def __aenter__(self):
        lock = _locks.get(self.key)
        if lock is None:
            lock = _locks[self.key] = asyncio.Lock()
        self.lock = lock
        await lock.acquire()
        return self

    async def __aexit__(self, *exc):
        lock = self.lock
        lock.release()
        if not lock.locked() and not getattr(lock, "_waiters", None):
            _locks.pop(self.key, None)
        return False


def serialized(fn):
    """Run a `(gid, contract_id, ...)` transition under that contract's lock."""
    @functools.wraps(fn)
    async def wrapper(gid, contract_id, *args, **kwargs):
        async with contract_lock(contract_id):
            return await fn(gid, contract_id, *args, **kwargs)
    return wrapper


# ── Shared pieces ────────────────────────────────────────────────────────────

def _load(gid: int, contract_id: str):
    """(contract, None) or (None, failure Result)."""
    c = cdb.get_contract(gid, contract_id)
    if not c:
        return None, _fail(NOT_FOUND, "Contract not found.")
    return c, None


def _is_bot_issued(c: dict) -> bool:
    return str(c.get("issuer_id")) == str(_api()._get_bot_user_id())


def _contract_label(c: dict) -> str:
    """A one-line name for this contract, for a ledger entry to be readable by.

    The mission text is what the player recognises — a contract id is what *we*
    recognise — so it leads, cut to a phrase and falling back to the id only when
    there is no text at all. The store truncates again on write; this cut is here
    so the truncation happens at a word rather than mid-sentence.
    """
    return store.tx_detail(c.get("mission"),
                           fallback="Contract " + str(c.get("contract_id") or "")[:12])


def _notify(gid: int, user_id: int, notif_type: str, title: str, message: str,
            contract_id: str) -> None:
    """Best-effort in-game notification. Never let a notification failure roll back a
    transition that has already moved money — the state change is the important half."""
    try:
        _api()._create_notification(gid, str(user_id), notif_type, title, message,
                                    {"contract_id": contract_id})
    except Exception as exc:
        log.warning("Could not notify %s about contract %s: %s", user_id, contract_id, exc)


async def _pay_issuer(gid: int, c: dict, *, refund: int = 0, income: int = 0) -> None:
    """Credit the issuer, unless the issuer is the bot — weekly/AI contracts have no
    wallet to pay into, and crediting one inflates a balance nobody spends.

    The two halves are separated because only one of them is *earnings*. `refund` is
    the issuer's own escrowed payment coming back, which garnishment must never touch
    — an issuer who owes a fine elsewhere would otherwise lose half their own stake
    every time a contract fell through. `income` is the fine they received, which is.
    """
    if _is_bot_issued(c):
        return
    uid = str(c["issuer_id"])
    # An issuer who ran the delete-my-data flow has no record, and `add_balance`
    # would mint one — a ghost `users/{id}` document holding the refund, flushed
    # to Firestore by the next auto-save. The account chose to leave, and the
    # coins were theirs; there is nowhere to put them, so they are not put anywhere.
    if not store.has_user(uid):
        log.info("Contract %s: issuer %s has no account record; %d refund / %d income "
                 "not credited", c.get("contract_id"), uid, refund, income)
        return
    label = _contract_label(c)
    if refund > 0:
        await store.add_balance(gid, uid, refund, category=store.TX_CONTRACT_REFUND,
                                detail=label)
    if income > 0:
        await store.add_balance(gid, uid, income, garnishable=True,
                                category=store.TX_FINE_RECEIVED, detail=label,
                                counterparty=str(c.get("contractor_id") or ""))


async def _charge_fine(gid: int, c: dict, contractor_id: str, fine: int) -> tuple[int, int]:
    """Collect what the contractor can pay and record the rest as a debt to the issuer.

    Returns (collected, owed). Every fine in the system is collected through here, so
    the partial-payment rule and the debt it leaves are stated once rather than in each
    of the four paths that can charge one.

    A bot-issued contract still creates a debt — the deterrent is the point — but files
    it under an empty creditor id, since there is no wallet on the other side. See
    `store.add_debt`.
    """
    if fine <= 0:
        return 0, 0
    collected = await store.debit_up_to(
        gid, contractor_id, fine, category=store.TX_CONTRACT_FINE,
        detail=_contract_label(c),
        counterparty="" if _is_bot_issued(c) else str(c.get("issuer_id") or ""))
    owed = fine - collected
    if owed > 0:
        creditor = "" if _is_bot_issued(c) else str(c["issuer_id"])
        total = await store.add_debt(gid, contractor_id, creditor, owed)
        log.info("Contract %s: %s owes %d of the %d fine (debt now %d)",
                 c.get("contract_id"), contractor_id, owed, fine, total)
    return collected, owed


def _fine_paid_sentence(collected: int, owed: int, sym: str) -> str:
    """What the *contractor* is told about a fine that was just charged.

    A partial payment has to say so and say what happens next: a player whose balance
    emptied and who was told only "fine paid" learns about the rest when their next
    reward arrives halved, which reads as the economy being broken.
    """
    if collected <= 0 and owed <= 0:
        return ""
    if owed <= 0:
        return f" You paid the {collected} {sym} fine."
    pct = settings.DEBT_GARNISH_PERCENT
    if collected <= 0:
        return (f" You owe {owed} {sym}, which will come out of "
                f"{pct}% of what you earn from here.")
    return (f" You paid {collected} {sym} and owe {owed} {sym}, which will come out of "
            f"{pct}% of what you earn from here.")


def _fine_outcome_sentence(collected: int, owed: int, sym: str) -> str:
    """What the *issuer* is told. Names the shortfall rather than hiding it — they are
    the creditor, and the money arrives later in pieces they would not otherwise be
    able to account for."""
    if collected <= 0 and owed <= 0:
        return "."
    if owed <= 0:
        return f" and paid the {collected} {sym} fine."
    if collected <= 0:
        return (f". They could not cover the {owed} {sym} fine; it is owed to you and "
                f"collected from their later earnings.")
    return (f" and paid {collected} {sym} of the fine. The remaining {owed} {sym} is "
            f"owed to you and collected from their later earnings.")


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
    `more_time_granted` is deliberately NOT here — see `dispute`'s bot branch: it counts
    the extensions a bot contract has *granted itself* over the contract's whole life,
    and resetting it on every reopen is what made that branch loop.
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


def mod_review_deadline(c: dict) -> datetime | None:
    """When this moderator review resolves itself, or None if it is not on the clock.

    Mirrors `auto_fine_at`, including the None case: a contract that entered MOD_REVIEW
    before this stamp existed carries no `mod_review_at`, and the sweep stamps it and
    waits a full window rather than resolving something that has been sitting there
    unmeasured.
    """
    stamped = c.get("mod_review_at")
    if c.get("status") != cdb.MOD_REVIEW or not stamped:
        return None
    try:
        return (datetime.fromisoformat(stamped)
                + timedelta(days=settings.MOD_REVIEW_TIMEOUT_DAYS))
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


def _xp_withheld_sentence(gate: str) -> str:
    """Why a player-issued contract paid no XP — said out loud, for the reason the
    debt and rating-floor code say things out loud: a reward that silently
    vanishes arrives as a bug report rather than as a question."""
    if gate == rewards.XP_GATE_COOLDOWN:
        return (f" No XP: player-contract XP is paid at most once every "
                f"{settings.CONTRACT_XP_COOLDOWN_MINUTES} minutes.")
    if gate == rewards.XP_GATE_DAILY:
        return (f" No XP: you have reached the {settings.CONTRACT_XP_HUMAN_DAILY_MAX} XP "
                f"that player contracts can pay in {settings.CONTRACT_PAIR_WINDOW_HOURS} hours.")
    if gate == rewards.XP_GATE_PAIR:
        return (f" No XP: you and this issuer have completed more than "
                f"{settings.CONTRACT_PAIR_XP_FREE_PER_DAY} contracts between you in "
                f"{settings.CONTRACT_PAIR_WINDOW_HOURS} hours.")
    return ""


def _other_party(c: dict, actor_id) -> str | None:
    """The party who did *not* act, or None when that party is the bot.

    Returned as the account id string: a website account's id is `a_<uid>`, and
    `cancel()` calls this after the status write and the escrow refund have landed,
    so an `int()` here turned a completed cancel into a 500.
    """
    actor = str(actor_id)
    other = c.get("issuer_id") if str(c.get("contractor_id")) == actor else c.get("contractor_id")
    if not other or str(other) == str(_api()._get_bot_user_id()):
        return None
    return str(other)


# ── Gates on taking on work ──────────────────────────────────────────────────
#
# Three ways to become the contractor of an ACTIVE contract: accept an offer, win
# an auction, select a weekly mission. The gates lived only on the first, so a
# debtor over DEBT_MAX_OUTSTANDING (or a player at MAX_ACTIVE_CONTRACTS_PER_USER)
# who was refused an offer kept taking obligations through the other two. Stated
# once here, with one wording, so a refusal reads the same wherever it lands.

def debt_limit_refusal(gid: int, user_id) -> str | None:
    """The sentence refusing `user_id` new work over the debt cap, or None.

    Not a lockout — garnishment is already collecting, and shutting a debtor out of
    the contract economy would remove the earnings it collects from. This only stops
    obligations piling up across MAX_ACTIVE_CONTRACTS_PER_USER contracts at once.
    """
    cap = settings.DEBT_MAX_OUTSTANDING
    if cap <= 0:
        return None
    owed = store.debt_total(gid, str(user_id))
    if owed <= cap:
        return None
    return (f"You owe {owed} {settings.CURRENCY_SYMBOL} in unpaid fines "
            f"(limit {cap}). Earn it down before taking on new contracts.")


def active_limit_refusal(gid: int, user_id) -> str | None:
    """The sentence refusing `user_id` an eleventh active contract, or None.
    Same wording as the create endpoints in `api_server`, which check the issuer."""
    cap = settings.MAX_ACTIVE_CONTRACTS_PER_USER
    if cap <= 0 or cdb.count_active(gid, str(user_id)) < cap:
        return None
    return f"Active contract limit reached ({cap})."


def contractor_gate(gid: int, user_id) -> str | None:
    """Both gates, debt first (it is the in-memory one). None means clear to proceed."""
    return debt_limit_refusal(gid, user_id) or active_limit_refusal(gid, user_id)


# ── Offer: accept / cancel ───────────────────────────────────────────────────

@serialized
async def accept(gid: int, contract_id: str, *, actor_id: int, actor_name: str) -> Result:
    """Contractor accepts a pending offer."""
    c, err = _load(gid, contract_id)
    if err:
        return err

    if str(c.get("contractor_id")) != str(actor_id):
        return _fail(FORBIDDEN, "This contract was not offered to you.", c)
    if c.get("status") != cdb.PENDING:
        return _fail(BAD_STATE, "Contract is not pending.", c)

    # See `debt_limit_refusal` for why this is a cap and not a lockout. The same
    # sentence gates auction bids and weekly-mission selection.
    if refusal := debt_limit_refusal(gid, actor_id):
        return _fail(DEBT_LIMIT, refusal, c)

    # And the active-contract cap, which this path never applied. It did not matter
    # while a PENDING offer already counted against the contractor's allowance — but
    # counting it there let a stranger fill a victim's cap with unwanted offers, so
    # it was removed, and this became the only place the contractor side is bounded
    # at all. Without it a player can collect unlimited offers from alts and accept
    # every one, ending with far more live obligations than the cap allows.
    if refusal := active_limit_refusal(gid, actor_id):
        return _fail(BAD_STATE, refusal, c)

    cdb.update_contract(gid, contract_id, status=cdb.ACTIVE)
    c["status"] = cdb.ACTIVE
    log.info("Contract %s accepted by %s", contract_id, actor_name)

    if not _is_bot_issued(c):
        _notify(gid, str(c["issuer_id"]), "contract_accepted", "🤝 Contract Accepted",
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


@serialized
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

    # An issuer withdrawing a contract the contractor has already ACCEPTED pays
    # the agreed fine to the contractor — the mirror of `give_up`. Without it the
    # issuer could preview the renders (`get_submission_preview`), decide the
    # work was not worth paying for, and walk away at zero cost while the
    # contractor's hours went unpaid. Declining a PENDING offer stays free: nobody
    # has done anything yet. A bot issuer never pays (no wallet), and a bot
    # contractor is never paid.
    #
    # The escrow comes back FIRST, and the fine is then collected out of it. In the
    # other order an issuer who kept their spare balance at 0 — everything they had
    # was the escrow — paid nothing now: `debit_up_to` found an empty wallet, the
    # whole fine became a debt, and the refund then landed in a wallet free to
    # spend it, since a refund is non-garnishable by design and a spend is never
    # garnished. The fine that exists to stop "preview the renders, walk away" was
    # unenforced against exactly the issuer most likely to walk. The escrow is the
    # issuer's own money, so refunding it before charging them is not a gift; it
    # only means the fine is paid from what they demonstrably have.
    await _pay_issuer(gid, c, refund=c.get("payment", 0))

    sym = settings.CURRENCY_SYMBOL
    collected = owed = 0
    contractor = str(c.get("contractor_id") or "")
    fine = int(c.get("fine", 0) or 0)
    if (status == cdb.ACTIVE and is_issuer and not is_contractor and fine > 0
            and not _is_bot_issued(c) and contractor
            and contractor != str(_api()._get_bot_user_id())):
        issuer = str(c["issuer_id"])
        label = _contract_label(c)
        collected = await store.debit_up_to(
            gid, issuer, fine, category=store.TX_CONTRACT_FINE, detail=label,
            counterparty=contractor)
        owed = fine - collected
        if owed > 0:
            await store.add_debt(gid, issuer, contractor, owed)
        if collected > 0 and store.has_user(contractor):
            # See `_pay_issuer`: crediting an account that deleted itself mints a ghost
            # `users/{id}` record and un-does the erasure. The coins stay uncredited.
            await store.add_balance(gid, contractor, collected, garnishable=True,
                                    category=store.TX_FINE_RECEIVED, detail=label,
                                    counterparty=issuer)
        elif collected > 0:
            log.info("Contract %s: contractor %s has no account record; %d withdrawal "
                     "fine not credited", contract_id, contractor, collected)

    log.info("Contract %s cancelled by %s (escrow %s refunded to issuer %s, fine %d/%d to contractor)",
             contract_id, actor_name, c.get("payment"), c.get("issuer_id"), collected, fine)

    other = _other_party(c, actor_id)
    if other:
        extra = ""
        if collected or owed:
            extra = (f" You received the {collected} {sym} withdrawal fine."
                     + (f" {owed} {sym} more is owed to you and comes out of their "
                        f"later earnings." if owed else ""))
        _notify(gid, other, "contract_cancelled", "🚫 Contract Cancelled",
                f"{actor_name} cancelled \"{c['mission'][:80]}\"." + extra, contract_id)

    await restore_rescue(gid, contract_id, c)
    msg = "Contract cancelled. Escrow refunded."
    if collected or owed:
        msg = ("Contract withdrawn. Escrow refunded."
               + _fine_paid_sentence(collected, owed, sym).replace("fine", "withdrawal fine", 1))
    return Result(ok=True, message=msg, contract=c,
                  data={"fine_collected": collected, "fine_owed": owed})


@serialized
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

    # Partial collection, not an all-or-nothing check. Refusing a contractor who cannot
    # cover the fine did not protect anything: it left the contract ACTIVE with no way
    # out, while the same player could submit junk, be refused, and have the dispute
    # timeout take exactly this much three days later. So the honest exit was the only
    # blocked one. What is short becomes a debt (see `_charge_fine`).
    # Status FIRST, money second. Every mutator on `store` edits the in-process dict
    # and sets a dirty flag; the only thing here that can raise is the Firestore status
    # write. Charging before writing therefore leaves the one state that must never
    # exist: money moved, contract still non-terminal. It is still counted by
    # `_ESCROW_STATUSES`, so the escrow reads as held after being paid out — and
    # `review(approve=True)` accepts DISPUTED, which would pay the contractor a
    # `payment` that is no longer there. Worse, both sweepers RETRY (30 min for
    # disputes, 30 s for auctions), so the next pass charges the fine and refunds the
    # escrow a second time. This order fails the other way: an unpaid closed contract,
    # which is visible, recoverable and does not duplicate. It is what `cancel`,
    # `settle_response`, `mod_resolve(cancel)`, `mod_reset` and `_auto_accept_contract`
    # already do.
    cdb.update_contract(gid, contract_id, status=cdb.CANCELLED,
                        completed_at=datetime.utcnow().isoformat(),
                        closed_by="give_up")
    c["status"] = cdb.CANCELLED
    collected, owed = await _charge_fine(gid, c, str(actor_id), fine)
    await _pay_issuer(gid, c, refund=c.get("payment", 0), income=collected)
    log.info("Contract %s given up by %s (fine %d to issuer %s)",
             contract_id, actor_name, fine, c.get("issuer_id"))

    if not _is_bot_issued(c):
        _notify(gid, str(c["issuer_id"]), "contract_cancelled", "🏳️ Contract Given Up",
                f"{c.get('contractor_name', actor_name)} gave up on \"{c['mission'][:80]}\""
                + _fine_outcome_sentence(collected, owed, sym), contract_id)

    await restore_rescue(gid, contract_id, c)

    msg = "Contract given up." + _fine_paid_sentence(collected, owed, sym)
    return Result(ok=True, message=msg, contract=c,
                  data={"fine_collected": collected, "fine_owed": owed})


# ── Flag-design submission ───────────────────────────────────────────────────


@serialized
async def submit_flag(gid: int, contract_id: str, *, actor_id, actor_name: str,
                      image: bytes, filename: str,
                      content_type: str = "image/png") -> Result:
    """Contractor hands over the image a `flag_design` contract asked for.

    Submission normally lives in the mod, because what a review is judged against —
    the craft, its mod list, the telemetry — can only be read from a running game.
    A flag is the one deliverable that is *only* an image, so it has no in-game
    upload and never had one: it was a Discord button, and this is that button's
    body with the Discord parts taken out, so the website can offer the same act.

    Two things are load-bearing and were not true of the Discord copy this replaces.
    The flip is `claim_submission`, the same transactional ACTIVE→SUBMITTED the craft
    path uses: real I/O (two Storage uploads) happens between reading the status and
    writing it, and a plain `update` would put SUBMITTED over a contract that was
    cancelled or given up in that window — whose escrow is already refunded — so the
    review that followed would pay it a second time. And the issuer gets a `_notify`
    as well as the Discord embed, so their in-game contract list refreshes; the
    Discord path told only Discord, which is why a flag submitted there was invisible
    to a player reviewing from the sidebar.

    The clean image stays private and is surfaced only once the contract completes
    (i.e. is paid for); what everyone sees until then is the watermarked preview.
    """
    import flag_preview

    c, err = _load(gid, contract_id)
    if err:
        return err

    if (c.get("mission_type") or "") != cdb.FLAG_DESIGN:
        return _fail(BAD_REQUEST,
                     "This contract is not a flag design. Submit it from inside KSP, "
                     "which sends the craft and the telemetry the review is judged "
                     "against.", c)
    if str(c.get("contractor_id")) != str(actor_id):
        return _fail(FORBIDDEN, "This contract is not yours to submit.", c)
    if c.get("status") != cdb.ACTIVE:
        return _fail(BAD_STATE, f"Cannot submit a {c.get('status')} contract.", c)
    if not image:
        return _fail(BAD_REQUEST, "That file was empty.", c)

    # Full-res stays gated: stored PRIVATE (a bare path), surfaced only through a
    # signed URL once the contract completes. Under a server-minted slot
    # (contracts/{cid}/submitted/…) because the name is the contractor's — an
    # upload called `flag_preview.png` would otherwise land exactly where the
    # watermarked preview is written next, and the issuer would review, and pay
    # for, a clean copy they already had.
    try:
        fullres_url = await cdb.upload_submission_file(
            contract_id, str(actor_id), filename, image, content_type, public=False)
        preview_url = await cdb.upload_to_storage(
            contract_id, "flag_preview.png",
            flag_preview.make_watermarked(image), "image/png")
    except Exception as exc:
        log.error("Flag upload failed for %s: %s", contract_id, exc)
        return _fail(UNAVAILABLE, "Could not store that flag. Try again.", c)

    fields = {
        "submitted_files": [],
        "flag_filename": cdb.display_filename(filename, "flag.png"),
        "flag_fullres_url": fullres_url,
        "flag_preview_url": preview_url,
        "submitted_at": datetime.utcnow().isoformat(),
    }
    if not await asyncio.to_thread(cdb.claim_submission, gid, contract_id, fields):
        # The contract moved out of ACTIVE while the two uploads were in flight.
        # The preview is public and the full-res is the deliverable of a contract
        # that no longer wants one, so neither should outlive the failed claim. The
        # preview is deleted by its *path*: `upload_to_storage` hands back a public
        # URL, and `delete_stored_file` refuses anything with a scheme in it — so
        # passing what the upload returned would silently leave the public copy of
        # an unsubmitted flag in the bucket.
        await asyncio.to_thread(cdb.delete_stored_file, fullres_url)
        await asyncio.to_thread(cdb.delete_stored_file,
                                f"contracts/{contract_id}/flag_preview.png")
        return _fail(BAD_STATE, "Contract is no longer active.", c)

    c = cdb.get_contract(gid, contract_id) or dict(c, status=cdb.SUBMITTED, **fields)
    log.info("Contract %s: flag submitted by %s", contract_id, actor_name)

    _notify(gid, str(c["issuer_id"]), "submission_received", "🚩 Flag Submitted",
            f"{c.get('contractor_name', actor_name)} submitted a flag for "
            f"\"{c['mission'][:80]}\"", contract_id)

    # A flag contract is always human-issued (a weekly mission has no flag to want),
    # so there is no AI-review branch here — the issuer decides.
    try:
        import discord
        from cogs.contract_views import _embed, ContractReviewView
        from i18n import t

        e = _embed(c, gid)
        e.title = f"📬 {t(gid, 'ct.review_title')}"
        e.color = discord.Color.orange()
        e.add_field(
            name="🚩 Flag",
            value="Preview is watermarked; the full-res flag is delivered to your "
                  "in-game flag picker on acceptance.",
            inline=False)
        # The id is passed as it is stored, not through `int()`: an issuer who
        # signed up on the website has an account id that is not a snowflake, and
        # coercing it would raise in here and log a delivery failure for something
        # `deliver_to_player` already declines to attempt.
        msg = await deliver_to_player(gid, c["issuer_id"], embed=e,
                                      view=ContractReviewView(contract_id, gid))
        if msg is not None:
            cdb.update_contract(gid, contract_id, issuer_review_msg_id=str(msg.id))
    except Exception as exc:
        log.error("Could not deliver flag review to issuer of %s: %s", contract_id, exc)

    return Result(ok=True, message="Flag submitted for review.", contract=c)


# ── Review ───────────────────────────────────────────────────────────────────

@serialized
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

    contractor_id = str(c["contractor_id"])
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
    await store.add_balance(gid, contractor_id, c["payment"], garnishable=True,
                            category=store.TX_CONTRACT_PAYMENT,
                            detail=_contract_label(c),
                            counterparty=str(c.get("issuer_id") or ""))
    # XP for a human-reviewed contract. The issuer is the sole judge of the work,
    # so two friendly accounts cycling one contract were an XP pump and, through
    # the level-up reward, a mint; `rewards.human_contract_xp` is the gate (cap,
    # cooldown, per-pair limit) and the coins above are untouched by it. A
    # bot-issued contract reviewed here (a moderator acting for the bot) is not
    # a player deal and keeps its plain XP.
    if _is_bot_issued(c):
        xp_due, gate, flag_pair = rewards.contract_xp(c["payment"], bot_issued=True), "", False
    else:
        xp_due, gate, flag_pair = await rewards.human_contract_xp(
            gid, contractor_id, str(c["issuer_id"]), c["payment"])
    if flag_pair:
        try:
            await _api().flag_suspicion(
                gid, contractor_id, c.get("contractor_name", ""), "contract_reciprocity",
                f"{c.get('contractor_name', contractor_id)} and "
                f"{c.get('issuer_name', c.get('issuer_id'))} (`{c.get('issuer_id')}`) have "
                f"completed more than {settings.CONTRACT_PAIR_XP_FREE_PER_DAY} player-issued "
                f"contracts between them in {settings.CONTRACT_PAIR_WINDOW_HOURS} hours "
                f"(latest: \"{c['mission'][:80]}\", {c['payment']} {sym}). XP for the pair "
                f"is withheld; coins still settle.", severity="medium")
        except Exception as exc:  # noqa: BLE001 - a flag must never roll back an approval
            log.warning("Could not flag contract reciprocity on %s: %s", contract_id, exc)
    if gate:
        log.info("Contract %s: XP withheld from %s (%s)", contract_id, contractor_id, gate)
    xp, _leveled = await rewards.grant_xp(gid, contractor_id, xp_due, reason="Mission approved")
    _notify(gid, contractor_id, "review_result", "✅ Mission Approved!",
            (f"{c.get('issuer_name', 'The issuer')} dropped the dispute on "
             f"\"{c['mission'][:80]}\" and accepted your work. "
             if was_disputed else
             f"Your submission for \"{c['mission'][:80]}\" was approved. ")
            + f"+{c['payment']} {sym} paid."
            + (f" +{xp} XP." if xp else _xp_withheld_sentence(gate)), contract_id)
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

    # A bought relay network moves the same way a rescue craft does, and in the same
    # order, which is the part that matters: deliver FIRST, and send the removal notice
    # only if the delivery was actually queued. Telling the contractor's client to
    # delete satellites the issuer never received destroys them for nothing, and
    # nothing gives them back.
    #
    # Bot-issued networks are excluded deliberately: there is no Boundless Missions
    # save for satellites to arrive in, so a weekly relay mission verifies the network
    # and leaves it alone.
    #
    # The notification TYPE is `rescue_craft_removed` because that is the one the
    # client's contract-scoped removal reads (GeneKermanMod.MaybeHandleRescueRemoval →
    # PeekRescueSubmissions → queue every recorded pid). The name is historical; the
    # behaviour behind it was never rescue-specific, it removes whatever this contract
    # recorded as handed over, which is exactly what a network needs.
    if c.get("mission_type") == cdb.CONSTELLATION and not _is_bot_issued(c):
        delivered = False
        try:
            delivered = await _api()._deliver_network_craft(gid, contract_id, c)
        except Exception as exc:
            log.error("Constellation %s approved but network delivery failed: %s",
                      contract_id, exc)
        if delivered:
            _notify(gid, contractor_id, "rescue_craft_removed",
                    "📡 Relay Network Transferred",
                    "The satellites you handed over were delivered to the issuer.",
                    contract_id)
        else:
            log.warning("Constellation %s: no removal notice sent, nothing was delivered.",
                        contract_id)

    # Flag design: the full-res flag was gated behind approval; queue it for the
    # issuer's in-game flag picker now that it is paid for.
    if c.get("mission_type") == cdb.FLAG_DESIGN and c.get("flag_fullres_url"):
        imp.enqueue(gid, str(c["issuer_id"]), source="flag", ref_id=contract_id,
                    craft_name=c["mission"], flag_url=c["flag_fullres_url"],
                    craft_filename=c.get("flag_filename") or "flag.png")
        _notify(gid, str(c["issuer_id"]), "flag_delivered", "🚩 Flag Delivered",
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


@serialized
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
    contractor_id = str(c["contractor_id"])

    # ── Pay Fine ── the contractor concedes: fine to the issuer, escrow released.
    if action == "pay_fine":
        fine = c.get("fine", 0)
        # Partial, for the same reason `give_up` is: refusing a contractor who cannot
        # cover it only parked the contract in dispute until the timeout took this
        # exact amount anyway. Conceding is now always possible; the shortfall is a debt.
        # Status FIRST, money second. Every mutator on `store` edits the in-process dict
        # and sets a dirty flag; the only thing here that can raise is the Firestore status
        # write. Charging before writing therefore leaves the one state that must never
        # exist: money moved, contract still non-terminal. It is still counted by
        # `_ESCROW_STATUSES`, so the escrow reads as held after being paid out — and
        # `review(approve=True)` accepts DISPUTED, which would pay the contractor a
        # `payment` that is no longer there. Worse, both sweepers RETRY (30 min for
        # disputes, 30 s for auctions), so the next pass charges the fine and refunds the
        # escrow a second time. This order fails the other way: an unpaid closed contract,
        # which is visible, recoverable and does not duplicate. It is what `cancel`,
        # `settle_response`, `mod_resolve(cancel)`, `mod_reset` and `_auto_accept_contract`
        # already do.
        cdb.update_contract(gid, contract_id, status=cdb.COMPLETED,
                            completed_at=datetime.utcnow().isoformat(),
                            pending_request=None, closed_by="pay_fine")
        c["status"] = cdb.COMPLETED
        collected, owed = await _charge_fine(gid, c, contractor_id, fine)
        await _pay_issuer(gid, c, refund=c.get("payment", 0), income=collected)
        _clear_request(c)
        if not _is_bot_issued(c):
            _notify(gid, str(c["issuer_id"]), "review_result", "💰 Fine Paid",
                    f"{c.get('contractor_name', actor_name)} settled the fine for "
                    f"\"{c['mission'][:80]}\". +{collected + c.get('payment', 0)} {sym}."
                    + (f" {owed} {sym} is still owed and comes out of their later "
                       f"earnings." if owed else ""),
                    contract_id)
        # The kerbals were never brought home — hand the issuer their wreck back.
        await restore_rescue(gid, contract_id, c)
        log.info("Contract %s: %s paid %d of the %d fine", contract_id, actor_name,
                 collected, fine)
        return Result(ok=True,
                      message="Contract closed." + _fine_paid_sentence(collected, owed, sym),
                      contract=c, data={"fine_collected": collected, "fine_owed": owed})

    # ── Sue ── escalate to moderators.
    if action == "sue":
        posted = await _escalate_to_mods(gid, contract_id, c, opener_id=contractor_id)
        if not posted:
            return _fail(UNAVAILABLE, "Moderator review is not configured.", c)
        _now_iso = datetime.utcnow().isoformat()
        cdb.update_contract(gid, contract_id, status=cdb.MOD_REVIEW,
                            pending_request=None, mod_review_at=_now_iso)
        c["status"] = cdb.MOD_REVIEW
        c["mod_review_at"] = _now_iso
        _clear_request(c)
        log.info("Contract %s escalated to moderators by %s", contract_id, actor_name)
        return Result(ok=True, message="Case escalated to moderators.", contract=c)

    # ── Settle ── ask the issuer to drop the contract with no exchange.
    if action == "settle":
        if _is_bot_issued(c):
            return _fail(BAD_REQUEST, "AI contracts cannot be settled.", c)
        # Asking again while the first ask is still unanswered changes nothing about
        # the contract, but it does write a notification and @-ping the issuer in
        # their corp channel — so uncapped it is a harassment channel aimed at a
        # person the contractor picked, and unmetered Firestore writes besides.
        # `more_time` has been capped from the start; this is the same rule.
        if _open_request_of(c, REQUEST_SETTLE) is not None:
            return _fail(BAD_STATE,
                         "You have already asked to settle. Wait for the issuer to "
                         "answer, or pay the fine or sue instead.", c)
        _open_request(gid, contract_id, c, REQUEST_SETTLE)
        _notify(gid, str(c["issuer_id"]), "dispute_request", "🤝 Settlement Requested",
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
        # A human issuer can refuse the request, so the per-dispute cap above is
        # enough for them. A bot grants it unconditionally, and a granted extension
        # ends in ACTIVE — from where the overdue sweep reopens the dispute through
        # `open_dispute_fields`, which resets the per-dispute counter. So for a bot
        # contract "once per dispute" was "once per week, forever", and
        # `expire_dispute` — the only path that charges the fine — was never
        # reached. The grants are therefore also counted per contract, in a field
        # no reopen touches.
        granted = int(c.get("more_time_granted") or 0)
        if granted >= settings.DISPUTE_MAX_MORE_TIME_REQUESTS:
            return _fail(BAD_STATE,
                         "This mission has already been extended once. Pay the fine, "
                         "sue, or submit before the dispute clock runs out.", c)
        end_of_week = _end_of_week()
        cdb.update_contract(gid, contract_id, due_date=end_of_week, status=cdb.ACTIVE,
                            pending_request=None, more_time_requests=used + 1,
                            more_time_granted=granted + 1)
        c["status"] = cdb.ACTIVE
        c["due_date"] = end_of_week
        c["more_time_requests"] = used + 1
        c["more_time_granted"] = granted + 1
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
    _notify(gid, str(c["issuer_id"]), "dispute_request", "⏰ Extension Requested",
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


@serialized
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
        _notify(gid, str(c["contractor_id"]), "review_result", "❌ Settlement Refused",
                f"The issuer refused to settle \"{c['mission'][:80]}\".", contract_id)
        log.info("Contract %s: settlement refused by %s", contract_id, actor_name)
        return Result(ok=True, message="Settlement refused.", contract=c)

    cdb.update_contract(gid, contract_id, status=cdb.CANCELLED, pending_request=None)
    c["status"] = cdb.CANCELLED
    _clear_request(c)
    # A settlement is "no fine, no payment" — the issuer's own escrow coming back,
    # never income, so garnishment must not touch it.
    await _pay_issuer(gid, c, refund=c.get("payment", 0))
    _notify(gid, str(c["contractor_id"]), "review_result", "🤝 Settled",
            f"\"{c['mission'][:80]}\" was settled. No fine, no payment.", contract_id)
    # Settled means the rescue never happened — return the issuer's stranded vessel.
    await restore_rescue(gid, contract_id, c)
    log.info("Contract %s settled by %s", contract_id, actor_name)
    return Result(ok=True, message="Settled. Escrow refunded.", contract=c)


@serialized
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

    # An OPEN request is required, unconditionally. This used to read
    # `req is None and not new_date`, which let a caller-supplied date stand in for the
    # request itself — so a stale Discord button (rebuilt by `from_custom_id` with an
    # empty date and re-scraped from the message text) still moved DISPUTED -> ACTIVE
    # and rewrote the deadline on a contract whose `pending_request` had since been
    # cleared by a refusal, by the grace path, or by a later settle. That takes the
    # contract off the auto-fine clock with nothing currently asked for, which is the
    # opposite of what the docstring above promises. `new_date` keeps its ONLY intended
    # job below: supplying the date for a legacy request that has no `new_date` field.
    req = _open_request_of(c, REQUEST_MORE_TIME)
    if req is None:
        return _fail(BAD_STATE, "There is no open extension request on this contract.", c)

    if not approve:
        cdb.update_contract(gid, contract_id, pending_request=None)
        _clear_request(c)
        _notify(gid, str(c["contractor_id"]), "review_result", "❌ Extension Refused",
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
    _notify(gid, str(c["contractor_id"]), "review_result", "⏰ Deadline Extended",
            f"\"{c['mission'][:80]}\" is active again, due {granted}.", contract_id)
    log.info("Contract %s extended to %s by %s", contract_id, granted, actor_name)
    return Result(ok=True, message=f"Deadline extended to {granted}.", contract=c,
                  data={"new_date": granted})


# ── The dispute clock ────────────────────────────────────────────────────────

@serialized
async def expire_dispute(gid: int, contract_id: str) -> Result:
    """Collect the fine on a dispute nobody resolved in time.

    Called by the sweep in `cogs/contracts.py`, never by a player, so it takes no actor
    — the *absence* of an action is what triggers it.

    Collects through `_charge_fine`, so what the contractor cannot cover is billed to
    them as a debt rather than forgiven. Taking what they have and closing matches what
    a moderator does with Enforce Fine, and what the contractor could have chosen
    themselves via Give Up or Pay Fine — this path exists only because they chose
    nothing.
    """
    c, err = _load(gid, contract_id)
    if err:
        return err
    if c.get("status") != cdb.DISPUTED:
        return _fail(BAD_STATE, "Contract is not in dispute.", c)
    if _clock_paused_by_suspension(gid, contract_id, c):
        return _fail(BAD_STATE, "Contractor is suspended; dispute clock paused.", c)

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

    # An open settle / extension request at the deadline means the contractor DID
    # act and the issuer never answered — fining them here made the issuer's
    # silence the contractor's penalty, and "don't answer" the cheapest way for an
    # issuer to collect a fine. The clock still does not pause (settings.py says
    # why: a pause is a stall); instead the unanswered request goes to the
    # moderators, who can settle, extend or enforce the fine with the facts in
    # front of them. Where moderator review is not configured, the request is
    # cleared and the clock restarts ONCE (`request_grace_used`), so an issuer who
    # keeps not answering still gets the agreed penalty on the next window rather
    # than the contractor stalling forever.
    req = c.get("pending_request") or None
    if req and not c.get("request_grace_used"):
        kind = req.get("kind") or "request"
        posted = await _escalate_to_mods(gid, contract_id, c,
                                         opener_id=str(c["contractor_id"]))
        if posted:
            _now_iso = datetime.utcnow().isoformat()
            cdb.update_contract(gid, contract_id, status=cdb.MOD_REVIEW,
                                pending_request=None, request_grace_used=True,
                                escalated_by="unanswered_request",
                                mod_review_at=_now_iso)
            c["status"] = cdb.MOD_REVIEW
            c["request_grace_used"] = True
            c["mod_review_at"] = _now_iso
            _clear_request(c)
            _notify(gid, str(c["contractor_id"]), "review_result", "⚖️ Sent to Moderators",
                    f"Your {kind.replace('_', ' ')} request on \"{c['mission'][:80]}\" "
                    f"went unanswered, so the case was handed to the moderators instead "
                    f"of collecting the fine.", contract_id)
            if not _is_bot_issued(c):
                _notify(gid, str(c["issuer_id"]), "review_result", "⚖️ Sent to Moderators",
                        f"You did not answer the {kind.replace('_', ' ')} request on "
                        f"\"{c['mission'][:80]}\" in time, so the moderators will decide it.",
                        contract_id)
            log.info("Contract %s: dispute timed out with an unanswered %s request; "
                     "escalated to moderators", contract_id, kind)
            return Result(ok=True, message="Unanswered request escalated to moderators.",
                          contract=c, data={"escalated": True})
        now = datetime.utcnow().isoformat()
        cdb.update_contract(gid, contract_id, disputed_at=now, pending_request=None,
                            request_grace_used=True)
        c["disputed_at"] = now
        c["request_grace_used"] = True
        _clear_request(c)
        if not _is_bot_issued(c):
            _notify(gid, str(c["issuer_id"]), "review_result", "⏱️ Request Unanswered",
                    f"You did not answer the {kind.replace('_', ' ')} request on "
                    f"\"{c['mission'][:80]}\". The dispute clock has restarted once; "
                    f"if it runs out again the fine is collected.", contract_id)
        log.info("Contract %s: dispute timed out with an unanswered %s request and no "
                 "moderator channel; clock restarted once", contract_id, kind)
        return _fail(BAD_STATE, "Unanswered request; dispute clock restarted once.", c)

    sym = settings.CURRENCY_SYMBOL
    fine = c.get("fine", 0)
    # Status FIRST, money second. Every mutator on `store` edits the in-process dict
    # and sets a dirty flag; the only thing here that can raise is the Firestore status
    # write. Charging before writing therefore leaves the one state that must never
    # exist: money moved, contract still non-terminal. It is still counted by
    # `_ESCROW_STATUSES`, so the escrow reads as held after being paid out — and
    # `review(approve=True)` accepts DISPUTED, which would pay the contractor a
    # `payment` that is no longer there. Worse, both sweepers RETRY (30 min for
    # disputes, 30 s for auctions), so the next pass charges the fine and refunds the
    # escrow a second time. This order fails the other way: an unpaid closed contract,
    # which is visible, recoverable and does not duplicate. It is what `cancel`,
    # `settle_response`, `mod_resolve(cancel)`, `mod_reset` and `_auto_accept_contract`
    # already do.
    cdb.update_contract(gid, contract_id, status=cdb.COMPLETED,
                        completed_at=datetime.utcnow().isoformat(),
                        pending_request=None, closed_by="dispute_timeout")
    c["status"] = cdb.COMPLETED
    collected, owed = await _charge_fine(gid, c, str(c["contractor_id"]), fine)
    await _pay_issuer(gid, c, refund=c.get("payment", 0), income=collected)
    _clear_request(c)

    _notify(gid, str(c["contractor_id"]), "review_result", "⏱️ Dispute Timed Out",
            f"\"{c['mission'][:80]}\" sat in dispute for "
            f"{settings.DISPUTE_AUTO_FINE_DAYS} days, so the fine was collected "
            f"automatically. -{collected} {sym}."
            + (f" {owed} {sym} could not be covered and is owed to the issuer, "
               f"collected from your later earnings." if owed else ""), contract_id)
    if not _is_bot_issued(c):
        _notify(gid, str(c["issuer_id"]), "review_result", "⏱️ Dispute Timed Out",
                f"The dispute on \"{c['mission'][:80]}\" expired. You received "
                f"{collected + c.get('payment', 0)} {sym}."
                + (f" A further {owed} {sym} is owed to you and comes out of their "
                   f"later earnings." if owed else ""), contract_id)

    await restore_rescue(gid, contract_id, c)
    log.info("Contract %s: dispute timed out, collected %d of %d fine (%d owed)",
             contract_id, collected, fine, owed)
    return Result(ok=True, message=f"Dispute timed out. Fine collected ({collected}).",
                  contract=c, data={"fine_collected": collected, "fine_owed": owed})


def _clock_paused_by_suspension(gid: int, contract_id: str, c: dict) -> bool:
    """Pause (or resume) a contract's clock around a service suspension.

    Submitting is a KSP-only action, and a suspension refuses exactly the KSP API. The
    two sweepers below did not know that, so a suspension quietly ran the overdue clock
    and then the dispute clock and collected the full fine — up to
    MAX_ACTIVE_CONTRACTS_PER_USER of them — turning what the shortfall could not cover
    into a garnished debt that follows the player afterwards. A four-day suspension was
    therefore a fine on every contract they held.

    That directly contradicts what a suspension is documented to be, in `data/suspensions.py`
    ("not a wipe: balance, XP, contracts and listings are untouched and waiting") and in
    the notice the mod draws for it ("nothing was deleted"). It was also unavoidable: the
    exits a contractor has — give up, pay the fine — take the same money, and the only
    action that would have saved them is the one the suspension blocks.

    So the clock stops instead. `clock_paused_at` is stamped the first time a sweep sees
    the contractor suspended, and the elapsed time is added back to the deadline when a
    later sweep sees them free. Driven entirely from the sweeps, so it needs no hook on
    the unsuspend path and self-corrects if one is ever missed — including for a
    suspension that expired on its own clock, which nothing announces.

    Reads FAIL OPEN, exactly as `data/suspensions.py` does: a Firestore blip must not
    freeze every deadline in the system. The cost of failing open is the pre-existing
    behaviour for one sweep pass.

    Returns True when the caller must do nothing this pass.
    """
    from data import suspensions as susp
    try:
        active = susp.get_active(str(c.get("contractor_id") or ""))
    except Exception as exc:  # noqa: BLE001 - fail open, see docstring
        log.warning("Contract %s: could not read suspension state (%s); clock unchanged",
                    contract_id, exc)
        return False

    paused_at = (c.get("clock_paused_at") or "").strip()

    if active:
        if not paused_at:
            now = datetime.utcnow().isoformat()
            cdb.update_contract(gid, contract_id, clock_paused_at=now)
            c["clock_paused_at"] = now
            log.info("Contract %s: contractor is suspended; deadline clock paused.",
                     contract_id)
        return True

    if not paused_at:
        return False

    # Suspension over. Give back exactly the time it took, then let the NEXT sweep judge
    # the extended deadline — resuming and fining in the same pass would hand back the
    # days and immediately spend them.
    try:
        paused_for = datetime.utcnow() - datetime.fromisoformat(paused_at)
    except (ValueError, TypeError):
        paused_for = timedelta(0)
    days = max(0, paused_for.days)
    fields: dict = {"clock_paused_at": None}

    due = (c.get("due_date") or "").strip()
    if days and due:
        try:
            fields["due_date"] = (datetime.strptime(due, "%Y-%m-%d")
                                  + timedelta(days=days)).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            pass
    stamp = (c.get("disputed_at") or "").strip()
    if stamp:
        try:
            fields["disputed_at"] = (datetime.fromisoformat(stamp) + paused_for).isoformat()
        except (ValueError, TypeError):
            pass

    cdb.update_contract(gid, contract_id, **fields)
    c.update({k: v for k, v in fields.items() if v is not None})
    c["clock_paused_at"] = None
    log.info("Contract %s: suspension over after %s; deadline clock resumed.",
             contract_id, paused_for)
    return True


@serialized
async def expire_mod_review(gid: int, contract_id: str) -> Result:
    """Resolve a moderator review nobody answered in time.

    MOD_REVIEW is entered by `dispute(action="sue")` and by the dispute grace path, and
    until now nothing ever left it except a moderator pressing a button. Every other
    transition is refused there, so the issuer's escrow and one of each party's ten
    contract slots were locked indefinitely — the "a contractor who owes a fine wants
    exactly nothing to happen" failure that DISPUTE_AUTO_FINE_DAYS was written to close,
    reappearing one state later.

    Resolves the same way an unanswered dispute does — the fine collects — because the
    alternative direction is gameable: if suing and waiting ended with the fine
    cancelled, suing would strictly dominate paying. See MOD_REVIEW_TIMEOUT_DAYS.

    Called by a sweep, never by a player, so it takes no actor. Money moves only after
    the terminal status is written, like every other transition here.
    """
    c, err = _load(gid, contract_id)
    if err:
        return err
    if c.get("status") != cdb.MOD_REVIEW:
        return _fail(BAD_STATE, "Contract is not in moderator review.", c)
    if _clock_paused_by_suspension(gid, contract_id, c):
        return _fail(BAD_STATE, "Contractor is suspended; review clock paused.", c)

    deadline = mod_review_deadline(c)
    if deadline is None:
        # Entered MOD_REVIEW before the stamp existed. Start the clock rather than
        # resolving instantly against time nobody was counting — the same choice
        # `expire_dispute` makes for a dispute with no `disputed_at`.
        now = datetime.utcnow().isoformat()
        cdb.update_contract(gid, contract_id, mod_review_at=now)
        c["mod_review_at"] = now
        log.info("Contract %s had no mod_review_at; review clock started now.", contract_id)
        return _fail(BAD_STATE, "Review clock started.", c)
    if datetime.utcnow() < deadline:
        return _fail(BAD_STATE, "Moderator review has not timed out yet.", c)

    sym = settings.CURRENCY_SYMBOL
    fine = c.get("fine", 0)
    cdb.update_contract(gid, contract_id, status=cdb.COMPLETED,
                        completed_at=datetime.utcnow().isoformat(),
                        closed_by="mod_review_timeout")
    c["status"] = cdb.COMPLETED
    collected, owed = await _charge_fine(gid, c, str(c["contractor_id"]), fine)
    await _pay_issuer(gid, c, refund=c.get("payment", 0), income=collected)

    _notify(gid, str(c["contractor_id"]), "review_result", "⚖️ Review Timed Out",
            f"\"{c['mission'][:80]}\" waited "
            f"{settings.MOD_REVIEW_TIMEOUT_DAYS} days for a moderator decision and none "
            f"came, so it closed on the agreed terms. -{collected} {sym}."
            + (f" {owed} {sym} is owed and comes out of your later earnings."
               if owed else ""), contract_id)
    if not _is_bot_issued(c):
        _notify(gid, str(c["issuer_id"]), "review_result", "⚖️ Review Timed Out",
                f"\"{c['mission'][:80]}\" waited "
                f"{settings.MOD_REVIEW_TIMEOUT_DAYS} days for a moderator decision and "
                f"none came, so it closed on the agreed terms. "
                f"+{collected + c.get('payment', 0)} {sym}.", contract_id)

    await restore_rescue(gid, contract_id, c)
    log.info("Contract %s: moderator review timed out, collected %d of %d fine (%d owed)",
             contract_id, collected, fine, owed)
    return Result(ok=True, message=f"Review timed out. Fine collected ({collected}).",
                  contract=c, data={"fine_collected": collected, "fine_owed": owed})


@serialized
async def expire_overdue(gid: int, contract_id: str) -> Result:
    """Push an ACTIVE contract past its due date into dispute.

    Like `expire_dispute`, this is called by a sweep and never by a player — the
    *absence* of a submission is what triggers it — so it takes no actor.

    It deliberately charges nothing. Missing a deadline is not the same as conceding:
    the contractor still has settle, more time, pay the fine and sue available to
    them, and this only starts the clock that makes one of those happen. Anything
    else would fine a player for a deadline they might have had a good reason to
    miss, with no chance to say so.
    """
    c, err = _load(gid, contract_id)
    if err:
        return err
    if c.get("status") != cdb.ACTIVE:
        return _fail(BAD_STATE, "Contract is not active.", c)
    if _clock_paused_by_suspension(gid, contract_id, c):
        return _fail(BAD_STATE, "Contractor is suspended; deadline clock paused.", c)

    due = (c.get("due_date") or "").strip()
    try:
        due_date = datetime.strptime(due, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        # No usable due date — nothing to measure lateness against. Left alone rather
        # than swept, since guessing one would dispute a contract that may be fine.
        return _fail(BAD_REQUEST, "Contract has no valid due date.", c)

    today = datetime.now(timezone(timedelta(hours=3))).date()
    if (today - due_date).days <= settings.CONTRACT_OVERDUE_GRACE_DAYS:
        return _fail(BAD_STATE, "Contract is not overdue yet.", c)

    # Through open_dispute_fields, like every other path into DISPUTED: it is what
    # resets the per-dispute more-time allowance, which a hand-written status
    # write here used to skip — so a contractor who had used their one extension
    # before was told "already asked" on the fresh dispute.
    fields = open_dispute_fields()
    cdb.update_contract(gid, contract_id, **fields)
    c.update(fields)

    _notify(gid, str(c["contractor_id"]), "review_result", "⌛ Deadline Passed",
            f"\"{c['mission'][:80]}\" was due {due} and has not been submitted, so it "
            f"has gone to dispute. Settle, ask for more time, pay the fine or sue. "
            f"Left alone, the fine collects itself in "
            f"{settings.DISPUTE_AUTO_FINE_DAYS} days.", contract_id)
    if not _is_bot_issued(c):
        _notify(gid, str(c["issuer_id"]), "review_result", "⌛ Deadline Passed",
                f"\"{c['mission'][:80]}\" was due {due} and was never submitted. "
                f"It is now in dispute.", contract_id)

    log.info("Contract %s went overdue (due %s) and was pushed to dispute", contract_id, due)
    return Result(ok=True, message="Contract overdue; dispute opened.", contract=c)


# ── Moderator resolution ─────────────────────────────────────────────────────

@serialized
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
        await _pay_issuer(gid, c, refund=payment)
        _notify(gid, str(c["contractor_id"]), "review_result", "⚖️ Fine Cancelled",
                f"Moderators cancelled the fine for \"{c['mission'][:80]}\".", contract_id)
        await restore_rescue(gid, contract_id, c)
        log.info("Contract %s: %s cancelled the fine", contract_id, actor_name)
        return Result(ok=True, message="Fine cancelled. Escrow refunded.", contract=c,
                      data={"fine_collected": 0})

    # Take whatever the contractor can actually pay and pass exactly that on, so the
    # issuer is never credited coins that were never debited. The shortfall is billed
    # rather than dropped — a moderator enforcing a fine has decided it is owed, which
    # is precisely the case debt exists for.
    # Status FIRST, money second. Every mutator on `store` edits the in-process dict
    # and sets a dirty flag; the only thing here that can raise is the Firestore status
    # write. Charging before writing therefore leaves the one state that must never
    # exist: money moved, contract still non-terminal. It is still counted by
    # `_ESCROW_STATUSES`, so the escrow reads as held after being paid out — and
    # `review(approve=True)` accepts DISPUTED, which would pay the contractor a
    # `payment` that is no longer there. Worse, both sweepers RETRY (30 min for
    # disputes, 30 s for auctions), so the next pass charges the fine and refunds the
    # escrow a second time. This order fails the other way: an unpaid closed contract,
    # which is visible, recoverable and does not duplicate. It is what `cancel`,
    # `settle_response`, `mod_resolve(cancel)`, `mod_reset` and `_auto_accept_contract`
    # already do.
    cdb.update_contract(gid, contract_id, status=cdb.COMPLETED,
                        completed_at=datetime.utcnow().isoformat(),
                        closed_by="mod_enforce")
    c["status"] = cdb.COMPLETED
    collected, owed = await _charge_fine(gid, c, str(c["contractor_id"]), c.get("fine", 0))
    await _pay_issuer(gid, c, refund=payment, income=collected)
    _notify(gid, str(c["contractor_id"]), "review_result", "⚖️ Fine Enforced",
            f"Moderators enforced the fine for \"{c['mission'][:80]}\". "
            f"-{collected} {settings.CURRENCY_SYMBOL}."
            + (f" {owed} {settings.CURRENCY_SYMBOL} is owed and comes out of your "
               f"later earnings." if owed else ""), contract_id)
    await restore_rescue(gid, contract_id, c)
    log.info("Contract %s: %s enforced the fine (%d collected, %d owed)",
             contract_id, actor_name, collected, owed)
    return Result(ok=True, message=f"Fine enforced ({collected}). Escrow refunded.",
                  contract=c, data={"fine_collected": collected, "fine_owed": owed})


@serialized
async def mod_review_submission(gid: int, contract_id: str, *, actor_id: int,
                                actor_name: str, approve: bool) -> Result:
    """Moderator decides a bot-issued submission the AI reviewer could not.

    `api_server._ai_review_submission` no longer pays a weekly mission when nobody
    reviewed it (key missing, budget spent, model error, quota): the contract stays
    SUBMITTED, a ticket is opened, and this is the button in that ticket. Approve
    runs the same payout `_auto_accept_contract` gives an AI approval; refuse opens
    the ordinary dispute. **Authorization is the caller's job**, exactly as for
    `mod_resolve`.
    """
    c, err = _load(gid, contract_id)
    if err:
        return err
    if not _is_bot_issued(c):
        return _fail(BAD_REQUEST, "Only bot-issued submissions are reviewed here; the "
                                  "issuer reviews their own.", c)
    if c.get("status") != cdb.SUBMITTED:
        return _fail(BAD_STATE, f"This submission is {c.get('status')}, not awaiting review.", c)
    contractor_id = str(c["contractor_id"])
    if approve:
        r = await _api()._auto_accept_contract(
            gid, contractor_id, contract_id, c, reason=f"Approved by moderator {actor_name}.")
        if not r.success:
            return _fail(BAD_STATE, r.message, c)
        c["status"] = cdb.COMPLETED
        log.info("Contract %s: held submission approved by %s", contract_id, actor_name)
        return Result(ok=True, message=r.message, contract=c)
    fields = open_dispute_fields()
    cdb.update_contract(gid, contract_id, review_reason=f"Refused by moderator {actor_name}.",
                        **fields)
    c.update(fields)
    _notify(gid, contractor_id, "review_result", "⚠️ Submission Refused",
            f"A moderator refused your submission for \"{c['mission'][:80]}\". Settle, "
            f"ask for more time, pay the fine or sue.", contract_id)
    await _dm_dispute_options(gid, contract_id, contractor_id)
    log.info("Contract %s: held submission refused by %s", contract_id, actor_name)
    return Result(ok=True, message="Submission refused. Dispute opened.", contract=c)


# ── Date helpers ─────────────────────────────────────────────────────────────

MOD_RESET_STATUSES = frozenset({cdb.PENDING, cdb.ACTIVE, cdb.SUBMITTED, cdb.DISPUTED,
                                cdb.MOD_REVIEW})


@serialized
async def mod_reset(gid: int, contract_id: str, *, actor_name: str) -> Result:
    """One step of a moderator's `/contractreset`: cancel this contract if it is
    still unfinished, refunding the issuer's escrow.

    The command used to do this itself from a snapshot it took in a thread, and
    wrote CANCELLED plus the refund without ever taking `contract_lock`. A `review`
    (or the AI auto-accept, or the player's own `cancel`) landing while that
    snapshot was out paid the contractor, and the reset then overwrote COMPLETED
    with CANCELLED and refunded the issuer the same escrow — one escrow, two
    payouts. So each contract is now re-read here, under the same lock every other
    transition holds, and the status is re-checked *after* the read: a contract that
    closed in the meantime is reported as skipped, not reset.

    This deliberately cancels statuses `cancel` refuses (submitted, disputed,
    mod_review) — it is a moderator's bulk clean-up, not a party backing out — and
    charges no fine, since a reset is not anyone conceding. `data["refunded"]` is
    what went back to the issuer, for the command's tally.
    """
    c, err = _load(gid, contract_id)
    if err:
        return err
    if c.get("status") not in MOD_RESET_STATUSES:
        return _fail(BAD_STATE, f"Contract is already {c.get('status')}.", c)

    cdb.update_contract(gid, contract_id, status=cdb.CANCELLED, pending_request=None,
                        closed_by="mod_reset")
    c["status"] = cdb.CANCELLED
    _clear_request(c)

    refunded = int(c.get("payment", 0) or 0) if not _is_bot_issued(c) else 0
    await _pay_issuer(gid, c, refund=refunded)

    for party in (c.get("issuer_id"), c.get("contractor_id")):
        if party and str(party) != str(_api()._get_bot_user_id()):
            _notify(gid, str(party), "contract_cancelled", "🚫 Contract Reset",
                    f"A moderator cancelled \"{c['mission'][:80]}\"."
                    + (" The escrow was refunded to the issuer." if refunded else ""),
                    contract_id)

    # A rescue's wreck was deleted from the issuer's save when the contract was
    # created. Cancelling without this leaves their ship gone for good.
    await restore_rescue(gid, contract_id, c)
    log.info("Contract %s reset by moderator %s (escrow %d refunded)",
             contract_id, actor_name, refunded)
    return Result(ok=True, message="Contract cancelled.", contract=c,
                  data={"refunded": refunded})


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

    A player who has turned corp pings off (`store.corp_pings_enabled`, set from
    the mod's Settings panel) still gets the mention *text* — the post has to say
    who it is addressed to, and a corp channel has other members reading it — but
    it is sent with `allowed_mentions=none`, so Discord does not notify them. The
    message itself is never withheld: every caller pairs this with `_notify`, so
    the in-game feed carries it either way, and dropping the Discord copy would
    hide a contract from the mods and corp members who can also see the channel.
    The DM fallback is untouched: a DM is the delivery, not a ping, and it is only
    ever reached when there is no corp channel to have posted in.
    """
    bot = _bot()
    if bot is None:
        return None
    from cogs.corps import find_user_corp

    # An account id is only a Discord snowflake when the account HAS a Discord.
    # A website-only player has neither a mention nor a corp channel, and both
    # surfaces below are Discord's — so skip the delivery rather than attempt it
    # with an id Discord cannot resolve. Their notification feed already carries
    # the same message: every caller pairs `_notify` with this.
    did = str(user_id) if str(user_id).isdigit() else ""
    if not did:
        return None

    mention = f"<@{did}>"
    try:
        corp = find_user_corp(gid, did)
        ch_id = int(corp.get("channel_id") or 0) if corp else 0
        if ch_id:
            # The corp may live in another guild — resolve at the bot level.
            channel = bot.get_channel(ch_id)
            if channel is None:
                channel = await bot.fetch_channel(ch_id)
            import discord
            # Ping the recipient and NOBODY else.
            #
            # This used to pass None when pings were on, which `send` reads as "use
            # the client default" — and the client sets none, so Discord parsed
            # every mention in the content. `content` is player-written text at one
            # call site (the auction "winner refused" notice interpolates the
            # auction's `mission`), which made `@everyone` in a mission notify the
            # corp channel's members and the mod role, from the official bot.
            #
            # `users=[recipient]` keeps the intended behaviour — the mention that
            # is the whole point of the message still pings — while making the
            # content itself inert. Falls back to the plain mention-only form if
            # the user object cannot be built.
            try:
                _target = discord.Object(id=int(did))
                _ping = discord.AllowedMentions(everyone=False, roles=False,
                                                users=[_target])
            except Exception:
                _ping = discord.AllowedMentions.none()
            allowed = (_ping if store.corp_pings_enabled(gid, did)
                       else discord.AllowedMentions.none())
            return await channel.send(content=f"{mention} {content}" if content else mention,
                                      embed=embed, view=view, allowed_mentions=allowed)
    except Exception as exc:
        log.warning("Corp-channel delivery to %s failed, trying DM: %s", user_id, exc)
    try:
        u = bot.get_user(int(did)) or await bot.fetch_user(int(did))
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
        msg = await deliver_to_player(gid, str(c["issuer_id"]), embed=e,
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
        msg = await deliver_to_player(gid, str(c["issuer_id"]), embed=e,
                                      view=MoreTimeApprovalView(contract_id, gid, new_date))
        return msg is not None
    except Exception as exc:
        log.warning("Could not send more-time request for %s: %s", contract_id, exc)
        return False


async def _escalate_to_mods(gid: int, contract_id: str, c: dict, *, opener_id: int,
                            view=None) -> bool:
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
        # The buttons the moderators get: enforce/cancel a fine by default, or
        # approve/refuse for a held bot-issued submission.
        view = view or ModReviewView(contract_id, gid)
        # Why the submission was refused, so mods can judge whether the refusal was wrong.
        reason = c.get("review_reason")
        if reason:
            # Escaped like every other free text in a moderator ticket: this is the
            # one field the model returns verbatim, and an embed renders markdown —
            # including masked links aimed at whoever holds the console. (`_embed`
            # itself carries the mission text and both display names; it is shared
            # with the player-facing views, so it is escaped at its own source.)
            e.add_field(name="Refusal Reason",
                        value=discord.utils.escape_markdown(str(reason))[:1024],
                        inline=False)
        files = c.get("submitted_files", [])
        if files:
            e.add_field(name="📁 Submitted Files",
                        # cdb.file_link escapes and caps the name: it is the client's
                        # string, and this field is a masked link in a moderator ticket.
                        value="\n".join(cdb.file_link(f) for f in files)[:1024],
                        inline=False)

        if guild_config.get_channel_id(gid, "ticket_category"):
            try:
                from cogs.tickets import create_ticket
                guild = bot.get_guild(gid)
                if guild is not None:
                    other_id = (str(c["issuer_id"]) if str(opener_id) == str(c.get("contractor_id"))
                                else str(c["contractor_id"]))
                    ch = await create_ticket(
                        bot, guild, opener_id=opener_id, kind="other",
                        title="Contract dispute (escalated)",
                        description=f"<@{opener_id}> escalated contract `{contract_id}` "
                                    "for moderator review.",
                        color=discord.Color.purple(),
                        extra_user_ids=[other_id],
                        extra_embeds=[e],
                        extra_view=view,
                    )
                    if ch is not None:
                        return True
            except Exception as exc:
                log.warning("Could not open sue ticket for %s: %s", contract_id, exc)

        ch = guild_config.resolve_channel(bot, gid, "contract_mod")
        if ch is None:
            return False
        await ch.send(embed=e, view=view)
        return True
    except Exception as exc:
        log.warning("Could not escalate contract %s to mods: %s", contract_id, exc)
        return False
