"""
cogs/corps.py – Corporation system.

Users can establish a corporation which creates a dedicated text channel.
Each user may own one corporation at a time. Data is persisted in Firestore.

Firestore path: guilds/{guild_id}/corps/{user_id}
"""

import logging
import datetime
import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)

# Import the shared Firestore client
from data.store import _db
import settings
from data import guild_config
from i18n import t, tp
# gkchannels is imported lazily at each call site, not bound here: corps loads
# BEFORE cogs.gkchannels, and load_extension re-execs an already-imported module
# into a second object. Binding the functions now would bind them to that dead
# copy, whose _gk_channels cache is empty — so add/remove would read an empty set
# and _persist would overwrite the guild's whole gk_channels list with one entry.
# (cogs/tickets.py and cogs/weeklymissions.py import is_mod the same way.)


def _get_corp_ref(guild_id: int, user_id: int):
    """Get a Firestore document reference for a user's corporation."""
    return (
        _db.collection("guilds")
        .document(str(guild_id))
        .collection("corps")
        .document(str(user_id))
    )


async def _create_corp_channel(
    guild: discord.Guild,
    owner: discord.Member,
    name: str,
) -> tuple[discord.TextChannel, discord.Message]:
    """Create the corp text channel and pin the establishment embed."""
    # Sanitise channel name (Discord auto-lowercases and replaces spaces with hyphens)
    cat_id = guild_config.get_channel_id(guild.id, "corp_category")
    category = guild.get_channel(cat_id) if cat_id else None
    channel = await guild.create_text_channel(
        name=f"corp-{name}",
        category=category,
        topic=f"🏢 {name} · Founded by {owner.display_name}",
        reason=f"Corporation established by {owner}",
    )

    # Set permissions: owner gets manage_channel so they can invite people etc.
    await channel.set_permissions(
        owner,
        manage_channels=False,
        manage_messages=True,
        send_messages=True,
        reason="Corp owner permissions",
    )

    now = datetime.datetime.now(datetime.timezone.utc)
    gid = guild.id
    embed = discord.Embed(
        title=t(gid, "corps.setup.title", name=name),
        description=t(gid, "corps.setup.desc"),
        color=discord.Color.gold(),
        timestamp=now,
    )
    embed.add_field(name=t(gid, "corps.setup.founder"), value=owner.mention, inline=True)
    embed.add_field(
        name=t(gid, "corps.setup.established"),
        value=discord.utils.format_dt(now, style="F"),
        inline=True,
    )
    embed.set_thumbnail(url=owner.display_avatar.url)
    embed.set_footer(text=f"Corp ID: {owner.id}")

    msg = await channel.send(embed=embed)
    await msg.pin()

    return channel, msg


def _save_corp(guild_id: int, user_id: int, data: dict) -> None:
    """Write corporation data to Firestore."""
    ref = _get_corp_ref(guild_id, user_id)
    ref.set(data)
    # Ensure guild parent doc exists
    _db.collection("guilds").document(str(guild_id)).set(
        {"_exists": True}, merge=True
    )


def _get_corp(guild_id: int, user_id: int) -> dict | None:
    """Read a user's corporation. Checks the given guild first, then falls back to
    the user's GLOBAL corp (which may live in another server), so callers that pass
    a contract/session guild still find the one corp the user owns anywhere.
    The returned dict carries `guild_id` so callers can resolve its channel."""
    doc = _get_corp_ref(guild_id, user_id).get()
    if doc.exists:
        d = doc.to_dict()
        d.setdefault("guild_id", str(guild_id))
        return d
    ptr = get_user_corp_global(user_id)
    if ptr:
        og = int(ptr.get("guild_id", 0) or 0)
        if og and og != guild_id:
            d2 = _get_corp_ref(og, user_id).get()
            if d2.exists:
                d = d2.to_dict()
                d.setdefault("guild_id", str(og))
                return d
    return None


