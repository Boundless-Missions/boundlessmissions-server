"""
cogs/corps.py – Corporation system.

Users can establish a corporation which creates a dedicated text channel.
Each user may own one corporation at a time. Data is persisted in Firestore.

Firestore path: guilds/{guild_id}/corps/{user_id}
"""

import asyncio
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


def _corp_overwrites(
    guild: discord.Guild,
    owner: discord.Member | None,
    members: list[discord.Member] = (),
) -> dict:
    """The private-channel permission set: the corp, the mods, the bot, nobody else.

    Contract offers, dispute hand-offs and craft deliveries are posted into corp
    channels (`contract_actions.deliver_to_player`), so `view_channel=False` on
    @everyone is what keeps that traffic between the parties and the moderators.
    Guild administrators bypass overwrites, so an unmapped "mod" role hides the
    channel from mods without the administrator permission — map it with
    `/admin setrole` first.
    """
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    mod_role = guild_config.resolve_role(guild, "mod")
    if mod_role is not None:
        overwrites[mod_role] = discord.PermissionOverwrite(view_channel=True)
    if owner is not None:
        overwrites[owner] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True,
            manage_messages=True, manage_channels=False,
        )
    for m in members:
        if owner is None or m.id != owner.id:
            overwrites[m] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
    return overwrites


