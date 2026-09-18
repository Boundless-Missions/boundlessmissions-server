"""
role_gate.py – restrict this instance to members holding specific roles.

The case this exists for is a **dev or testing bot sharing a guild with the live
one**, the same situation `TICKET_PANEL_ENABLED` covers from the other side. Both
bots see every interaction in the server; without this, a player pressing a button
or running a slash command gets whichever instance answers first, and the test bot
writes to the same shared, guild-independent records (`users/{user_id}`, contracts)
as the real one.

## This is the mirror image of `guild_gate.py`, deliberately

That module hardcodes its allowlist and says why: an allowlist whose contents are
configuration is one that an edit to `.env` can empty, and emptying it silently
restores a hole. Everything here is inverted, so the same reasoning lands the other
way round:

  * The list **is** configuration, because the roles differ per deployment and
    there is no safe hardcoded answer.
  * The gate is **off by default**, so the shipped behaviour is "answer everyone" —
    the live bot must never be narrowed by a file that failed to load.
  * Emptying the list therefore cannot open a hole. It can only *close* one.

So this file may make the bot serve fewer people than `guild_gate` allows, never
more. It is a second, narrower filter, never a bypass: a guild `guild_gate` refuses
stays refused whatever roles the caller holds.

## Enabled with no roles listed refuses everybody

Which is what it literally says. The alternative — treating an empty list as "off" —
silently ignores a switch the operator explicitly turned on, and if they turned it on
to isolate a test instance they would never find out it did nothing. A bot that
answers nobody is diagnosed in seconds; one that answers everybody while claiming not
to is not diagnosed at all. `describe()` says so at boot, at ERROR level.

## One trap worth knowing

Discord gives **@everyone the same id as the guild**, and every member holds it. A
guild id pasted into `ALLOWED_ROLE_IDS` therefore opens the gate to the whole server
instead of restricting it. That is correct behaviour for a role allowlist — it is
a real role — so nothing here refuses it; it is simply the mistake to avoid.

## Two deliberate holes

**The bot owner always passes.** `BOT_OWNER_ID` is the one account that must not be
able to lock itself out: the gate is env-driven, and an owner who mistypes a role id
would otherwise have no way to reach the bot to correct it — every command, button
and modal would be refused, including the admin ones.

**DMs pass**, for the reason `guild_gate` gives: a DM interaction is a button on
something the bot itself sent (the contract settle / more-time / dispute hand-off),
it carries no member and therefore no roles at all, and refusing it would break
delivery rather than close a hole.

## What it does NOT cover

Only the interaction surface — slash commands, autocomplete, buttons, modals and
prefix commands, the four points `guild_gate` is applied at. The `on_message`
listeners (XP in `cogs/xp.py`, the ticket relay in `cogs/tickets.py`, `gkchannels`)
are untouched: they are three separate listeners with no shared funnel, so gating
them means three more patches rather than one, and none of them *answers* anybody.
A test instance that must not touch the shared store at all needs its own Firebase
project, which is the real boundary — this is a narrower tool than that.
"""

import logging
import os

import config as _config  # noqa: F401  — imported for its load_dotenv() side effect

log = logging.getLogger(__name__)

_ENABLED_ENV = "ROLE_GATE_ENABLED"
_ROLES_ENV = "ALLOWED_ROLE_IDS"

REFUSAL = (
    "This bot instance is restricted to testers right now, and your account does "
    "not hold one of the roles it answers. If you were looking for Boundless "
    "Missions, use the main bot."
)


def _flag(name: str) -> bool:
    return (os.getenv(name, "") or "").strip().lower() in {"1", "true", "yes", "on"}


def _ids_from_env() -> frozenset[int]:
    """Role ids from `ALLOWED_ROLE_IDS`, comma- or semicolon-separated.

    A malformed entry is dropped with a warning rather than raising: this is read
    at import time, and a typo must not stop the bot booting. Dropping one can only
    ever make the gate *narrower*, which is the safe direction here — the opposite
    of `guild_gate`, where a dropped entry would widen the hole it exists to close.
    """
    raw = os.getenv(_ROLES_ENV, "") or ""
    out: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.add(int(chunk))
        except ValueError:
            log.warning("%s: ignoring unparseable role id %r", _ROLES_ENV, chunk)
    return frozenset(out)


ENABLED: bool = _flag(_ENABLED_ENV)
ALLOWED_ROLE_IDS: frozenset[int] = _ids_from_env()


def _owner_id() -> int:
    """`BOT_OWNER_ID`, or 0 when unset. Read through the module rather than
    captured at import so a test can move it."""
    try:
        from config import cfg
        return int(getattr(cfg, "OWNER_ID", 0) or 0)
    except Exception:
        return 0


def is_allowed_member(member) -> bool:
    """True if this member may be served.

    Passes everything when the gate is off. A user with no `roles` attribute is a
    `discord.User` rather than a `Member` — a DM, or an object we could not resolve
    — and passes; see the module docstring.
    """
    if not ENABLED:
        return True

    uid = getattr(member, "id", None)
    owner = _owner_id()
    if owner and uid == owner:
        return True

    roles = getattr(member, "roles", None)
    if roles is None:
        return True                        # not a member: DM, or unresolvable

    for role in roles:
        rid = getattr(role, "id", None)
        if rid is not None and rid in ALLOWED_ROLE_IDS:
            return True
    return False


def is_allowed_interaction(interaction) -> bool:
    """True if this interaction may be served.

    Judged on whoever actually pressed the button, not on the mimic target: the
    admin mimic system swaps `interaction.user` before dispatch and stashes the
    real actor in `extras["_mimic_real_user"]`. Reading the swapped value would
    gate an owner on somebody else's roles, and — worse — would let a mimic of an
    allowed member carry an interaction from a caller this gate refuses.
    """
    if not ENABLED:
        return True

    user = getattr(interaction, "user", None)
    try:
        real = (getattr(interaction, "extras", None) or {}).get("_mimic_real_user")
        if real is not None:
            user = real
    except Exception:
        pass
    return is_allowed_member(user)


def describe() -> str:
    """One-line summary for the boot log."""
    if not ENABLED:
        return "off (answering every member)"
    if not ALLOWED_ROLE_IDS:
        return (f"ON with NO roles listed. This instance will refuse every member. "
                f"Set {_ROLES_ENV}, or {_ENABLED_ENV}=false to serve everyone.")
    ids = ",".join(str(r) for r in sorted(ALLOWED_ROLE_IDS))
    return f"on, {len(ALLOWED_ROLE_IDS)} role(s) answered ({ids})"


def misconfigured() -> bool:
    """Enabled but with nothing listed — worth an ERROR at boot rather than a
    silent bot. See the module docstring."""
    return ENABLED and not ALLOWED_ROLE_IDS
