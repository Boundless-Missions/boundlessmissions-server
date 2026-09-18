"""
bot.py – Main entry point.

Loads all cogs, syncs slash commands, and starts the bot.

Usage:
  Normal start (no sync):  .venv/bin/python bot.py
  Sync commands then run:  .venv/bin/python bot.py --sync

Only pass --sync when you have added or changed slash commands.
Doing it on every restart will hit Discord's rate limit.
"""

import asyncio
import logging
import sys
import threading
import time
import discord
from discord import app_commands
from discord.ext import commands
from config import cfg, insecure_gates
import guild_gate
import role_gate

# Parse --sync flag before the bot starts
_SYNC_COMMANDS = "--sync" in sys.argv

# Ensure our own (non-discord, non-uvicorn) module logs reach stdout at INFO.
# No-op if the root logger is already configured elsewhere.
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

log = logging.getLogger(__name__)

# ── Mimic System ─────────────────────────────────────────────────────────────
# We monkey-patch the three internal dispatch points in discord.py to swap
# interaction.user with the mimicked target BEFORE handlers run. The 'user'
# slot on Interaction is writable, so direct assignment works fine.
#
# SECURITY: the swap only changes *business-logic identity*. It must NEVER let the
# real actor gain authority they don't have. Two invariants enforce that:
#   1. /mimic is owner-only (see cogs/admin.py). It may target any member, including
#      server administrators — the target's permissions are never borrowed, so who it
#      points at is not what makes it safe.
#   2. Every permission check gates on the REAL invoker via cogs/perms.real_user
#      (which reads interaction.extras["_mimic_real_user"]), not the swapped user.
#      This is the load-bearing one. A new check that reads interaction.user directly
#      would be a privilege escalation, so route every one of them through perms.
# Entries auto-expire (MIMIC_TTL) so a forgotten mimic can't linger indefinitely.

MIMIC_TTL = 1800.0   # seconds — a mimic session auto-clears after 30 minutes


def _mimic_target(bot, real_id):
    """The live mimic target for `real_id`, or None. Drops the entry once expired.
    mimic_map values are (target_member, expires_at)."""
    mmap = getattr(bot, "mimic_map", None)
    if not mmap or real_id is None:
        return None
    entry = mmap.get(real_id)
    if entry is None:
        return None
    target, expires = entry
    if time.time() > expires:
        mmap.pop(real_id, None)
        return None
    return target


def _apply_mimic(bot, interaction, *, exclude=()):
    """Swap interaction.user to the mimic target (stashing the real user) unless the
    invoked command is in `exclude` (so mimic/unmimic always run as the real actor)."""
    real_user = interaction.user
    real_id = getattr(real_user, "id", None)
    target = _mimic_target(bot, real_id)
    if target is None:
        return
    if exclude:
        cmd = getattr(interaction, "command", None)
        if (getattr(cmd, "name", None) if cmd else None) in exclude:
            return
    interaction.extras["_mimic_real_user"] = real_user
    interaction.user = target


# ── Guild allowlist, at the two dispatch points the command tree does not cover ─
#
# `GatedCommandTree.interaction_check` below covers slash commands and autocomplete.
# Buttons and modals never reach the tree — they are dispatched straight out of
# ViewStore — and a view outlives the message it was sent on, so a guild that is
# de-listed (or that we have left) can still have our buttons on screen in it.
# These two hooks are already patched for mimic, so the gate rides along rather
# than adding a third patch of discord.py internals.


async def _refuse_interaction(interaction, reason: str = guild_gate.REFUSAL) -> None:
    """Answer a gated-off interaction, best effort.

    The sentence is a parameter because two different gates refuse here and they
    are not the same news: "this server is not served" and "you are not a tester on
    this instance" send the reader to different places.
    """
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(reason, ephemeral=True)
    except Exception:                      # already answered, expired, 403 in a DM…
        pass


