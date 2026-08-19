"""
cogs/gkchannels.py – Gene Kerman channel gating system.

Controls which channels the bot can operate in:
- Mods can toggle any channel as a "GK channel" via /mod gkchannel
- All corporation channels are GK channels by default
- Regular users can only use /gk commands in GK channels
- Mods can use /gk commands anywhere
- If a user @mentions the bot in a non-GK channel, the message is deleted
  and the user receives a DM explaining where to interact with the bot

Firestore: guilds/{guild_id} → gk_channels: [channel_id_str, ...]
"""

import logging
import discord
from discord import app_commands
from discord.ext import commands

from data.store import _db
from cogs import perms
from i18n import t, tp

log = logging.getLogger(__name__)

# ── In-memory cache ──────────────────────────────────────────────────────────
# guild_id (str) → set of channel_id (str)
_gk_channels: dict[str, set[str]] = {}


# ── Translation strings (added inline, will merge into i18n.py) ──────────────
# We import S to add our keys
from i18n import S

S.update({
    "gk.channel_enabled":     {"en": "✅ This channel is now a Boundless Missions channel."},
    "gk.channel_disabled":    {"en": "❌ This channel is no longer a Boundless Missions channel."},
    "gk.wrong_channel":       {"en": "⚠️ Your message was deleted. You can only interact with Boundless Missions in its channels.\n\nBoundless Missions channels: {channels}"},
    "gk.cmd_wrong_channel":   {"en": "❌ You can only use this command in Boundless Missions channels.\n\nBoundless Missions channels: {channels}"},
})


# ═══════════════════════════════════════════════════════════════════════════
#  Public helpers — used by bot.py for the global command check
# ═══════════════════════════════════════════════════════════════════════════

def is_gk_channel(guild_id: int, channel_id: int) -> bool:
    """Check if a channel is a GK channel."""
    channels = _gk_channels.get(str(guild_id), set())
    return str(channel_id) in channels


def add_gk_channel(guild_id: int, channel_id: int) -> None:
    """Mark a channel as a GK channel (in cache + Firestore)."""
    gid = str(guild_id)
    if gid not in _gk_channels:
        _gk_channels[gid] = set()
    _gk_channels[gid].add(str(channel_id))
    _persist(guild_id)


def remove_gk_channel(guild_id: int, channel_id: int) -> None:
    """Remove a channel from GK channels (in cache + Firestore)."""
    gid = str(guild_id)
    channels = _gk_channels.get(gid, set())
    channels.discard(str(channel_id))
    _persist(guild_id)


def get_gk_channel_mentions(guild: discord.Guild) -> str:
    """Get a formatted string of GK channel mentions for the guild."""
    gid = str(guild.id)
    channels = _gk_channels.get(gid, set())
    if not channels:
        return "None"
    mentions = []
    for cid in channels:
        ch = guild.get_channel(int(cid))
        if ch:
            mentions.append(ch.mention)
    return ", ".join(mentions) if mentions else "None"


def is_mod(member: discord.Member) -> bool:
    """Check if a member has mod permissions (owner, per-guild mod role, kick, or admin)."""
    from config import cfg
    from data import guild_config
    if member.id == cfg.OWNER_ID:
        return True
    mod_role = guild_config.resolve_role(member.guild, "mod")
    if mod_role and member.get_role(mod_role.id):
        return True
    return (
        member.guild_permissions.kick_members
        or member.guild_permissions.administrator
    )

def mod_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        u = perms.real_user(interaction)   # mimic-safe: gate on the real invoker
        if isinstance(u, discord.Member):
            return is_mod(u)
        return False
    return app_commands.check(predicate)


def load_gk_channels() -> None:
    """Load GK channel lists from Firestore. Call at startup."""
    try:
        for doc in _db.collection("guilds").stream():
            data = doc.to_dict() or {}
            ch_list = data.get("gk_channels", [])
            if ch_list:
                _gk_channels[doc.id] = set(ch_list)
        total = sum(len(v) for v in _gk_channels.values())
        log.info("Loaded %d GK channels across %d guilds", total, len(_gk_channels))
    except Exception as exc:
        log.error("Failed to load GK channels: %s", exc)


def _persist(guild_id: int) -> None:
    """Write current GK channel set to Firestore."""
    gid = str(guild_id)
    channels = list(_gk_channels.get(gid, set()))
    try:
        _db.collection("guilds").document(gid).set(
            {"gk_channels": channels}, merge=True
        )
    except Exception as exc:
        log.error("Failed to save GK channels: %s", exc)


# ═══════════════════════════════════════════════════════════════════════════
#  Cog
# ═══════════════════════════════════════════════════════════════════════════

class GKChannels(commands.Cog, name="GKChannels"):
    """Gene Kerman channel gating system."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        load_gk_channels()

    # ── /gkchannel (mod toggle) ──────────────────────────────────────────────
    # NOT named `setchannel`: cogs/admin.py owns that name, and every command is
    # registered flat on the tree before bot.py regroups them into /admin and /mod,
    # so the two would collide at load time and take this whole cog down with them.
    # (With COMMAND_GROUP empty — the default — they would collide outright.)
    @app_commands.command(
        name="gkchannel",
        description="Toggle this channel as a Boundless Missions channel (Mod only)",
    )
    @app_commands.default_permissions(kick_members=True)
    @mod_only()
    async def gkchannel(self, interaction: discord.Interaction) -> None:
        gid = interaction.guild_id
        cid = interaction.channel_id
        uid = interaction.user.id

        if is_gk_channel(gid, cid):
            remove_gk_channel(gid, cid)
            await interaction.response.send_message(
                tp(gid, uid, "gk.channel_disabled"), ephemeral=True
            )
            log.info("%s removed GK channel: #%s (%d)", interaction.user, interaction.channel.name, cid)
        else:
            add_gk_channel(gid, cid)
            await interaction.response.send_message(
                tp(gid, uid, "gk.channel_enabled"), ephemeral=True
            )
            log.info("%s added GK channel: #%s (%d)", interaction.user, interaction.channel.name, cid)

    # ── Listener: delete bot mentions in non-GK channels ─────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # Ignore bots, DMs
        if message.author.bot or message.guild is None:
            return

        # Only care about messages that mention the bot
        if self.bot.user not in message.mentions:
            return

        # If this is a GK channel, allow it
        if is_gk_channel(message.guild.id, message.channel.id):
            return

        # Mods are exempt
        if isinstance(message.author, discord.Member) and is_mod(message.author):
            return

        # Delete the message
        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound):
            pass

        # DM the user
        channels_str = get_gk_channel_mentions(message.guild)
        try:
            await message.author.send(
                tp(message.guild.id, message.author.id, "gk.wrong_channel",
                   channels=channels_str)
            )
        except discord.Forbidden:
            pass

    # ── Error handler ─────────────────────────────────────────────────────────
    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message(
                tp(interaction.guild_id, interaction.user.id, "common.no_perm"),
                ephemeral=True,
            )
        else:
            log.error("GKChannels cog error: %s", error, exc_info=True)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    tp(interaction.guild_id, interaction.user.id, "common.error"),
                    ephemeral=True,
                )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GKChannels(bot))
