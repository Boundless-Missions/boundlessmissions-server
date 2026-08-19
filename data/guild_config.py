"""
data/guild_config.py – Per-guild channel & role configuration.

Historically every functional channel (auctions, marketplace, weekly missions,
tickets, …) and every assignable role (the 15 KSP "Level" achievement roles, the
mod role) was a single hardcoded ID in settings.py. Those IDs only make sense in
one "home" server: discord.py resolves channels/roles by *global* ID, so running
the bot in a second guild would post into the wrong server or fail.

This module stores those mappings PER GUILD in the existing `guilds/{guild_id}`
Firestore document (the same doc gkchannels.py writes `gk_channels` into), so the
bot can operate across many servers, each with its own channels and role IDs.

Firestore layout (merged into guilds/{gid}):
    config_channels: { "<channel_key>": <channel_id_int>, ... }
    config_roles:    { "<role_key>":    <role_id_int>,    ... }

settings.py values remain as FALLBACK defaults for the home guild only — a default
is used only when `resolve_channel`/`resolve_role` confirms the target actually
belongs to the guild asking, so a home-server ID can never leak into another guild.
"""

from __future__ import annotations

import logging

import discord

import settings
from data.store import _db

log = logging.getLogger(__name__)


# ── Channel registry ─────────────────────────────────────────────────────────
# key -> (label, description, kind, settings_attr)
#   kind: "text" (text channel) or "category" (channel category)
#   settings_attr: name of the home-guild fallback constant in settings.py (or None)
CHANNEL_TYPES: dict[str, tuple[str, str, str, str | None]] = {
    "weekly_missions":   ("Weekly Missions Board", "Where the weekly missions embed is posted.", "text", "WEEKLY_MISSIONS_CHANNEL_ID"),
    "auction":           ("Auction Listings",      "Where /auction reverse-auction posts go.", "text", "AUCTION_CHANNEL_ID"),
    "checkpoint_photos": ("Checkpoint Photos",     "Where in-game milestone 'hero shots' are posted.", "text", "CHECKPOINT_PHOTOS_CHANNEL_ID"),
    "level_up":          ("Level-Up Announcements","Optional dedicated channel for level-up messages.", "text", "LEVEL_UP_CHANNEL_ID"),
    "contract_mod":      ("Contract Escalations",  "Where contract 'sue' escalations are posted (mod review).", "text", "CONTRACT_MOD_CHANNEL_ID"),
    "ticket_panel":      ("Ticket Panel",          "Channel holding the persistent 'Open a Ticket' button.", "text", "TICKET_PANEL_CHANNEL_ID"),
    "ticket_category":   ("Ticket Category",       "Category under which private ticket channels are created.", "category", "TICKET_CATEGORY_ID"),
    "corp_category":     ("Corp Category",         "Category under which corporation channels are created.", "category", "CORP_CATEGORY_ID"),
}


# ── Role registry ────────────────────────────────────────────────────────────
# Level roles are derived from settings.LEVEL_ROLES (names/descriptions + the
# home-guild fallback ID). The notification, mod, admin and bug_report roles are
# added explicitly. "admin" has deliberately NO settings.py fallback: bot-admin
# authority must be granted per guild on purpose, never inherited from a default.
def _level_role_key(level: int) -> str:
    return f"level_{level}"


def role_label(key: str) -> str:
    """Human label for a role key (used by the /admin setrole UI)."""
    if key == "notifications":
        return "🔔 Notifications (self-assign ping role)"
    if key == "mod":
        return "🛡️ Moderator role"
    if key == "admin":
        return "⭐ Bot-admin role (guild-scoped /admin commands)"
    if key == "bug_report":
        return "🐛 Bug-report role (pinged by in-game bug reports)"
    if key.startswith("level_"):
        try:
            lvl = int(key.split("_", 1)[1])
        except ValueError:
            return key
        info = settings.LEVEL_ROLES.get(lvl)
        if info:
            return f"{info[1]}: {info[2][:60]}"
        return key
    return key


def all_role_keys() -> list[str]:
    keys = [_level_role_key(lvl) for lvl in sorted(settings.LEVEL_ROLES)]
    keys.append("notifications")
    keys.append("mod")
    keys.append("admin")
    keys.append("bug_report")
    return keys


# Keys that must ALL be mapped (to roles that exist in the guild) for the
# achievement-role feature to be enabled in that guild. The mod role is a
# permission concern, not part of the assignable-role gate.
def _required_role_keys() -> list[str]:
    return [_level_role_key(lvl) for lvl in sorted(settings.LEVEL_ROLES)] + ["notifications"]


# ── In-memory cache ──────────────────────────────────────────────────────────
# guild_id (str) -> {"channels": {key: id}, "roles": {key: id}}
_config: dict[str, dict[str, dict[str, int]]] = {}


def _guild_entry(guild_id: int) -> dict[str, dict[str, int]]:
    gid = str(guild_id)
    if gid not in _config:
        _config[gid] = {"channels": {}, "roles": {}}
    return _config[gid]


# ── Defaults from settings.py ────────────────────────────────────────────────