def find_user_corp(guild_id: int, user_id: int) -> dict | None:
    """Find the corp a user belongs to, as owner or member. None if they're in none.

    Corps are keyed by the owner's id, so an owner is a direct (now global) lookup;
    members are found via the `members` array within the given guild.
    """
    own = _get_corp(guild_id, user_id)
    if own:
        return own
    col = _db.collection("guilds").document(str(guild_id)).collection("corps")
    for doc in col.where("members", "array_contains", str(user_id)).stream():
        d = doc.to_dict()
        d.setdefault("guild_id", str(guild_id))
        return d
    return None


def _delete_corp(guild_id: int, user_id: int) -> None:
    """Delete a corporation record from Firestore."""
    _get_corp_ref(guild_id, user_id).delete()


# ── Global corp ownership (one corp per user across ALL servers) ─────────────
# The per-guild corp doc still lives at guilds/{gid}/corps/{uid} (its channel is in
# a specific server), but a global pointer records the ONE server a user owns a
# corp in, so establishing a corp anywhere replaces a corp owned elsewhere.

def _owner_ref(user_id: int):
    return _db.collection("corp_owners").document(str(user_id))


def get_user_corp_global(user_id: int) -> dict | None:
    """Where (if anywhere) this user owns a corp: {guild_id, channel_id, name}."""
    snap = _owner_ref(user_id).get()
    return snap.to_dict() if snap.exists else None


def _set_owner_ptr(user_id: int, guild_id: int, channel_id: int, name: str) -> None:
    _owner_ref(user_id).set({
        "user_id": str(user_id),
        "guild_id": str(guild_id),
        "channel_id": str(channel_id),
        "name": name,
    })


def _clear_owner_ptr(user_id: int) -> None:
    _owner_ref(user_id).delete()


# ── Confirmation UI ──────────────────────────────────────────────────────────

class CorpReplaceView(discord.ui.View):
    """DM view asking if the user wants to replace their existing corporation."""

    def __init__(self, cog: "Corps", guild: discord.Guild, owner: discord.Member, new_name: str):
        super().__init__(timeout=120)
        self.cog = cog
        self.guild = guild
        self.owner = owner
        self.new_name = new_name
        self.result: bool | None = None

    @discord.ui.button(label="Replace", style=discord.ButtonStyle.danger, emoji="🔄")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = True
        self.stop()
        await interaction.response.edit_message(
            content=t(self.guild.id, "corps.replace.confirming"), view=None
        )
        await self.cog._replace_corp(self.guild, self.owner, self.new_name)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = False
        self.stop()
        await interaction.response.edit_message(
            content=t(self.guild.id, "corps.replace.cancelled"), view=None
        )

    async def on_timeout(self) -> None:
        self.result = False


# ── Cog ──────────────────────────────────────────────────────────────────────

