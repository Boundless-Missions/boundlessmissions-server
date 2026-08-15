"""
cogs/admin.py – Administrative commands (bot owner / server admins only).
"""

import asyncio
import hashlib
import logging
import re
import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import View, Select, Button, Modal, TextInput
from config import cfg
from cogs import perms
from api_auth import generate_link_code
from data import mod_version as mver
from data import policy as policy
from data import guild_config
from cost_guard import guard as cost_guard

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
#  /admin setchannel — per-guild channel configuration UI
# ═══════════════════════════════════════════════════════════════════════════

class SetChannelView(View):
    """Embed + selects letting the admin map each channel type to a channel in
    THIS guild. Re-renders after every change so the status list stays current."""

    def __init__(self, guild: discord.Guild, author_id: int):
        super().__init__(timeout=300)
        self.guild = guild
        self.author_id = author_id
        self.selected_key: str | None = None
        self._rebuild()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This panel isn't yours.", ephemeral=True)
            return False
        return True

    def _rebuild(self) -> None:
        self.clear_items()
        self.add_item(_ChannelTypeSelect(self))
        if self.selected_key:
            kind = guild_config.CHANNEL_TYPES[self.selected_key][2]
            # Text slots accept both normal text and announcement (news) channels —
            # the bot can post in either. Category slots accept only categories.
            ctypes = ([discord.ChannelType.category] if kind == "category"
                      else [discord.ChannelType.text, discord.ChannelType.news])
            self.add_item(_GuildChannelSelect(self, ctypes))
            self.add_item(_ClearChannelButton(self))
            # Fallback for Discord's native picker, which truncates the channel
            # list (channels in the bottom-most category often don't appear).
            # Lets the admin paste a channel ID / #mention directly instead.
            self.add_item(_SetChannelByIdButton(self))

    def build_embed(self) -> discord.Embed:
        lines = []
        for key, (label, desc, kind, _attr) in guild_config.CHANNEL_TYPES.items():
            cid = guild_config.get_channel_id(self.guild.id, key)
            ch = self.guild.get_channel(cid) if cid else None
            mark = "✅" if ch else "❌"
            value = ch.mention if ch else "_not set_"
            sel = " ⬅️" if key == self.selected_key else ""
            lines.append(f"{mark} **{label}** — {value}{sel}")
        embed = discord.Embed(
            title="⚙️ Channel configuration",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"{self.guild.name} • pick a type below, then choose a channel.")
        return embed


class _ChannelTypeSelect(Select):
    def __init__(self, parent: SetChannelView):
        self.parent_view = parent
        options = []
        for key, (label, desc, kind, _attr) in guild_config.CHANNEL_TYPES.items():
            cid = guild_config.get_channel_id(parent.guild.id, key)
            ch = parent.guild.get_channel(cid) if cid else None
            options.append(discord.SelectOption(
                label=label[:100],
                value=key,
                description=(f"Currently: #{ch.name}" if ch else "Not set")[:100],
                emoji="✅" if ch else "⬜",
                default=(key == parent.selected_key),
            ))
        super().__init__(placeholder="Pick a channel type to configure…",
                         min_values=1, max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.selected_key = self.values[0]
        self.parent_view._rebuild()
        await interaction.response.edit_message(
            embed=self.parent_view.build_embed(), view=self.parent_view)


class _GuildChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, parent: SetChannelView, ctypes: list[discord.ChannelType]):
        self.parent_view = parent
        label = guild_config.CHANNEL_TYPES[parent.selected_key][0]
        # Pre-select the currently configured channel so the picker reflects it.
        cid = guild_config.get_channel_id(parent.guild.id, parent.selected_key)
        current = parent.guild.get_channel(cid) if cid else None
        defaults = [current] if current is not None else []
        super().__init__(channel_types=ctypes, min_values=1, max_values=1,
                         default_values=defaults,
                         placeholder=f"Choose a channel for: {label}"[:150], row=1)

    async def callback(self, interaction: discord.Interaction):
        ch = self.values[0]
        key = self.parent_view.selected_key
        guild_id = self.parent_view.guild.id
        guild_config.set_channel(guild_id, key, ch.id)
        log.info("%s set channel '%s' -> %s in guild %s",
                 interaction.user, key, ch.id, guild_id)
        self.parent_view._rebuild()
        await interaction.response.edit_message(
            embed=self.parent_view.build_embed(), view=self.parent_view)
        # When a marketplace/auction channel is first set, mirror the existing
        # global catalogue into it (back-fill) so the server isn't empty.
        if key in ("marketplace", "auction"):
            interaction.client.loop.create_task(
                _backfill_after_setchannel(interaction, key, guild_id))