def _gate_component(interaction) -> bool:
    """False when this component/modal interaction must NOT be dispatched.

    Answering matters: an undispatched custom_id renders as "This interaction
    failed", which reads as the bot being broken rather than as a deliberate
    refusal — the same reason cogs/marketplace.py survives as a tombstone. Dispatch
    is synchronous, so the reply is scheduled onto the loop, and every failure in
    that scheduling is swallowed: the refusal must hold even if the reply cannot.

    Two gates, checked in that order and never merged: the guild allowlist is an
    authority boundary, while the role gate only narrows which members THIS
    instance answers. A guild refused by the first stays refused whatever roles the
    caller holds.
    """
    if guild_gate.is_allowed_interaction(interaction):
        if role_gate.is_allowed_interaction(interaction):
            return True
        log.info(
            "Role gate: refused a component interaction from user %s in guild %s",
            getattr(getattr(interaction, "user", None), "id", None),
            getattr(interaction, "guild_id", None),
        )
        _schedule_refusal(interaction, role_gate.REFUSAL)
        return False
    log.warning(
        "Guild gate: refused a component interaction from guild %s (user %s)",
        getattr(interaction, "guild_id", None),
        getattr(getattr(interaction, "user", None), "id", None),
    )
    _schedule_refusal(interaction, guild_gate.REFUSAL)
    return False


def _schedule_refusal(interaction, reason: str) -> None:
    """Put the refusal reply on the loop. Dispatch is synchronous, so this cannot
    await; every failure in the scheduling is swallowed, because the refusal has to
    hold even when the reply does not reach the user."""
    try:
        client = getattr(interaction, "client", None) or getattr(interaction, "_client", None)
        loop = getattr(client, "loop", None)
        if loop is not None:
            loop.create_task(_refuse_interaction(interaction, reason))
    except Exception:
        pass


# Patch CommandTree._from_interaction (slash commands + autocomplete)
_original_from_interaction = discord.app_commands.CommandTree._from_interaction

def _patched_from_interaction(self, interaction):
    _apply_mimic(self.client, interaction, exclude=("mimic", "unmimic"))
    _original_from_interaction(self, interaction)

discord.app_commands.CommandTree._from_interaction = _patched_from_interaction

# Also patch view dispatch for button/dropdown interactions
_original_view_dispatch = discord.ui.view.ViewStore.dispatch_view

def _patched_view_dispatch(self, component_type, custom_id, interaction):
    if not _gate_component(interaction):
        return
    bot = getattr(interaction, "client", None) or getattr(interaction, "_client", None)
    if bot:
        _apply_mimic(bot, interaction)
    _original_view_dispatch(self, component_type, custom_id, interaction)

discord.ui.view.ViewStore.dispatch_view = _patched_view_dispatch

# Also patch modal dispatch
_original_modal_dispatch = discord.ui.view.ViewStore.dispatch_modal

def _patched_modal_dispatch(self, custom_id, interaction, components, resolved):
    if not _gate_component(interaction):
        return
    bot = getattr(interaction, "client", None) or getattr(interaction, "_client", None)
    if bot:
        _apply_mimic(bot, interaction)
    _original_modal_dispatch(self, custom_id, interaction, components, resolved)

discord.ui.view.ViewStore.dispatch_modal = _patched_modal_dispatch


# ── Intents ──────────────────────────────────────────────────────────────────
# All intents enabled so the bot can react to every guild event.
# Admin bots typically need the full set; trim down for production if desired.
intents = discord.Intents.all()