def _channel_default(key: str) -> int | None:
    meta = CHANNEL_TYPES.get(key)
    if not meta or not meta[3]:
        return None
    return getattr(settings, meta[3], None)


def _role_default(key: str) -> int | None:
    if key == "mod":
        return settings.MOD_ROLE_ID
    if key == "bug_report":
        return settings.BUG_REPORT_ROLE_ID
    if key.startswith("level_"):
        try:
            lvl = int(key.split("_", 1)[1])
        except ValueError:
            return None
        info = settings.LEVEL_ROLES.get(lvl)
        return info[0] if info else None
    return None  # notifications has no home-guild default


# ── Reads ────────────────────────────────────────────────────────────────────

def get_channel_id(guild_id: int, key: str) -> int | None:
    """Configured channel id for this guild, else the settings.py default (raw —
    NOT guild-validated; use resolve_channel before sending)."""
    configured = _guild_entry(guild_id)["channels"].get(key)
    if configured:
        return configured
    return _channel_default(key)


def resolve_channel(bot, guild_id: int, key: str):
    """Return the configured channel object, but only if it exists AND belongs to
    `guild_id`. This is the safe accessor: a settings.py fallback that lives in the
    home guild will resolve to None for any other guild, so content can never be
    posted into the wrong server."""
    cid = get_channel_id(guild_id, key)
    if not cid:
        return None
    ch = bot.get_channel(cid)
    if ch is None:
        return None
    ch_guild_id = getattr(getattr(ch, "guild", None), "id", None)
    if ch_guild_id != guild_id:
        return None
    return ch


def any_channel_configured(bot, key: str) -> bool:
    """True if at least one guild the bot is in has `key` resolvable to a real
    channel. Used to gate globally-mirrored features (marketplace, auctions)."""
    for guild in getattr(bot, "guilds", []) or []:
        if resolve_channel(bot, guild.id, key) is not None:
            return True
    return False


def get_role_id(guild_id: int, key: str) -> int | None:
    """Configured role id for this guild, else the settings.py default (raw)."""
    configured = _guild_entry(guild_id)["roles"].get(key)
    if configured:
        return configured
    return _role_default(key)


def resolve_role(guild: discord.Guild, key: str):
    """Return the configured role object, but only if it exists in `guild`."""
    if guild is None:
        return None
    rid = get_role_id(guild.id, key)
    if not rid:
        return None
    return guild.get_role(rid)


def roles_ready(guild: discord.Guild) -> bool:
    """True only when every required assignable role (all level roles + the
    notification role) is mapped to a role that currently exists in this guild.
    The achievement-role feature self-disables in any guild where this is False."""
    if guild is None:
        return False
    return all(resolve_role(guild, key) is not None for key in _required_role_keys())


def missing_role_keys(guild: discord.Guild) -> list[str]:
    """Required role keys that are not yet mapped to an existing role in `guild`."""
    if guild is None:
        return list(_required_role_keys())
    return [key for key in _required_role_keys() if resolve_role(guild, key) is None]


# ── Writes ───────────────────────────────────────────────────────────────────

def set_channel(guild_id: int, key: str, channel_id: int | None) -> None:
    entry = _guild_entry(guild_id)["channels"]
    if channel_id is None:
        entry.pop(key, None)
    else:
        entry[key] = int(channel_id)
    _persist(guild_id)


def clear_channel(guild_id: int, key: str) -> None:
    set_channel(guild_id, key, None)


def set_role(guild_id: int, key: str, role_id: int | None) -> None:
    entry = _guild_entry(guild_id)["roles"]
    if role_id is None:
        entry.pop(key, None)
    else:
        entry[key] = int(role_id)
    _persist(guild_id)


def clear_role(guild_id: int, key: str) -> None:
    set_role(guild_id, key, None)


# ── Persistence ──────────────────────────────────────────────────────────────

def _persist(guild_id: int) -> None:
    entry = _guild_entry(guild_id)
    try:
        _db.collection("guilds").document(str(guild_id)).set(
            {"config_channels": entry["channels"], "config_roles": entry["roles"]},
            merge=True,
        )
    except Exception as exc:  # pragma: no cover - network/IO
        log.error("Failed to save guild config for %s: %s", guild_id, exc)


def load() -> None:
    """Load all per-guild channel/role config from Firestore. Call at startup,
    next to gkchannels.load_gk_channels()."""
    try:
        n_ch = n_role = 0
        for doc in _db.collection("guilds").stream():
            data = doc.to_dict() or {}
            channels = {k: int(v) for k, v in (data.get("config_channels") or {}).items() if v}
            roles = {k: int(v) for k, v in (data.get("config_roles") or {}).items() if v}
            if channels or roles:
                _config[doc.id] = {"channels": channels, "roles": roles}
                n_ch += len(channels)
                n_role += len(roles)
        log.info("Loaded guild config: %d channel mappings, %d role mappings across %d guilds",
                 n_ch, n_role, len(_config))
    except Exception as exc:  # pragma: no cover - network/IO
        log.error("Failed to load guild config: %s", exc)
