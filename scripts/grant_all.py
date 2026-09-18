#!/usr/bin/env python
"""grant_all.py - give every existing player the same one-off amount of coins.

WHY A SCRIPT AND NOT A COMMAND
------------------------------
There is no bulk-grant surface in the bot: `/givemoney` and the console's
`admin_user_adjust` each move one wallet. This is the one-off, and it lives in
`scripts/` with the other migrations rather than becoming a slash command,
because a command that pays everybody is a command that can pay everybody twice.

IT REUSES THE STORE, IT DOES NOT REIMPLEMENT IT
-----------------------------------------------
The grant goes through `store.add_balance(..., category=TX_ADMIN)` and
`store.save()` — the same path `/givemoney` takes. Writing `balance` straight
into Firestore would land the coins and skip the transaction ledger, so the
Finance tab's history would no longer add up to the balance it claims to
explain, which is the one property that makes it worth showing.

NOT GARNISHABLE, DELIBERATELY
-----------------------------
`garnishable=True` marks a credit as *earnings*, from which an unpaid fine debt
is repaid. A blanket gift is an admin correction, not something the player
earned, and skimming it would take a gift straight to somebody else's wallet —
the same reason `/givemoney` does not pass the flag. `--garnishable` is there if
you decide otherwise, but the default is off.

THE BOT MUST BE STOPPED FIRST
-----------------------------
The running bot holds every user record in memory and flushes a dirty one with a
whole-document `set`. Write to Firestore underneath it and the grant survives
only for players who stay idle: anyone who earns XP, sells a craft or settles a
contract gets their record rewritten from the bot's stale memory, silently
reverting the coins for exactly the active players. So this refuses to run while
the public API answers. On the VPS:

    systemctl stop bm-bot
    ./.venv/bin/python scripts/grant_all.py --apply
    systemctl start bm-bot

IDEMPOTENT, AND REVERSIBLE
--------------------------
Every grant carries a `--grant-id` (default: today's date) written into the
ledger entry's detail. A user whose ledger already shows that id is skipped, so
a re-run after a partial failure pays the remainder and nobody twice. Each run
writes a receipt to `scripts/grants/<grant-id>.json` recording every id and its
before/after, and `--revert <receipt>` takes back exactly what that run gave —
so this is undoable by the numbers rather than by memory.

USAGE
-----
    ./.venv/bin/python scripts/grant_all.py                 # dry run, 1000 each
    ./.venv/bin/python scripts/grant_all.py --apply
    ./.venv/bin/python scripts/grant_all.py --amount 500 --apply
    ./.venv/bin/python scripts/grant_all.py --revert scripts/grants/2026-09-09.json --apply
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from urllib import request as urlrequest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from data.store import store  # noqa: E402

HEALTH_URL = "https://mainserver.boundlessmissions.com/api/v1/health"
RECEIPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "grants")
DETAIL_PREFIX = "Community grant"


def detail_for(grant_id: str) -> str:
    """The ledger detail line. This string IS the idempotency marker, so it must
    be stable across runs and unique per grant."""
    return f"{DETAIL_PREFIX} {grant_id}"


def already_granted(user: dict, detail: str) -> bool:
    """Whether this user's ledger already records this grant.

    `tx` is a ring buffer capped at TX_MAX, so an entry can age out — but only
    behind 250 later movements, which no plausible re-run window reaches.
    """
    for entry in user.get("tx") or []:
        if str(entry.get("d") or "") == detail:
            return True
    return False


def bot_is_live() -> bool:
    """True if the public API answers — i.e. the bot is up and its memory would
    overwrite whatever we write."""
    try:
        with urlrequest.urlopen(HEALTH_URL, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


async def run_grant(args) -> int:
    detail = detail_for(args.grant_id)
    await store.load()

    users = store.get_all_users(0)
    if not users:
        print("No user records loaded, refusing to act on an empty store.")
        return 1

    targets, skipped = [], []
    for uid, rec in sorted(users.items()):
        (skipped if already_granted(rec, detail) else targets).append(
            (uid, int(rec.get("balance") or 0)))

    print(f"grant id:   {args.grant_id}")
    print(f"amount:     {args.amount} each  (garnishable={args.garnishable})")
    print(f"users:      {len(users)}")
    print(f"to grant:   {len(targets)}")
    print(f"already in: {len(skipped)}")
    print(f"total cost: {len(targets) * args.amount}")

    if not args.apply:
        for uid, bal in targets[:10]:
            print(f"  would grant {uid}: {bal} -> {bal + args.amount}")
        if len(targets) > 10:
            print(f"  ... and {len(targets) - 10} more")
        print("\nDRY RUN, nothing written. Re-run with --apply.")
        return 0

    granted = []
    for uid, before in targets:
        after = await store.add_balance(
            0, uid, args.amount,
            garnishable=args.garnishable,
            category=store.TX_ADMIN, detail=detail)
        granted.append({"user_id": uid, "before": before, "after": after,
                        "amount": after - before})

    await store.save()

    receipt = {
        "grant_id": args.grant_id,
        "detail": detail,
        "amount": args.amount,
        "garnishable": args.garnishable,
        "at": datetime.now(timezone.utc).isoformat(),
        "granted": granted,
    }
    os.makedirs(RECEIPT_DIR, exist_ok=True)
    path = os.path.join(RECEIPT_DIR, f"{args.grant_id}.json")
    with open(path, "w") as fh:
        json.dump(receipt, fh, indent=2)

    print(f"\nGranted {len(granted)} user(s). Receipt: {path}")
    return 0


async def run_revert(args) -> int:
    with open(args.revert) as fh:
        receipt = json.load(fh)

    await store.load()
    detail = f"Reverted {receipt['detail']}"

    print(f"reverting:  {receipt['grant_id']}  ({len(receipt['granted'])} users)")
    if not args.apply:
        print("DRY RUN, nothing written. Re-run with --apply.")
        return 0

    for row in receipt["granted"]:
        # Take back what actually landed, not what was asked for. `add_balance`
        # clamps at zero, so a wallet that has since been spent below the grant
        # gives back what it can rather than going negative.
        await store.add_balance(0, row["user_id"], -int(row["amount"]),
                                category=store.TX_ADMIN, detail=detail)
    await store.save()
    print(f"Reverted {len(receipt['granted'])} user(s).")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--amount", type=int, default=1000, help="coins per player (default 1000)")
    p.add_argument("--grant-id", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                   help="idempotency marker written into the ledger detail")
    p.add_argument("--garnishable", action="store_true",
                   help="treat as earnings, so fine debt is repaid out of it (default off)")
    p.add_argument("--revert", metavar="RECEIPT", help="undo the run described by a receipt file")
    p.add_argument("--apply", action="store_true", help="actually write (default: dry run)")
    p.add_argument("--force-while-live", action="store_true",
                   help="skip the running-bot check (you will lose the grant for active players)")
    args = p.parse_args()

    if args.amount <= 0 and not args.revert:
        print("--amount must be positive.")
        return 2

    if args.apply and not args.force_while_live and bot_is_live():
        print(f"REFUSING: {HEALTH_URL} is answering, so the bot is running.\n"
              "Its in-memory store would overwrite this grant for every player who\n"
              "does anything before the next restart. Stop it first:\n\n"
              "    systemctl stop bm-bot\n"
              "    ./.venv/bin/python scripts/grant_all.py --apply\n"
              "    systemctl start bm-bot\n")
        return 1

    return asyncio.run(run_revert(args) if args.revert else run_grant(args))


if __name__ == "__main__":
    raise SystemExit(main())
