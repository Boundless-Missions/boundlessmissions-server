"""
guild_gate.py – the set of Discord guilds this bot is allowed to operate in.

Before this module existed there was no such set. `cfg.GUILD_IDS` looks like one
and is not: its only use is `bot._sync_commands`, which decides where slash
commands are *registered*. That is discoverability, not authority — it does not
stop the bot being added to a server, it does not cover prefix commands or
button/modal interactions, and left empty (the shipped `.env.example` default) it
syncs globally to every guild the bot is in. Nothing anywhere refused a guild:
there was no `on_guild_join`, no allowlist and no `leave()` in the codebase.

That mattered because the records the mod tier writes are **global**. A wallet is
`users/{user_id}` at the top level, guild-independent by design, and contracts
live in one collection — so an administrator of any server the bot happened to be
in passed `perms.is_mod_user` (which falls back to `kick_members or
administrator`) and could mint into the shared economy. `perms.moderatable_here`
bounds *who* they can act on; nothing bounded *which server they came from*.

The list is therefore **hardcoded below** rather than read from the environment.
An allowlist whose contents are configuration is one that an edit to `.env` — or
a `.env` that failed to load — can empty, and emptying this one silently restores
the hole. The environment may only ever *add* to it, never remove or replace.

Three enforcement points, and the split between them is deliberate:

  * **`on_guild_join` leaves immediately.** A guild we have just been added to
    holds nothing of ours — no corp channels, no tickets, no `guild_config` — so
    leaving costs nothing, and this is the exact case the module exists for.

  * **The boot sweep reports; it does not leave.** A guild we were *already* in
    when this landed may hold all of those things, and a restart that silently
    abandoned one would destroy state in a way the join case cannot. It logs
    loudly every time and leaves only when `GUILD_GATE_LEAVE_ON_BOOT` is set,
    which is a decision for a human rather than for a deploy.

  * **The interaction gate refuses regardless of presence.** This is what makes
    not-leaving safe — presence without service — and it is also what covers the
    window between a join and the leave call completing. It is applied at all
    three dispatch points (slash, button, modal), not just the command tree,
    because a view outlives the message it was sent on.

DMs are deliberately allowed through the interaction gate. A DM interaction is a
button on something the bot itself sent (the contract settle / more-time /
dispute hand-off), it carries no guild to check, and the authority behind it is
gated per command anyway — `perms.moderatable_here` already refuses the mod tools
outright in a DM. Refusing DMs here would break delivery, not close a hole.
"""

import logging
import os

import config as _config  # noqa: F401  — imported for its load_dotenv() side effect

log = logging.getLogger(__name__)


# ── The allowlist ────────────────────────────────────────────────────────────
#
# Hardcoded on purpose. See the module docstring: configuration can be emptied,
# and an empty allowlist is the bug this file was written to remove.
PRIMARY_GUILD_ID = 1518702825604120646

_EXTRA_ENV = "EXTRA_ALLOWED_GUILD_IDS"
_LEAVE_ENV = "GUILD_GATE_LEAVE_ON_BOOT"

REFUSAL = (
    "This bot is not available in this server. Boundless Missions runs in its own "
    "Discord, and its economy, contracts and player records are shared across "
    "every account, so it only accepts commands from servers its owner has "
    "approved."
)


def _extra_from_env() -> frozenset[int]:
    """Additional guild ids from `EXTRA_ALLOWED_GUILD_IDS`, for a dev server.

    Additive only. A malformed entry is dropped with a warning rather than
    raising: this is read at import time, and a typo here must not be able to
    stop the bot booting — the hardcoded id is still in the set either way.
    """
    raw = os.getenv(_EXTRA_ENV, "") or ""
    out: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.add(int(chunk))
        except ValueError:
            log.warning("%s: ignoring unparseable guild id %r", _EXTRA_ENV, chunk)
    return frozenset(out)


ALLOWED_GUILD_IDS: frozenset[int] = frozenset({PRIMARY_GUILD_ID}) | _extra_from_env()


def is_allowed_guild(guild_id) -> bool:
    """True if this guild id is on the allowlist.

    `None` is False. A DM has no guild, and a caller that wants to permit one has
    to say so explicitly (`is_allowed_interaction` does); silently reading "no
    guild" as "allowed" is how a gate ends up passing the case nobody considered.
    """
    if guild_id is None:
        return False
    try:
        return int(guild_id) in ALLOWED_GUILD_IDS
    except (TypeError, ValueError):
        return False


def is_allowed_interaction(interaction) -> bool:
    """True if this interaction may be served: an allowed guild, or a DM.

    See the module docstring for why a DM passes.
    """
    gid = getattr(interaction, "guild_id", None)
    if gid is None:
        return True
    return is_allowed_guild(gid)


def leave_on_boot() -> bool:
    """Whether the boot sweep should actually leave disallowed guilds.

    Off by default, because leaving one destroys its corps, tickets and config,
    and a deploy is the wrong place for that decision. The interaction gate has
    already made the bot inert there by the time this is consulted.
    """
    return (os.getenv(_LEAVE_ENV, "") or "").strip().lower() in {"1", "true", "yes", "on"}


def describe() -> str:
    """One-line summary for the boot log."""
    extra = sorted(ALLOWED_GUILD_IDS - {PRIMARY_GUILD_ID})
    text = f"primary={PRIMARY_GUILD_ID}"
    if extra:
        text += f", extra={','.join(str(g) for g in extra)}"
    return f"{len(ALLOWED_GUILD_IDS)} allowed guild(s) ({text})"