async def _backfill_after_setchannel(interaction: discord.Interaction, key: str, guild_id: int) -> None:
    """Background task: mirror existing marketplace/auction content into a freshly
    configured channel, then quietly report the count to the admin."""
    try:
        if key == "marketplace":
            from cogs.marketplace import backfill_guild
            label = "marketplace listing"
        else:
            from cogs.auctions import backfill_guild
            label = "open auction"
        n = await backfill_guild(interaction.client, guild_id)
        if n:
            await interaction.followup.send(
                f"📦 Mirrored **{n}** existing {label}(s) into this server's channel.",
                ephemeral=True)
    except Exception as exc:
        log.error("Back-fill (%s) failed for guild %s: %s", key, guild_id, exc)


class _ClearChannelButton(Button):
    def __init__(self, parent: SetChannelView):
        self.parent_view = parent
        super().__init__(label="Clear this channel", style=discord.ButtonStyle.red, row=2)

    async def callback(self, interaction: discord.Interaction):
        guild_config.clear_channel(self.parent_view.guild.id, self.parent_view.selected_key)
        log.info("%s cleared channel '%s' in guild %s",
                 interaction.user, self.parent_view.selected_key, self.parent_view.guild.id)
        self.parent_view._rebuild()
        await interaction.response.edit_message(
            embed=self.parent_view.build_embed(), view=self.parent_view)


class _SetChannelByIdButton(Button):
    """Opens a modal to set the selected slot from a pasted channel ID / #mention,
    bypassing Discord's native channel picker (which truncates long lists)."""

    def __init__(self, parent: SetChannelView):
        self.parent_view = parent
        super().__init__(label="Enter ID / #mention",
                         style=discord.ButtonStyle.gray, row=2)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(_SetChannelByIdModal(self.parent_view))


class _SetChannelByIdModal(Modal, title="Set channel by ID / mention"):
    channel_ref = TextInput(
        label="Channel ID or name",
        placeholder="e.g. 1518702830637027526 or ticket-creation",
        required=True, max_length=100,
    )

    def __init__(self, parent: SetChannelView):
        super().__init__()
        self.parent_view = parent

    async def on_submit(self, interaction: discord.Interaction):
        key = self.parent_view.selected_key
        kind = guild_config.CHANNEL_TYPES[key][2]
        guild = self.parent_view.guild

        raw = str(self.channel_ref.value).strip()
        # Accept a raw ID or a <#id> mention (modals submit mentions as plain text,
        # but a pasted <#id> still carries the digits).
        m = re.search(r"\d{15,25}", raw)
        ch = guild.get_channel(int(m.group())) if m else None
        # Otherwise resolve by channel name (with or without a leading '#').
        if ch is None:
            name = raw.lstrip("#").casefold()
            ch = next((c for c in guild.channels if c.name.casefold() == name), None)

        if ch is None:
            return await interaction.response.send_message(
                "❌ Couldn't find a channel with that ID/name in this server.",
                ephemeral=True)

        # Enforce the same type constraint as the native picker.
        if kind == "category":
            ok = isinstance(ch, discord.CategoryChannel)
            want = "a category"
        else:
            ok = ch.type in (discord.ChannelType.text, discord.ChannelType.news)
            want = "a text or announcement channel"
        if not ok:
            return await interaction.response.send_message(
                f"❌ **{guild_config.CHANNEL_TYPES[key][0]}** must be {want}.",
                ephemeral=True)

        guild_config.set_channel(guild.id, key, ch.id)
        log.info("%s set channel '%s' -> %s (by ID) in guild %s",
                 interaction.user, key, ch.id, guild.id)
        self.parent_view._rebuild()
        await interaction.response.edit_message(
            embed=self.parent_view.build_embed(), view=self.parent_view)

        if key in ("marketplace", "auction"):
            interaction.client.loop.create_task(
                _backfill_after_setchannel(interaction, key, guild.id))


# ═══════════════════════════════════════════════════════════════════════════
#  /admin setrole — per-guild role mapping UI
# ═══════════════════════════════════════════════════════════════════════════