async def _create_corp_channel(
    guild: discord.Guild,
    owner: discord.Member,
    name: str,
) -> tuple[discord.TextChannel, discord.Message]:
    """Create the corp text channel (private to the corp + mods) and pin the
    establishment embed."""
    # Sanitise channel name (Discord auto-lowercases and replaces spaces with hyphens)
    cat_id = guild_config.get_channel_id(guild.id, "corp_category")
    category = guild.get_channel(cat_id) if cat_id else None
    channel = await guild.create_text_channel(
        name=name,
        category=category,
        topic=f"🏢 {name} · Founded by {owner.display_name}",
        overwrites=_corp_overwrites(guild, owner),
        reason=f"Corporation established by {owner}",
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


async def _establish_corp(
    guild: discord.Guild, member: discord.Member, name: str
) -> discord.TextChannel:
    """Create and persist a corporation: channel + GK registration + Firestore
    record + global ownership pointer. The one implementation behind /corpsetup,
    corp replacement, and auto-generation — callers only decide the name."""
    channel, pin_msg = await _create_corp_channel(guild, member, name)

    # Auto-register as GK channel (lazy import — see the note at the top imports)
    from cogs.gkchannels import add_gk_channel
    add_gk_channel(guild.id, channel.id)

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
    log.info("Established corporation '%s' for %s (channel: %s)", name, member, channel.id)
    return channel


def _auto_corp_name(member: discord.Member) -> str:
    """'{username} Space Agency' — the name auto-generated corps are given.
    Uses the display name, which is what the member is known as in the guild;
    the raw account name is kept separately as `owner_name`."""
    return f"{member.display_name} Space Agency"


async def _resolve_member(guild: discord.Guild, uid) -> discord.Member | None:
    """A member by id, or None for anyone who has left (or a bad id)."""
    try:
        return guild.get_member(int(uid)) or await guild.fetch_member(int(uid))
    except (discord.NotFound, discord.HTTPException, ValueError, TypeError):
        return None


async def _apply_corp_privacy(guild: discord.Guild, corp: dict, *,
                              reason: str, force: bool = False) -> str:
    """Bring one corp's channel up to spec: the private overwrite set, and the
    channel name without the retired `corp-` prefix.

    Returns "updated", "ok" (already compliant — skipped unless `force`, so the
    startup sweep costs zero API calls on a guild that is already migrated),
    or "missing" (channel gone). `force` re-asserts the overwrites even on a
    private channel, for when the mod role mapping or membership has changed;
    both fixes share one `edit` call, since channel edits are rate-limited.
    """
    channel = guild.get_channel(int(corp.get("channel_id") or 0))
    if channel is None:
        return "missing"
    edits: dict = {}
    if channel.name.startswith("corp-") and channel.name != "corp-":
        edits["name"] = channel.name[len("corp-"):]
    if force or channel.overwrites_for(guild.default_role).view_channel is not False:
        owner = await _resolve_member(guild, corp.get("owner_id"))
        members = [m for m in [await _resolve_member(guild, uid)
                               for uid in corp.get("members", [])] if m]
        edits["overwrites"] = _corp_overwrites(guild, owner, members)
    if not edits:
        return "ok"
    await channel.edit(reason=reason, **edits)
    return "updated"


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
        self._swept = False  # the startup sweep runs once, not on every reconnect

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

        channel = await _establish_corp(guild, member, name)

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
        channel = await _establish_corp(guild, owner, new_name)

        try:
            await owner.send(t(guild.id, "corps.replace.done",
                name=new_name, guild=guild.name, channel=channel.id))
        except discord.Forbidden:
            pass

        log.info("%s replaced corporation with '%s' (channel: %s)", owner, new_name, channel.id)

    # ── Startup sweep ─────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Bring every guild up to date on boot: privatize corp channels created
        before privacy existed, then create corps for members who joined before
        auto-generation existed. Idempotent and cheap when there is nothing to
        do — already-private channels are detected from the local permission
        cache (zero API calls), and corp ownership is one stream of the global
        `corp_owners` collection rather than a query per member."""
        if self._swept:  # on_ready re-fires on every reconnect
            return
        self._swept = True

        # Everyone who owns a corp anywhere.
        owners = {doc.id for doc in _db.collection("corp_owners").stream()}

        for guild in self.bot.guilds:
            covered = set(owners)
            privatized = created = 0

            col = _db.collection("guilds").document(str(guild.id)).collection("corps")
            for doc in col.stream():
                d = doc.to_dict() or {}
                d.setdefault("owner_id", doc.id)
                # Non-owner corp members have a corp channel already; they must
                # not get a second one of their own.
                covered.update(str(u) for u in d.get("members", []))
                covered.add(doc.id)
                try:
                    if await _apply_corp_privacy(
                            guild, d, reason="Corp privacy startup sweep") == "updated":
                        privatized += 1
                except Exception as exc:
                    log.warning("Startup privacy sweep failed for corp %s in %s: %s",
                                doc.id, guild.id, exc)

            for member in list(guild.members):
                if member.bot or str(member.id) in covered:
                    continue
                try:
                    await _establish_corp(guild, member, _auto_corp_name(member))
                    owners.add(str(member.id))
                    created += 1
                    await asyncio.sleep(2)  # channel-create rate limit
                except Exception as exc:
                    log.warning("Startup corp generation failed for %s in %s: %s",
                                member, guild.id, exc)

            if privatized or created:
                log.info("Corp startup sweep in %s: %d channel(s) updated "
                         "(privacy/rename), %d corp(s) created", guild.id, privatized, created)

    # ── Auto-generation ───────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Every human member gets a corporation on arrival, so contract offers,
        dispute hand-offs and craft deliveries always have a corp channel to land
        in (rather than the DM fallback). One corp per user globally still holds:
        someone who already owns one in another server is left alone."""
        if member.bot:
            return
        try:
            if find_user_corp(member.guild.id, member.id):
                return
            await _establish_corp(member.guild, member, _auto_corp_name(member))
        except Exception as exc:
            log.warning("Could not auto-establish corp for %s: %s", member, exc)

    # ── /admin corpsgenerate ──────────────────────────────────────────────────
    @app_commands.command(
        name="corpsgenerate",
        description="Create a corporation for every member that doesn't have one",
    )
    @app_commands.default_permissions(administrator=True)
    async def corpsgenerate(self, interaction: discord.Interaction) -> None:
        """Backfill for members who joined before auto-generation existed.
        Channel creation is heavily rate-limited by Discord, so this paces
        itself; on a large guild it can take a while. Safe to re-run — members
        who already have a corp (here or anywhere) are skipped."""
        if not interaction.guild:
            await interaction.response.send_message(
                tp(None, interaction.user.id, "common.server_only"), ephemeral=True)
            return
        guild = interaction.guild
        await interaction.response.defer(ephemeral=True)

        created, failed, skipped = 0, [], 0
        for member in list(guild.members):
            if member.bot:
                continue
            try:
                if find_user_corp(guild.id, member.id):
                    skipped += 1
                    continue
                await _establish_corp(guild, member, _auto_corp_name(member))
                created += 1
                await asyncio.sleep(2)  # be gentle with the channel-create rate limit
            except Exception as exc:
                log.warning("corpsgenerate: could not create corp for %s: %s", member, exc)
                failed.append(member.display_name)

        lines = [f"🏢 Created **{created}** corporation(s); {skipped} member(s) already had one."]
        if failed:
            lines.append(f"⚠️ Failed for: {', '.join(failed[:10])}"
                         + (f" (+{len(failed) - 10} more)" if len(failed) > 10 else ""))
        try:
            await interaction.followup.send("\n".join(lines), ephemeral=True)
        except discord.HTTPException:
            # A very large backfill can outlive the interaction token (15 min).
            log.info("corpsgenerate in %s: %d created, %d skipped, %d failed",
                     guild.id, created, skipped, len(failed))

    # ── /admin corpsprivacy ───────────────────────────────────────────────────
    @app_commands.command(
        name="corpsprivacy",
        description="Make every existing corporation channel private (corp + mods only)",
    )
    @app_commands.default_permissions(administrator=True)
    async def corpsprivacy(self, interaction: discord.Interaction) -> None:
        """One-time migration: apply the private overwrites `_create_corp_channel`
        now sets to corp channels created before privacy existed. Discord
        permissions are not retroactive, so old channels stay public until this
        sweep runs. Safe to re-run — it just re-asserts the same overwrites."""
        if not interaction.guild:
            await interaction.response.send_message(
                tp(None, interaction.user.id, "common.server_only"), ephemeral=True)
            return
        guild = interaction.guild
        await interaction.response.defer(ephemeral=True)

        updated, failed, missing = [], [], []
        col = _db.collection("guilds").document(str(guild.id)).collection("corps")
        for doc in col.stream():
            d = doc.to_dict() or {}
            d.setdefault("owner_id", doc.id)
            name = d.get("name", doc.id)
            try:
                # force=True: re-asserts the set even on already-private channels,
                # so this command also repairs a changed mod-role mapping.
                outcome = await _apply_corp_privacy(
                    guild, d, reason=f"Corp privacy migration by {interaction.user}",
                    force=True)
            except discord.Forbidden:
                failed.append(name)
                continue
            if outcome == "missing":
                missing.append(name)
            else:
                updated.append(name)

        lines = [f"🔒 Made **{len(updated)}** corp channel(s) private."]
        if failed:
            lines.append(f"⚠️ No permission to edit: {', '.join(failed[:10])}")
        if missing:
            lines.append(f"👻 Channel gone (skipped): {', '.join(missing[:10])}")
        if guild_config.resolve_role(guild, "mod") is None:
            lines.append("⚠️ No `mod` role is mapped (`/admin setrole`) — only "
                         "administrators can see the channels until one is.")
        await interaction.followup.send("\n".join(lines), ephemeral=True)
        log.info("%s ran corpsprivacy in %s: %d updated, %d failed, %d missing",
                 interaction.user, guild.id, len(updated), len(failed), len(missing))

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