# ── Gated command tree ───────────────────────────────────────────────────────
class GatedCommandTree(app_commands.CommandTree):
    """The guild allowlist, applied to every slash command and autocomplete.

    Returning False from `interaction_check` makes discord.py set `command_failed`
    and return *silently* (see CommandTree._call), which Discord renders as "the
    application did not respond" a few seconds later. So the refusal is spoken
    here rather than left to the caller's error handler, which never runs on this
    path. Autocomplete is refused without a reply — it has no response type that
    can carry a sentence, and an empty list is the correct answer for a guild we
    do not serve.
    """

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not guild_gate.is_allowed_interaction(interaction):
            log.warning(
                "Guild gate: refused /%s from guild %s (user %s)",
                getattr(getattr(interaction, "command", None), "qualified_name", "?"),
                interaction.guild_id,
                getattr(interaction.user, "id", None),
            )
            if interaction.type is discord.InteractionType.application_command:
                await _refuse_interaction(interaction, guild_gate.REFUSAL)
            return False

        # Narrower, and only ever narrower — see role_gate's docstring on why it is
        # the mirror image of the allowlist above rather than part of it.
        if not role_gate.is_allowed_interaction(interaction):
            log.info(
                "Role gate: refused /%s from user %s in guild %s",
                getattr(getattr(interaction, "command", None), "qualified_name", "?"),
                getattr(interaction.user, "id", None),
                interaction.guild_id,
            )
            if interaction.type is discord.InteractionType.application_command:
                await _refuse_interaction(interaction, role_gate.REFUSAL)
            return False

        return True