class SetRoleView(View):
    """Embed + selects letting the admin map each bot role key (level_1..15,
    notifications, mod) to a role in THIS guild."""

    def __init__(self, guild: discord.Guild, author_id: int):
        super().__init__(timeout=300)
        self.guild = guild
        self.author_id = author_id
        self.selected_key: str | None = None
        self._rebuild()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This panel isn't yours.", ephemeral=True)
            return False
        return True

    def _rebuild(self) -> None:
        self.clear_items()
        self.add_item(_RoleKeySelect(self))
        if self.selected_key:
            self.add_item(_GuildRoleSelect(self))
            self.add_item(_ClearRoleButton(self))

    def build_embed(self) -> discord.Embed:
        missing = guild_config.missing_role_keys(self.guild)
        ready = not missing
        lines = []
        for key in guild_config.all_role_keys():
            role = guild_config.resolve_role(self.guild, key)
            mark = "✅" if role else "❌"
            value = role.mention if role else "_not set_"
            sel = " ⬅️" if key == self.selected_key else ""
            lines.append(f"{mark} **{guild_config.role_label(key)}** — {value}{sel}")
        status = ("🟢 **Role feature ENABLED** — all required roles are mapped."
                  if ready else
                  f"🔴 **Role feature DISABLED** — {len(missing)} required role(s) "
                  "still unmapped (level + notification roles must all be set).")
        embed = discord.Embed(
            title="🎭 Role configuration",
            description=status + "\n\n" + "\n".join(lines),
            color=discord.Color.green() if ready else discord.Color.orange(),
        )
        embed.set_footer(text=f"{self.guild.name} • pick a role key below, then choose a role.")
        return embed