class Corps(commands.Cog, name="Corps"):
    """Corporation management system."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── /corpsetup ────────────────────────────────────────────────────────────
    @app_commands.command(
        name="corpsetup",
        description="Establish a new corporation with its own text channel",
    )
    @app_commands.describe(name="Name for your corporation")
    async def corpsetup(
        self, interaction: discord.Interaction, name: str
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                tp(None, interaction.user.id, "common.server_only"), ephemeral=True
            )
            return

        guild = interaction.guild
        member = interaction.user
        gid = guild.id

        # One corp per user GLOBALLY — check whether they own one in any server.
        existing = get_user_corp_global(member.id)

        if existing:
            # User already owns a corp somewhere — DM them for confirmation. The
            # replace flow deletes the old one wherever it lives, then creates the
            # new one here.
            old_name = existing.get("name", "Unknown")
            old_guild = self.bot.get_guild(int(existing.get("guild_id", 0) or 0))
            old_guild_name = old_guild.name if old_guild else "another server"
            old_channel_id = existing.get("channel_id")

            view = CorpReplaceView(self, guild, member, name)

            try:
                dm_embed = discord.Embed(
                    title=t(gid, "corps.replace.title"),
                    description=t(gid, "corps.replace.desc",
                        guild=old_guild_name, old=old_name,
                        channel=old_channel_id, new=name),
                    color=discord.Color.orange(),
                )
                await member.send(embed=dm_embed, view=view)
                await interaction.response.send_message(
                    tp(gid, member.id, "corps.replace.check_dm"),
                    ephemeral=True,
                )
            except discord.Forbidden:
                await interaction.response.send_message(
                    tp(gid, member.id, "corps.replace.no_dm"),
                    ephemeral=True,
                )
            return

        # No existing corp — create one
        await interaction.response.defer(ephemeral=True)

        channel, pin_msg = await _create_corp_channel(guild, member, name)

        # Auto-register as GK channel
        from cogs.gkchannels import add_gk_channel
        add_gk_channel(guild.id, channel.id)

        # Save to Firestore (per-guild record + global ownership pointer)
        now = datetime.datetime.now(datetime.timezone.utc)
        _save_corp(guild.id, member.id, {
            "name": name,
            "owner_id": str(member.id),
            "owner_name": member.name,
            "channel_id": str(channel.id),
            "pin_message_id": str(pin_msg.id),
            "established_at": now.isoformat(),
            "members": [str(member.id)],
        })
        _set_owner_ptr(member.id, guild.id, channel.id, name)

        await interaction.followup.send(
            tp(gid, member.id, "corps.setup.done", name=name, channel=channel.mention),
            ephemeral=True,
        )
        log.info("%s established corporation '%s' (channel: %s)", member, name, channel.id)

    async def _replace_corp(
        self, guild: discord.Guild, owner: discord.Member, new_name: str
    ) -> None:
        """Delete the user's existing corp WHEREVER it lives (possibly another
        server), then create a replacement in `guild`."""
        existing = get_user_corp_global(owner.id)
        if existing:
            old_gid = int(existing.get("guild_id", 0) or 0)
            old_channel_id = existing.get("channel_id")
            old_guild = self.bot.get_guild(old_gid)

            # Delete the old channel + GK registration in whichever guild it was in.
            if old_channel_id and old_guild:
                old_channel = old_guild.get_channel(int(old_channel_id))
                if old_channel:
                    try:
                        await old_channel.delete(reason=f"Corporation replaced by {owner}")
                    except discord.Forbidden:
                        log.warning("No permission to delete old corp channel %s", old_channel_id)
                from cogs.gkchannels import remove_gk_channel
                remove_gk_channel(old_gid, int(old_channel_id))

            # Delete the old per-guild record + clear the global pointer.
            if old_gid:
                _delete_corp(old_gid, owner.id)
            _clear_owner_ptr(owner.id)

        # Create new corp in the requesting guild.
        channel, pin_msg = await _create_corp_channel(guild, owner, new_name)

        # Auto-register as GK channel
        from cogs.gkchannels import add_gk_channel
        add_gk_channel(guild.id, channel.id)

        now = datetime.datetime.now(datetime.timezone.utc)
        _save_corp(guild.id, owner.id, {
            "name": new_name,
            "owner_id": str(owner.id),
            "owner_name": owner.name,
            "channel_id": str(channel.id),
            "pin_message_id": str(pin_msg.id),
            "established_at": now.isoformat(),
            "members": [str(owner.id)],
        })
        _set_owner_ptr(owner.id, guild.id, channel.id, new_name)

        try:
            await owner.send(t(guild.id, "corps.replace.done",
                name=new_name, guild=guild.name, channel=channel.id))
        except discord.Forbidden:
            pass

        log.info("%s replaced corporation with '%s' (channel: %s)", owner, new_name, channel.id)

    # ── Error handler ─────────────────────────────────────────────────────────
    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        log.error("Corps cog error: %s", error, exc_info=True)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                t(interaction.guild_id, "common.error"), ephemeral=True
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Corps(bot))