# ── Bot subclass ─────────────────────────────────────────────────────────────
class GeneKermanBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(
            command_prefix=cfg.COMMAND_PREFIX,
            intents=intents,
            owner_id=cfg.OWNER_ID or None,
            help_command=None,  # We provide our own in cogs/general.py
            tree_cls=GatedCommandTree,
        )
        self.mimic_map: dict[int, discord.Member] = {}
        self.extlog_enabled = False
        # Prefix commands (!help) are dispatched by ext.commands and never touch the
        # app-command tree, so the allowlist has to be added here too. A global check
        # is the whole surface: it runs for every prefix command in every cog.
        self.add_check(self._guild_gate_check)
        self.add_check(self._role_gate_check)

    async def _guild_gate_check(self, ctx: commands.Context) -> bool:
        """Guild allowlist for prefix commands. Silent by design: an unapproved
        server should get no reply at all from a `!` message, since answering one
        confirms the bot is listening and invites a second try."""
        if ctx.guild is None:
            return True                      # DMs, as elsewhere — see guild_gate
        if guild_gate.is_allowed_guild(ctx.guild.id):
            return True
        log.warning("Guild gate: refused prefix command in guild %s (user %s)",
                    ctx.guild.id, getattr(ctx.author, "id", None))
        return False

    async def _role_gate_check(self, ctx: commands.Context) -> bool:
        """Role gate for prefix commands. Silent for the same reason the guild one
        is: a `!` message that gets an answer confirms this instance is listening,
        which is the opposite of what an isolated test bot wants."""
        if role_gate.is_allowed_member(getattr(ctx, "author", None)):
            return True
        log.info("Role gate: refused prefix command from user %s in guild %s",
                 getattr(ctx.author, "id", None),
                 getattr(getattr(ctx, "guild", None), "id", None))
        return False

    async def close(self) -> None:
        """Flush buffered user data before shutting down so a /shutdown or console
        stop can't drop the last few minutes of unsaved XP/balance writes. Covers
        every shutdown path since they all funnel through close()."""
        try:
            from data.store import store
            await store.save_if_dirty()
            log.info("Flushed user data on shutdown.")
        except Exception as exc:
            log.error("Failed to flush user data on shutdown: %s", exc)
        try:
            from cost_guard import guard
            guard.flush()
        except Exception as exc:
            log.error("Failed to flush cost guard on shutdown: %s", exc)
        await super().close()

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    async def setup_hook(self) -> None:
        """Called once before the bot connects – load cogs, optionally sync commands."""
        cog_modules = [
            "cogs.general",
            "cogs.admin",
            "cogs.info",
            "cogs.xp",
            "cogs.economy",
            "cogs.corps",
            "cogs.gkchannels",
            "cogs.screenshots",
            "cogs.contracts",
            "cogs.auctions",
            "cogs.weeklymissions",
            "cogs.roles",
            "cogs.ksp_bridge",
            "cogs.account",
            "cogs.marketplace",   # tombstone only — the marketplace is the website's
            "cogs.contractcraft",
            "cogs.tickets",
            "cogs.costwatch",
        ]

        if cfg.ENABLE_MOD_COMMANDS:
            cog_modules.append("cogs.moderation")
            log.info("Moderation commands: ENABLED")
        else:
            log.info("Moderation commands: DISABLED (ENABLE_MOD_COMMANDS=false)")

        failed_cogs: list[str] = []
        for module in cog_modules:
            try:
                await self.load_extension(module)
                log.info("Loaded cog: %s", module)
            except Exception as exc:
                log.error("Failed to load cog %s: %s", module, exc, exc_info=True)
                failed_cogs.append(module)

        # The wallet must be in memory before this process serves anything.
        #
        # `cogs.xp.cog_load` is what awaits `store.load()`, and the loop above
        # deliberately swallows a cog failure so one broken feature cannot stop the
        # bot. For every other cog that is right; for this one it is not, and the
        # difference is that the store's failure mode is silent rather than absent.
        # A bot running on an unloaded store answers every balance as 0, accrues no
        # XP, never flushes, and says so nowhere a player can see — which is
        # indistinguishable from the wipe that `store.load()` refuses to allow, and
        # arrives as "the economy is broken" rather than as an outage. `store` blocks
        # its own writes in that state, so nothing is destroyed; what is missing is
        # someone noticing. This is that.
        #
        # `store.loaded` is a positive assertion, so it also catches the case where
        # `cogs.xp` failed before `store.load()` ever ran — an empty store whose
        # writes are NOT blocked, which is the more dangerous of the two.
        from data.store import store
        if not store.loaded and store.budget_blocked:
            # The one unloaded state we do NOT die on. `cost_guard` FROZEN is cleared
            # from the admin console, which is a route in this same process, and the
            # level is persisted — so exiting here would remove the only way back and
            # would do it on every restart, which under `Restart=always` is a crash
            # loop that anyone able to drive the Firebase bill can trigger. Under a
            # freeze every Firestore and Storage call is refused anyway, so there is
            # nothing here to overwrite while the owner raises the budget.
            log.critical(
                "Starting with an UNLOADED wallet because the Firebase cost guard is "
                "FROZEN. Balances read as zero and nothing is written. Clear the "
                "freeze or raise the budget from the admin console, then restart.")
        elif not store.loaded:
            raise RuntimeError(
                "The user wallet did not load, so this bot will not start.\n"
                + (f"Cogs that failed to load: {', '.join(failed_cogs)}\n" if failed_cogs else "")
                + "Nothing has been written and no record was changed. The store "
                  "blocks its own writes in this state. Check the CRITICAL line above "
                  "for the Firestore error, then start the bot again."
            )

        # If a command group is configured, wrap all commands under it
        # This must happen every boot so the tree matches what Discord expects
        if cfg.COMMAND_GROUP:
            group_name = cfg.COMMAND_GROUP.lower()
            log.info("Command group active: /%s …", group_name)

            # Subclass to add GK channel gating
            from cogs.gkchannels import is_gk_channel, is_mod, get_gk_channel_mentions
            from cogs.perms import real_user
            from i18n import tp

            class GKGroup(app_commands.Group):
                async def interaction_check(self, interaction: discord.Interaction) -> bool:
                    # DMs — allow
                    if interaction.guild is None:
                        return True
                    # Mods bypass — on the REAL invoker. This is an authority check
                    # like any other, so it goes through perms.real_user rather than
                    # the mimic-swapped interaction.user (see the invariant above).
                    ru = real_user(interaction)
                    if isinstance(ru, discord.Member) and is_mod(ru):
                        return True
                    # GK channels — allow
                    if is_gk_channel(interaction.guild_id, interaction.channel_id):
                        return True
                    # Block with ephemeral message
                    channels_str = get_gk_channel_mentions(interaction.guild)
                    await interaction.response.send_message(
                        tp(interaction.guild_id, interaction.user.id,
                           "gk.cmd_wrong_channel", channels=channels_str),
                        ephemeral=True,
                    )
                    return False

            parent = GKGroup(
                name=group_name,
                description="Boundless Missions bot commands",
            )

            admin_group = app_commands.Group(
                name="admin", 
                description="Admin commands",
                default_permissions=discord.Permissions(administrator=True)
            )
            mod_group = app_commands.Group(
                name="mod", 
                description="Moderation commands",
                default_permissions=discord.Permissions(kick_members=True)
            )
            info_group = app_commands.Group(name="info", description="Info commands")

            for cmd in list(self.tree.get_commands()):
                self.tree.remove_command(cmd.name)
                
                cog_name = getattr(cmd.binding, "qualified_name", "").lower()
                
                cmd_is_admin = False
                cmd_is_mod = False
                
                if cmd.default_permissions:
                    if getattr(cmd.default_permissions, "administrator", False):
                        cmd_is_admin = True
                    if getattr(cmd.default_permissions, "kick_members", False) or getattr(cmd.default_permissions, "manage_guild", False):
                        cmd_is_mod = True
                if any("mod_only" in getattr(c, "__qualname__", "") for c in getattr(cmd, "checks", [])):
                    cmd_is_mod = True
                
                if cog_name == "admin" or cmd_is_admin:
                    admin_group.add_command(cmd)
                elif cog_name == "moderation" or cmd_is_mod:
                    mod_group.add_command(cmd)
                elif cog_name == "info":
                    info_group.add_command(cmd)
                else:
                    parent.add_command(cmd)

            if info_group.commands:
                parent.add_command(info_group)

            self.tree.add_command(parent)
            
            # Add mod and admin to top-level so their default_permissions are respected by Discord
            if admin_group.commands:
                self.tree.add_command(admin_group)
            if mod_group.commands:
                self.tree.add_command(mod_group)

        if _SYNC_COMMANDS:
            await self._sync_commands()
        else:
            log.info("Skipping command sync (pass --sync to force)")

    async def _sync_commands(self) -> None:
        """Push the current command tree to Discord's API, to the allowed guilds only.

        This used to read `cfg.GUILD_IDS`, and when that was empty — the shipped
        `.env.example` default — it synced *globally*, registering every command in
        every server the bot had been added to. Registration was never the security
        boundary (guild_gate is), but a command that is not registered cannot be
        invoked at all, so the two should not disagree about where the bot works.

        The allowlist is never empty, so there is no global-sync branch any more;
        the global wipe stays, because a previous global sync has to be undone.
        """
        for guild_id in sorted(guild_gate.ALLOWED_GUILD_IDS):
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d commands to guild %d", len(synced), guild_id)

        # Wipe global commands on Discord's side by clearing internal global commands
        # AFTER we already copied them to the guild.
        self.tree.clear_commands(guild=None)
        await self.tree.sync(guild=None)
        log.info("Wiped any leftover global commands.")
        
        self.tree.on_error = self.on_app_command_error

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Leave any server that is not on the allowlist, immediately.

        Safe to do unconditionally, unlike the boot sweep: a guild we have just
        joined holds nothing of ours — no corps, no tickets, no guild_config — so
        there is no state to destroy, and this is the exact case guild_gate exists
        for. The interaction gate has already made us inert here, so a failed
        leave() degrades to "present but useless" rather than to the old behaviour.
        """
        if guild_gate.is_allowed_guild(guild.id):
            log.info("Joined allowed guild %s (%s)", guild.name, guild.id)
            return
        log.warning(
            "Guild gate: left %s (%s, owner %s), not on the allowlist",
            guild.name, guild.id, getattr(guild.owner, "id", "?"),
        )
        try:
            await guild.leave()
        except Exception as exc:
            log.error("Guild gate: could not leave %s (%s): %s", guild.name, guild.id, exc)

    async def _sweep_disallowed_guilds(self) -> None:
        """Report guilds we are in but not allowed in. Leaves only on request.

        Deliberately not symmetrical with on_guild_join. A guild we were already in
        when the allowlist landed may hold corp channels, open tickets and a
        guild_config nobody can reconstruct, and a restart that silently abandoned
        one would be a destructive act taken by a deploy rather than by a person.
        The interaction gate has already stopped us serving it, so reporting is
        sufficient and leaving is a separate, opt-in decision
        (GUILD_GATE_LEAVE_ON_BOOT).
        """
        stray = [g for g in self.guilds if not guild_gate.is_allowed_guild(g.id)]
        if not stray:
            return
        for g in stray:
            log.warning("Guild gate: in non-allowed guild %s (%s, %d members), "
                        "commands are refused there", g.name, g.id, g.member_count or 0)
        if not guild_gate.leave_on_boot():
            log.warning(
                "Guild gate: %d non-allowed guild(s). Not leaving: set "
                "GUILD_GATE_LEAVE_ON_BOOT=true to leave them, or add them to "
                "EXTRA_ALLOWED_GUILD_IDS to serve them.", len(stray))
            return
        for g in stray:
            try:
                await g.leave()
                log.warning("Guild gate: left %s (%s) on boot", g.name, g.id)
            except Exception as exc:
                log.error("Guild gate: could not leave %s (%s): %s", g.name, g.id, exc)

    async def on_ready(self) -> None:
        log.info("=" * 50)
        log.info("Bot ready!  Logged in as %s (ID: %s)", self.user, self.user.id)
        log.info("Guilds: %d", len(self.guilds))
        log.info("Guild gate: %s", guild_gate.describe())
        # ERROR, not info, when the gate is on with nothing listed: that instance
        # answers nobody, and this line is the only thing that will say why.
        (log.error if role_gate.misconfigured() else log.info)(
            "Role gate: %s", role_gate.describe())
        log.info("=" * 50)
        await self._sweep_disallowed_guilds()
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.playing,
                name="/b",
                state="Managing Missions",
            )
        )
        # Register the bot handle and id with api_server. Deliberately *not* gated on
        # KSP_API_ENABLED: `contract_actions` reads both through api_server, and it now
        # backs the Discord contract buttons as well as the KSP endpoints. Left behind
        # the gate, an install with the API turned off would treat weekly contracts as
        # human-issued (bot id 0 matches nobody) and could not deliver the settle /
        # more-time / dispute hand-off DMs at all.
        from api_server import set_bot_user_id, set_bot_instance
        set_bot_user_id(self.user.id)
        set_bot_instance(self)
        log.info("Bot user ID registered with api_server: %s", self.user.id)

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if getattr(self, "extlog_enabled", False):
            real_user = interaction.extras.get("_mimic_real_user", interaction.user)
            spoofed_user = interaction.user
            spoof_str = f" (Mimicking {spoofed_user})" if getattr(real_user, "id", None) != getattr(spoofed_user, "id", None) else ""
            
            if interaction.type == discord.InteractionType.component:
                custom_id = interaction.data.get("custom_id")
                print(f"[ExtLog] {real_user}{spoof_str} clicked button/select: {custom_id}")
            elif interaction.type == discord.InteractionType.application_command:
                command = interaction.data.get("name")
                print(f"[ExtLog] {real_user}{spoof_str} used command: /{command}")
            elif interaction.type == discord.InteractionType.modal_submit:
                custom_id = interaction.data.get("custom_id")
                print(f"[ExtLog] {real_user}{spoof_str} submitted modal: {custom_id}")

    @staticmethod
    def _budget_stop(error: BaseException) -> "FirebaseBudgetExceeded | None":
        """The FirebaseBudgetExceeded behind a command error, if that's what this
        is. discord.py wraps handler exceptions in CommandInvokeError, so the
        cause chain has to be walked rather than the top-level type checked."""
        from cost_guard import FirebaseBudgetExceeded

        seen = 0
        cursor: BaseException | None = error
        while cursor is not None and seen < 8:
            if isinstance(cursor, FirebaseBudgetExceeded):
                return cursor
            cursor = getattr(cursor, "original", None) or cursor.__cause__
            seen += 1
        return None

    async def on_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        budget = self._budget_stop(error)
        if budget is not None:
            # Not a fault, and not news to the maintainer — cogs/costwatch.py
            # DMs them the moment the level changes. Just say what's happening.
            await ctx.send(f"🛑 {budget}")
            return
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ You don't have permission to use that command.")
        elif isinstance(error, commands.CommandNotFound):
            pass  # silently ignore unknown prefix commands
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"⚠️ Missing argument: `{error.param.name}`")
        else:
            log.error("Unhandled command error: %s", error, exc_info=True)
            try:
                maintainer = self.get_user(815228135049527297) or await self.fetch_user(815228135049527297)
                if maintainer:
                    await maintainer.send(f"⚠️ **Error in prefix command `{ctx.command.name if ctx.command else 'Unknown'}`:**\n```py\n{error}\n```")
            except Exception as exc:
                log.error("Failed to notify maintainer: %s", exc)
            await ctx.send("💥 An unexpected error occurred. The maintainer (<@815228135049527297>) has been pinged via DM.")

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        # Imported here, not at module level: setup_hook does the same, and i18n
        # loads its string tables on import. The CheckFailure branch below used to
        # reach for `tp` bound only inside setup_hook, so every refused permission
        # check raised NameError instead of saying "no permission".
        from i18n import tp

        budget = self._budget_stop(error)
        if budget is not None:
            # A spending stop is a known state, not a crash. Saying "unexpected
            # error" here (and DMing the maintainer for every command anyone runs
            # while frozen) buries the one message that actually explains it.
            log.warning("Command refused by the cost guard: %s", error)
            msg = f"🛑 {budget}"
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
            return

        if isinstance(error, app_commands.CheckFailure):
            # A permission check said no. discord.py runs this handler after the
            # cog's own one regardless of what it did, so a refusal used to arrive
            # as "no permission" followed by "an unexpected error occurred" and a
            # DM to the maintainer for every non-mod who tried a mod command.
            # Only answer if the cog has not.
            # Prefer the sentence the check itself raised. Several checks build a
            # specific one — which permission is missing (cogs/moderation.mod_only),
            # or that no mod role is mapped in this server (cogs/perms) — and the
            # generic "no permission" used to discard all of it, sending an owner
            # hunting for a permission problem that was really a configuration one.
            detail = str(error).strip()
            msg = detail or tp(interaction.guild_id, interaction.user.id, "common.no_perm")
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            return

        log.error("Unhandled app command error: %s", error, exc_info=True)
        try:
            maintainer = self.get_user(815228135049527297) or await self.fetch_user(815228135049527297)
            if maintainer:
                await maintainer.send(f"⚠️ **Error in slash command `{interaction.command.name if interaction.command else 'Unknown'}`:**\n```py\n{error}\n```")
        except Exception as exc:
            log.error("Failed to notify maintainer: %s", exc)
        msg = "💥 An unexpected error occurred. The maintainer (<@815228135049527297>) has been pinged via DM."
        if not interaction.response.is_done():
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            await interaction.followup.send(msg, ephemeral=True)


# ── Runner ───────────────────────────────────────────────────────────────────
async def main() -> None:
    bot = GeneKermanBot()
    loop = asyncio.get_running_loop()

    def console_reader():
        """Read console commands on a daemon thread.

        Deliberately NOT loop.run_in_executor: sys.stdin.readline blocks until
        input, and a blocked default-executor thread would stall asyncio.run()'s
        shutdown_default_executor() forever whenever the bot is closed any other
        way (e.g. the /shutdown command). A daemon thread never blocks exit.
        """
        for line in sys.stdin:
            cmd = line.strip().lower()
            if cmd == "stop":
                log.info("Stop command received from console. Shutting down...")
                asyncio.run_coroutine_threadsafe(bot.close(), loop)
                return
            elif cmd == "extlog":
                bot.extlog_enabled = not getattr(bot, "extlog_enabled", False)
                state = "ON" if bot.extlog_enabled else "OFF"
                print(f"[ExtLog] Extensive logging is now {state}")
                log.info("Extensive logging is now %s", state)

    threading.Thread(target=console_reader, name="console", daemon=True).start()

    api_server = None

    async with bot:
        # Start KSP API server alongside the bot
        if cfg.KSP_API_ENABLED:
            import uvicorn
            from api_server import app as api_app

            # Security gates that are OFF get shouted about on every start. These are
            # meant to be flipped off briefly during development — a rebuild changes
            # the DLL hash, so the version gate blocks the build you are testing — and
            # the failure mode is forgetting to flip them back, which ships a server
            # that accepts outdated and modified clients without challenge.
            # Derived from the register in config.py, never re-listed here: this
            # list used to be maintained by hand and had fallen two gates behind.
            disabled_gates = insecure_gates()
            if disabled_gates:
                banner = "  ".join(disabled_gates)
                print("\n" + "!" * 72)
                print("!!  SECURITY GATES DISABLED: DO NOT RUN THIS IN PRODUCTION")
                print(f"!!  {banner}")
                print("!!  Set these back to true in .env before public deployment.")
                print("!" * 72 + "\n")
                log.warning("SECURITY GATES DISABLED: %s, dev only, re-enable before deployment.",
                            ", ".join(disabled_gates))

            # Serve HTTPS directly if a cert+key are configured; otherwise plain
            # HTTP (fine on localhost or behind a TLS-terminating reverse proxy).
            ssl_kwargs = {}
            scheme = "http"
            if cfg.API_SSL_CERTFILE and cfg.API_SSL_KEYFILE:
                ssl_kwargs = {
                    "ssl_certfile": cfg.API_SSL_CERTFILE,
                    "ssl_keyfile": cfg.API_SSL_KEYFILE,
                }
                scheme = "https"

            api_config = uvicorn.Config(
                api_app,
                host=cfg.API_HOST,
                port=cfg.API_PORT,
                log_level="info",
                access_log=False,
                # This server is a task inside the Discord bot's own process and
                # event loop, so anything that exhausts it here also takes the bot
                # down — moderation, tickets and auctions with it. A concurrency
                # ceiling turns that into 503s for the excess rather than a dead
                # process.
                #
                # It must stay well above what the per-user WebSocket cap permits:
                # uvicorn counts a live WS against this, every linked player holds at
                # least one for their whole session, and NotificationHub.MAX_PER_USER
                # allows eight — so a ceiling near the player count would answer 503
                # to every HTTP request at ordinary load, which is the outcome this
                # is meant to prevent. The per-user cap is the control that bounds
                # sockets; this only stops runaway.
                limit_concurrency=2048,
                **ssl_kwargs,
            )
            api_server = uvicorn.Server(api_config)
            asyncio.create_task(api_server.serve())
            log.info("KSP API server starting on %s://%s:%d", scheme, cfg.API_HOST, cfg.API_PORT)
        else:
            log.info("KSP API server: DISABLED (KSP_API_ENABLED=false)")

        try:
            await bot.start(cfg.TOKEN)
        finally:
            # Bot has stopped — tell uvicorn to leave its serve loop so the process
            # can actually terminate instead of hanging on a live server task.
            if api_server is not None:
                api_server.should_exit = True


if __name__ == "__main__":
    asyncio.run(main())