class _RoleKeySelect(Select):
    def __init__(self, parent: SetRoleView):
        self.parent_view = parent
        options = []
        for key in guild_config.all_role_keys():
            role = guild_config.resolve_role(parent.guild, key)
            options.append(discord.SelectOption(
                label=guild_config.role_label(key)[:100],
                value=key,
                description=(f"Currently: @{role.name}" if role else "Not set")[:100],
                emoji="✅" if role else "⬜",
                default=(key == parent.selected_key),
            ))
        super().__init__(placeholder="Pick a role to map…",
                         min_values=1, max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.selected_key = self.values[0]
        self.parent_view._rebuild()
        await interaction.response.edit_message(
            embed=self.parent_view.build_embed(), view=self.parent_view)


class _GuildRoleSelect(discord.ui.RoleSelect):
    def __init__(self, parent: SetRoleView):
        self.parent_view = parent
        label = guild_config.role_label(parent.selected_key)
        # Pre-select the currently mapped role so the picker reflects it.
        current = guild_config.resolve_role(parent.guild, parent.selected_key)
        defaults = [current] if current is not None else []
        super().__init__(min_values=1, max_values=1,
                         default_values=defaults,
                         placeholder=f"Choose a role for: {label}"[:150], row=1)

    async def callback(self, interaction: discord.Interaction):
        role = self.values[0]
        guild_config.set_role(self.parent_view.guild.id, self.parent_view.selected_key, role.id)
        log.info("%s mapped role '%s' -> %s in guild %s",
                 interaction.user, self.parent_view.selected_key, role.id, self.parent_view.guild.id)
        self.parent_view._rebuild()
        await interaction.response.edit_message(
            embed=self.parent_view.build_embed(), view=self.parent_view)


class _ClearRoleButton(Button):
    def __init__(self, parent: SetRoleView):
        self.parent_view = parent
        super().__init__(label="Clear this mapping", style=discord.ButtonStyle.red, row=2)

    async def callback(self, interaction: discord.Interaction):
        guild_config.clear_role(self.parent_view.guild.id, self.parent_view.selected_key)
        log.info("%s cleared role '%s' in guild %s",
                 interaction.user, self.parent_view.selected_key, self.parent_view.guild.id)
        self.parent_view._rebuild()
        await interaction.response.edit_message(
            embed=self.parent_view.build_embed(), view=self.parent_view)


def is_admin():
    """Check: user must have Administrator permission or be the bot owner.

    Gates on the *real* invoker (mimic-safe) so an admin mimicking a higher-
    privileged user can't borrow their authority."""
    async def predicate(interaction: discord.Interaction) -> bool:
        return perms.is_admin_user(interaction)
    return app_commands.check(predicate)


def is_owner():
    """Check: user must be the bot owner (set via BOT_OWNER_ID in .env). Gates on
    the real invoker, so mimicking the owner does not pass this check."""
    async def predicate(interaction: discord.Interaction) -> bool:
        return perms.is_owner_user(interaction)
    return app_commands.check(predicate)


class Admin(commands.Cog, name="Admin"):
    """Commands restricted to server admins and the bot owner."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── /setchannel ───────────────────────────────────────────────────────────
    @app_commands.command(
        name="setchannel",
        description="Configure which channels the bot uses in this server (Admin only)",
    )
    @is_admin()
    async def setchannel(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ Run this inside a server.", ephemeral=True)
            return
        view = SetChannelView(interaction.guild, interaction.user.id)
        await interaction.response.send_message(
            embed=view.build_embed(), view=view, ephemeral=True)

    # ── /setrole ──────────────────────────────────────────────────────────────
    @app_commands.command(
        name="setrole",
        description="Map the bot's level / notification / mod roles in this server (Admin only)",
    )
    @is_admin()
    async def setrole(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ Run this inside a server.", ephemeral=True)
            return
        view = SetRoleView(interaction.guild, interaction.user.id)
        await interaction.response.send_message(
            embed=view.build_embed(), view=view, ephemeral=True)

    # ── /announce ─────────────────────────────────────────────────────────────
    @app_commands.command(
        name="announce",
        description="Send an announcement embed to a channel (Admin only)",
    )
    @app_commands.describe(
        channel="Target channel",
        title="Embed title",
        message="Embed body text",
        color="Hex color code e.g. #5865F2 (optional)",
    )
    @is_admin()
    async def announce(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        title: str,
        message: str,
        color: str = "#5865F2",
    ) -> None:
        try:
            hex_color = int(color.lstrip("#"), 16)
        except ValueError:
            hex_color = 0x5865F2

        embed = discord.Embed(
            title=title,
            description=message,
            color=discord.Color(hex_color),
        )
        embed.set_footer(text=f"Announced by {interaction.user}")
        await channel.send(embed=embed)
        await interaction.response.send_message(
            f"✅ Announcement sent to {channel.mention}", ephemeral=True
        )
        log.info("%s sent announcement to #%s", interaction.user, channel.name)

    # ── /reload ───────────────────────────────────────────────────────────────
    @app_commands.command(
        name="reload", description="Reload a cog without restarting (Owner only)"
    )
    @app_commands.describe(cog="Cog module path e.g. cogs.general")
    @is_owner()
    async def reload(self, interaction: discord.Interaction, cog: str) -> None:
        try:
            await self.bot.reload_extension(cog)
            await interaction.response.send_message(
                f"🔄 Reloaded `{cog}`", ephemeral=True
            )
            log.info("%s reloaded %s", interaction.user, cog)
        except Exception as exc:
            await interaction.response.send_message(
                f"❌ Failed to reload `{cog}`: {exc}", ephemeral=True
            )
            log.error("Reload error for %s: %s", cog, exc)

    # ── /shutdown ─────────────────────────────────────────────────────────────
    @app_commands.command(
        name="shutdown", description="Gracefully shut the bot down (Owner only)"
    )
    @is_owner()
    async def shutdown(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("👋 Shutting down…", ephemeral=True)
        log.warning("%s initiated shutdown", interaction.user)
        await self.bot.close()

    # ── /setprefix ────────────────────────────────────────────────────────────
    @app_commands.command(
        name="setprefix",
        description="Change the bot's prefix command character (Admin only)",
    )
    @app_commands.describe(prefix="New prefix character(s)")
    @is_admin()
    async def setprefix(
        self, interaction: discord.Interaction, prefix: str
    ) -> None:
        self.bot.command_prefix = prefix
        await interaction.response.send_message(
            f"✅ Prefix changed to `{prefix}`", ephemeral=True
        )
        log.info("%s changed prefix to '%s'", interaction.user, prefix)

    # ── /linkas ───────────────────────────────────────────────────────────────
    @app_commands.command(
        name="linkas",
        description="Generate a KSP link code that logs in as another user (Admin only)",
    )
    @app_commands.describe(target="The user whose KSP session to assume")
    @is_admin()
    async def linkas(self, interaction: discord.Interaction, target: discord.Member) -> None:
        await interaction.response.defer(ephemeral=True)
        code = await asyncio.to_thread(
            generate_link_code, interaction.guild_id, target.id, target.display_name
        )
        embed = discord.Embed(
            title="🔧 Admin KSP Link Code",
            description=f"Linking as **{target.display_name}** (`{target.id}`).\n\nEnter this code in KSP:\n\n# `{code}`\n\n⏰ Expires in 10 minutes.",
            color=discord.Color.orange(),
        )
        embed.set_footer(text=f"Issued by {interaction.user}; session will run as {target.display_name}")
        await interaction.followup.send(embed=embed, ephemeral=True)
        log.info("%s generated admin link code for %s (%s)", interaction.user, target, target.id)

    # ── /mimic ────────────────────────────────────────────────────────────────
    # Owner-only. Mimic lets the actor run every interaction AS another user, so it is
    # a powerful impersonation tool. What keeps it safe is not who it may target but
    # that it swaps *business identity only*: every authority check goes through
    # `cogs/perms.py`, which unwraps the swap via `interaction.extras`, so a mimicked
    # target's permissions are never borrowed. Sessions auto-expire (bot.MIMIC_TTL).
    @app_commands.command(
        name="mimic", description="Act as another user for testing (Owner only)"
    )
    @app_commands.describe(target="The user to mimic")
    @is_owner()
    async def mimic(self, interaction: discord.Interaction, target: discord.Member) -> None:
        # Server administrators used to be refused here. That was an accountability
        # rule rather than a security boundary, and it made testing awkward — an admin
        # is often the only other account on a dev server. Lifting it is safe because
        # authority never rides on the swapped identity:
        #   • nothing reads interaction.user.guild_permissions directly;
        #   • is_owner_user / is_admin_user / is_mod_user all gate on perms.real_user;
        #   • the one app_commands.checks.has_permissions (/setxp) sits behind
        #     default_permissions(administrator=True), which Discord enforces against
        #     the real invoker before the interaction reaches the bot.
        # The audit trail is unaffected: the start/stop pair is logged below, and
        # on_interaction tags every mimicked interaction with "(Mimicking …)".
        #
        # The owner is still refused: mimicking yourself is a no-op that would leave a
        # live entry in mimic_map for no reason.
        if target.id == cfg.OWNER_ID:
            await interaction.response.send_message(
                "❌ You can't mimic yourself.", ephemeral=True)
            return

        import time as _time
        from bot import MIMIC_TTL
        if not hasattr(self.bot, "mimic_map"):
            self.bot.mimic_map = {}
        self.bot.mimic_map[interaction.user.id] = (target, _time.time() + MIMIC_TTL)
        await interaction.response.send_message(
            f"🎭 You are now mimicking {target.mention}. Interactions will run as them "
            f"for {int(MIMIC_TTL // 60)} minutes, or until /unmimic.", ephemeral=True
        )
        # Audit on the REAL actor (interaction.user is the actor here — /mimic is
        # excluded from the swap), so the trail records who impersonated whom.
        log.warning("MIMIC: %s (%s) is now mimicking %s (%s)",
                    interaction.user, interaction.user.id, target, target.id)

    # ── /unmimic ──────────────────────────────────────────────────────────────
    @app_commands.command(
        name="unmimic", description="Stop mimicking another user (Owner only)"
    )
    @is_owner()
    async def unmimic(self, interaction: discord.Interaction) -> None:
        if hasattr(self.bot, "mimic_map") and interaction.user.id in self.bot.mimic_map:
            target, _ = self.bot.mimic_map.pop(interaction.user.id)
            await interaction.response.send_message(
                f"🎭 Stopped mimicking {target.mention}.", ephemeral=True
            )
            log.warning("MIMIC: %s (%s) stopped mimicking %s",
                        interaction.user, interaction.user.id, target)
        else:
            await interaction.response.send_message(
                "❌ You are not mimicking anyone.", ephemeral=True
            )

    # ── /publishversion ───────────────────────────────────────────────────────
    @app_commands.command(
        name="publishversion",
        description="Register a KSP mod DLL version + hash for the update gate (Admin only)",
    )
    @app_commands.describe(
        version="Version label, e.g. 1.2.0",
        download_url="Where players download this version",
        dll="Upload GeneKerman.dll to auto-compute its SHA256 (preferred)",
        sha256="Paste the DLL's SHA256 instead of uploading (optional)",
        set_latest="Make this the required latest version (default: yes)",
    )
    @is_admin()
    async def publishversion(
        self,
        interaction: discord.Interaction,
        version: str,
        download_url: str,
        dll: discord.Attachment | None = None,
        sha256: str | None = None,
        set_latest: bool = True,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        digest = (sha256 or "").strip().lower()
        dll_bytes = None
        if dll is not None:
            dll_bytes = await dll.read()
            digest = hashlib.sha256(dll_bytes).hexdigest()

        if not digest:
            await interaction.followup.send(
                "❌ Provide either a `dll` upload or a `sha256` hash.", ephemeral=True
            )
            return
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            await interaction.followup.send(
                "❌ That doesn't look like a SHA256 hash (expected 64 hex chars).", ephemeral=True
            )
            return

        rec = await asyncio.to_thread(
            mver.publish_version, version, digest, download_url, set_latest,
            str(interaction.user), dll_bytes
        )

        # If this became the latest, poke every live client to re-check now so
        # already-running clients gate without waiting for a restart.
        broadcast = rec.get("latest_version") == version.strip()
        if broadcast:
            try:
                import api_server
                api_server.broadcast_version_update()
            except Exception as exc:
                log.warning("Could not broadcast version update: %s", exc)

        embed = discord.Embed(
            title="✅ Mod version published",
            description=(
                f"**Version:** `{version}`\n"
                f"**SHA256:** `{digest}`\n"
                f"**Download:** {download_url}\n"
                f"**Latest now:** `{rec.get('latest_version')}`"
                + ("\n📡 Live clients poked to re-check." if broadcast else "")
                + ("\n🛡️ Attestation enabled (pristine DLL stored)."
                   if rec.get("versions", {}).get(version.strip(), {}).get("has_dll") else
                   "\n⚠️ Attestation OFF for this version. Upload the `dll` (not just a hash) to enable challenge-response anti-tamper.")
            ),
            color=discord.Color.green(),
        )
        if not cfg.KSP_VERSION_CHECK_ENABLED:
            embed.set_footer(text="⚠️ The version gate is disabled (KSP_VERSION_CHECK_ENABLED=false), so clients won't be blocked.")
        await interaction.followup.send(embed=embed, ephemeral=True)
        log.info("%s published mod version %s (%s, latest=%s)",
                 interaction.user, version, digest[:12], rec.get("latest_version"))

    # ── /versioninfo ──────────────────────────────────────────────────────────
    @app_commands.command(
        name="versioninfo",
        description="Show the currently published latest KSP mod version (Admin only)",
    )
    @is_admin()
    async def versioninfo(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        data = await asyncio.to_thread(mver.get_config)
        if not data or not data.get("latest_hash"):
            await interaction.followup.send(
                "ℹ️ No mod version has been published yet; the update gate is inactive.",
                ephemeral=True,
            )
            return
        versions = data.get("versions") or {}
        history = "\n".join(
            f"• `{v}`: `{(info.get('hash') or '')[:12]}…`"
            for v, info in versions.items()
        ) or "N/A"
        embed = discord.Embed(
            title="📦 KSP mod version registry",
            color=discord.Color.blurple(),
            description=(
                f"**Latest:** `{data.get('latest_version')}`\n"
                f"**Hash:** `{data.get('latest_hash')}`\n"
                f"**Download:** {data.get('download_url')}\n"
                f"**Gate:** {'on' if cfg.KSP_VERSION_CHECK_ENABLED else 'off (disabled in .env)'}\n"
                f"**Updated:** {data.get('updated_at', 'N/A')} by {data.get('updated_by', 'N/A')}\n\n"
                f"**Published versions:**\n{history}"
            ),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /policyversion ──────────────────────────────────────────────────────────
    @app_commands.command(
        name="policyversion",
        description="Show or bump the Privacy/Terms version players must accept (Admin only)",
    )
    @app_commands.describe(
        version="New policy version to require (omit to just view the current one)",
        summary="Short note on what changed (optional, stored for the record)",
        privacy_url="Override the Privacy Policy URL shown to players (optional)",
        terms_url="Override the Terms of Service URL shown to players (optional)",
    )
    @is_admin()
    async def policyversion(
        self,
        interaction: discord.Interaction,
        version: int | None = None,
        summary: str | None = None,
        privacy_url: str | None = None,
        terms_url: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        current = await asyncio.to_thread(policy.get_version)

        # No version given → just report the current policy version.
        if version is None:
            data = await asyncio.to_thread(policy.get_config)
            embed = discord.Embed(
                title="📜 Policy version",
                color=discord.Color.blurple(),
                description=(
                    f"**Current required version:** `{current}`\n"
                    f"**Summary:** {data.get('summary') or 'N/A'}\n"
                    f"**Updated:** {data.get('updated_at', 'N/A')} by {data.get('updated_by', 'N/A')}\n\n"
                    f"Bump it with `/policyversion version:{current + 1}` to force every "
                    f"player who accepted an older version to re-accept."
                ),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # Guard against accidentally lowering the version: clients only re-prompt
        # when the server version exceeds what they accepted, so going backwards
        # silently does nothing and is almost always a mistake.
        if version <= current:
            await interaction.followup.send(
                f"❌ New version (`{version}`) must be greater than the current one "
                f"(`{current}`). Re-consent is only triggered by a higher version.",
                ephemeral=True,
            )
            return

        rec = await asyncio.to_thread(
            policy.set_version, version, str(interaction.user),
            summary, privacy_url, terms_url,
        )

        # Poke live clients so they raise the re-consent gate now rather than on
        # their next restart.
        poked = False
        try:
            import api_server
            api_server.broadcast_policy_update()
            poked = True
        except Exception as exc:
            log.warning("Could not broadcast policy update: %s", exc)

        embed = discord.Embed(
            title="✅ Policy version bumped",
            color=discord.Color.green(),
            description=(
                f"**Now requires:** `{rec.get('version')}` (was `{current}`)\n"
                f"**Summary:** {rec.get('summary') or 'N/A'}\n"
                "Players who accepted an older version must now re-accept the "
                "Privacy Policy & Terms before the mod transmits again."
                + ("\n📡 Live clients poked to re-check." if poked else "")
            ),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        log.info("%s bumped policy version to %s (was %s)",
                 interaction.user, version, current)

    # ── /costs ────────────────────────────────────────────────────────────────
    @app_commands.command(
        name="costs",
        description="Show this month's estimated Gemini & Firebase spend (Admin only)",
    )
    @is_admin()
    async def costs(self, interaction: discord.Interaction) -> None:
        snap = cost_guard.snapshot()

        def fmt_budget(svc: dict) -> str:
            if svc["unlimited"]:
                return f"**${svc['usd']:.4f}** / unlimited"
            pct = (svc["usd"] / svc["budget"] * 100) if svc["budget"] else 0
            state = "🟢 active" if svc["ok"] else "🔴 capped"
            return f"**${svc['usd']:.4f}** / ${svc['budget']:.2f}  ({pct:.0f}%) · {state}"

        def human_bytes(n: int) -> str:
            for unit in ("B", "KB", "MB", "GB"):
                if n < 1024 or unit == "GB":
                    return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
                n /= 1024

        g = snap["gemini"]
        f = snap["firebase"]

        # Per-component Firebase breakdown (skip rows that cost nothing).
        rows = []
        for label, count, usd in f["lines"]:
            if count == 0:
                continue
            qty = human_bytes(count) if "Storage" in label else f"{count:,}"
            rows.append(f"• {label}: {qty} → **${usd:.4f}**")
        breakdown = "\n".join(rows) or "• _no usage recorded yet_"

        guard_state = "on" if snap["enabled"] else "OFF (caps disabled in settings)"
        embed = discord.Embed(
            title=f"💸 Estimated service spend: {snap['month']}",
            color=discord.Color.green() if (g["ok"] and f["ok"]) else discord.Color.red(),
            description=(
                f"Cost guard: **{guard_state}**\n"
                "Figures are local estimates from usage, not Google's billing.\n\n"
                f"**🤖 Gemini** (soft-degrade)\n{fmt_budget(g)}\n\n"
                f"**🔥 Firebase** (hard-stop)\n{fmt_budget(f)}\n{breakdown}"
            ),
        )
        embed.set_footer(text="Budgets reset on the 1st (UTC). Tune prices/budgets in settings.py / .env.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── Error handler ─────────────────────────────────────────────────────────
    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message(
                "❌ You don't have permission to use this command.", ephemeral=True
            )
        else:
            log.error("Admin cog error: %s", error, exc_info=True)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "💥 An error occurred.", ephemeral=True
                )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Admin(bot))
