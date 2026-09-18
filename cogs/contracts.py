"""
cogs/contracts.py – Player-to-player contract system (Discord side).

Contracts are **created in the KSP mod or on the website**, never here: the old
`/contract` and `/flagcontract` commands were retired because a contract written
from Discord could not carry the half that matters — the craft on the build stage,
the mod list, the orbit and Δv margins a mission is judged against. What is left
here is the part Discord is actually good at: the offer/dispute/review buttons that
land in a DM (see `cogs/contract_views.py`), the mod clean-up tool, and the rescue
leaderboard. `contract_actions.py` is the shared service all front ends call.
"""
import asyncio
import logging
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks

import settings
from cogs import perms, targets
from data.store import store, _db
from data import contracts as cdb
from i18n import t, tp, S
from cogs import contract_views as cv
import contract_actions as ca

log = logging.getLogger(__name__)

# ── i18n ─────────────────────────────────────────────────────────────────────
S.update({
    "ct.title":          {"en": "Contract"},
    "ct.mission":        {"en": "📋 Mission"},
    "ct.issuer":         {"en": "👤 Issuer"},
    "ct.contractor":     {"en": "🔧 Contractor"},
    "ct.payment":        {"en": "💰 Payment"},
    "ct.fine":           {"en": "⚠️ Fine"},
    "ct.due":            {"en": "📅 Due"},
    "ct.status":         {"en": "📌 Status"},
    "ct.review_title":   {"en": "Submission Review"},
    "ct.accepted":       {"en": "Contract Accepted!"},
    "ct.accepted_desc":  {"en": "**{payment}** {sym} transferred to your account."},
    "ct.disputed":       {"en": "Submission Refused"},
    "ct.disputed_desc":  {"en": "The other party refused your submission. Use one of the options below."},
    "ct.settle_request": {"en": "Settlement Request"},
    "ct.settle_desc":    {"en": "**{name}** is requesting a settlement (no exchange)."},
    "ct.settle_sent":    {"en": "✅ Settlement request sent."},
    "ct.settled":        {"en": "Settled. Escrow refunded."},
    "ct.settle_refused": {"en": "Settlement refused."},
    "ct.mod_review":     {"en": "Mod Review"},
    "ct.sued":           {"en": "⚖️ Case escalated to moderators."},
    "ct.fine_paid":      {"en": "Fine paid."},
    "ct.no_funds":       {"en": "❌ Insufficient balance."},
    "ct.moretime_request":{"en": "Time Extension Request"},
    "ct.moretime_desc":  {"en": "**{name}** is requesting a deadline extension.\nCurrent: **{old}** → New: **{new}**"},
    # Submissions awaiting the issuer's decision (/submissions)
    "ct.rv.none":        {"en": "✅ Nothing is waiting on you. No contract you issued has a submission to review."},
    "ct.rv.header":      {"en": "📤 **{count}** submission(s) awaiting your review. Accept or refuse each below."},
    "ct.rv.more":        {"en": "…and **{count}** more. Deal with these first, then run the command again."},
    "ct.rv.error":       {"en": "⚠️ Couldn't read your contracts just now. Nothing has changed. Try again in a moment."},
    # Rescue stats / leaderboard
    "rescue.stat.title":  {"en": "🛟 {name}'s Rescues"},
    "rescue.stat.desc":   {"en": "Completed rescue missions: **{count}**"},
    "rescue.lb.title":    {"en": "🛟 Rescue Leaderboard"},
    "rescue.lb.empty":    {"en": "No rescues completed yet. Be the first to bring someone home!"},
    "rescue.lb.line":     {"en": "{prefix} **{name}** · `{count}` rescue(s)"},
})


# How many review cards one `/submissions` sends. Each card is its own ephemeral
# message because a Discord view belongs to one message and every contract needs
# its own ✅/❌ pair; five is a readable burst rather than a wall, and the rest are
# counted out loud so nobody thinks the list is complete when it is not.
REVIEW_CARDS_PER_PAGE = 5


def mod_only():
    """The in-code gate. `@default_permissions` on its own is only a *default*:
    any server administrator can hand the command to any role — or @everyone —
    in Server Settings → Integrations, and the bot then runs it with no check
    of its own. Gates on the real invoker (mimic-safe), like every other
    moderator command."""
    async def predicate(interaction: discord.Interaction) -> bool:
        return perms.is_mod_user(interaction)
    return app_commands.check(predicate)


class Contracts(commands.Cog, name="Contracts"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.dispute_timeout_loop.start()
        self.mod_review_timeout_loop.start()
        self.overdue_loop.start()
        self.gift_sweep_loop.start()
        self.repair_bot_issuer.start()

    def cog_unload(self):
        self.dispute_timeout_loop.cancel()
        self.mod_review_timeout_loop.cancel()
        self.overdue_loop.cancel()
        self.gift_sweep_loop.cancel()
        self.repair_bot_issuer.cancel()

    async def cog_app_command_error(self, interaction: discord.Interaction,
                                    error: app_commands.AppCommandError) -> None:
        # A refused check is an answer, not a crash: without this the bot-wide
        # handler reports "an unexpected error" and pages the maintainer.
        if isinstance(error, app_commands.CheckFailure):
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    tp(interaction.guild_id, interaction.user.id, "common.no_perm"),
                    ephemeral=True)
        # Anything else is left to the bot-wide handler, which discord.py runs
        # after this one regardless (`CommandTree._dispatch_error`).

    @app_commands.command(name="contractreset", description="[MOD] Cancel all active contracts for a user")
    @app_commands.describe(user="The user whose contracts should be cancelled",
                           username=targets.USERNAME_DESC)
    @app_commands.default_permissions(manage_guild=True)
    @perms.global_records_mod_only()
    @targets.username_param
    async def contractreset(self, interaction: discord.Interaction,
                            user: discord.Member | None = None,
                            username: str | None = None):
        gid = interaction.guild_id
        await interaction.response.defer(ephemeral=True)
        try:
            tgt = await targets.resolve(interaction, user, username)
        except targets.TargetError as err:
            await targets.reject(interaction, err)
            return
        # Guild-local authority, global records: refuse a target this
        # moderator's server does not cover. See `perms.moderatable_here`.
        _no = perms.moderatable_here(interaction, tgt)
        if _no:
            await interaction.edit_original_response(content=_no)
            return

        cancelled = 0
        refunded = 0

        # The list is a snapshot; the decision is not. Each contract is cancelled by
        # `ca.mod_reset`, which re-reads it under `contract_lock` and re-checks the
        # status there — a review/auto-accept/cancel that lands while the thread
        # below is out closes the contract first and the reset then skips it,
        # rather than writing CANCELLED over COMPLETED and refunding an escrow that
        # was just paid out. The snapshot's status is only a pre-filter, so a
        # contract that finished long ago is not locked and re-read for nothing.
        reset_ids: set[str] = set()
        for c in await asyncio.to_thread(cdb.iter_user_contracts, gid, tgt.account_id):
            if c.get("status") not in ca.MOD_RESET_STATUSES:
                continue
            r = await ca.mod_reset(gid, c["contract_id"],
                                   actor_name=interaction.user.display_name)
            if not r.ok:
                log.info("contractreset: skipped %s (%s)", c["contract_id"], r.message)
                continue
            refunded += r.data.get("refunded", 0)
            reset_ids.add(str(c["contract_id"]))
            cancelled += 1

        # Also clear weekly mission selections for this user. The claim is keyed on
        # the account and is guild-independent (see weeklymissions._selection_ref) —
        # a per-guild claim let a player in two guilds select the same mission twice
        # and be paid twice — so this queries by user rather than streaming a guild's
        # collection, and clears the player's selections wherever they were made.
        #
        # But ONLY the claims whose contract this command just cancelled. The claim is
        # the single guard against a weekly mission being selected — and therefore paid
        # — twice, and a bot-issued contract has no escrow behind it, so a second payout
        # is a straight mint. Deleting every claim the account held (which is what this
        # did) re-opened missions already COMPLETED and paid this week, one player at a
        # time. Note the loop above is careful about exactly this and says so; this loop
        # had no way to be, because nothing on the claim named its contract. It does now
        # (`weeklymissions.link_selection_contract`).
        #
        # A claim written before that field existed carries no `contract_id`. Those are
        # SKIPPED rather than deleted: the cost of keeping one is a player who cannot
        # re-select a mission this week, which a moderator can see and explain; the cost
        # of deleting one is minting the reward again, which nobody sees. Reported
        # separately so the moderator knows the difference.
        sel_col = _db.collection("weekly_selections")
        selections_cleared = 0
        selections_kept = 0
        docs = await asyncio.to_thread(
            lambda: list(sel_col.where("user_id", "==", tgt.account_id).stream()))
        for doc in docs:
            cid = (doc.to_dict() or {}).get("contract_id")
            if cid and str(cid) in reset_ids:
                doc.reference.delete()
                selections_cleared += 1
            else:
                selections_kept += 1

        sym = settings.CURRENCY_SYMBOL
        await interaction.followup.send(
            f"✅ Cancelled **{cancelled}** contract(s) for {tgt.mention}. "
            f"Refunded **{refunded}** {sym}. Cleared **{selections_cleared}** mission selection(s)."
            + (f" Kept **{selections_kept}** whose mission was already finished or "
               f"predates selection tracking. Clearing those would pay the reward twice."
               if selections_kept else ""),
            ephemeral=True,
        )
        log.info("%s reset contracts for %s (%s): %d cancelled, %d refunded, %d selections cleared",
                 interaction.user, tgt.label, tgt.account_id,
                 cancelled, refunded, selections_cleared)

    # ── Background: close disputes nobody resolved ───────────────────────────
    #
    # A dispute is the one state with no natural end. Every other status is driven
    # forward by somebody who wants something — but a contractor who owes a fine wants
    # exactly nothing to happen, and before this loop nothing ever did.
    #
    # Half-hourly rather than by-the-minute: the deadline is measured in days, so the
    # worst case is being fined 30 minutes late, and a tighter loop would just re-query
    # Firestore for no one's benefit.
    # A one-shot repair for contracts written while the bot's own id was unknown.
    #
    # The API starts serving before `on_ready`, so `_get_bot_user_id()` answered 0
    # during the login window and a weekly mission selected in it was written with
    # `issuer_id = "0"` — an id no wallet has. Such a contract can never pay its
    # contractor, is not recognised as bot-issued (so nothing auto-reviews it), and
    # still charges its fine to the player on a give-up, crediting nothing. The
    # window is closed now (`select_mission` refuses while the id is unset), but the
    # documents it already wrote are sitting in Firestore.
    #
    # Runs once per start rather than behind a flag doc: it is a single indexed
    # equality query that matches nothing once the repair is done, and running it
    # every start means the same accident happening again repairs itself. Only
    # non-terminal contracts are rewritten — a settled one has already moved
    # whatever money it was going to move, and changing its issuer now would only
    # misreport history — but terminal ones are counted and logged so the scale is
    # visible.
    @tasks.loop(count=1)
    async def repair_bot_issuer(self):
        bot_uid = self.bot.user.id if self.bot.user else 0
        if not bot_uid:
            log.error("Bot-issuer repair skipped: the bot's own id is still unknown.")
            return
        try:
            broken = await asyncio.to_thread(cdb.list_by_issuer, "0")
        except Exception as exc:
            log.error("Bot-issuer repair could not query contracts: %s", exc)
            return
        if not broken:
            return

        repaired = settled = failed = 0
        for c in broken:
            cid = c.get("contract_id")
            if c.get("status") not in cdb.ACTIVE_STATUSES:
                settled += 1
                continue
            try:
                await asyncio.to_thread(
                    cdb.update_contract, int(c.get("guild_id") or 0), cid,
                    issuer_id=str(bot_uid), issuer_name="Boundless Missions")
                repaired += 1
            except Exception as exc:
                failed += 1
                log.error("Bot-issuer repair failed on %s: %s", cid, exc)
        log.warning("Bot-issuer repair: %d contract(s) reassigned to the bot, "
                    "%d already settled (left as they are), %d failed.",
                    repaired, settled, failed)

    @repair_bot_issuer.before_loop
    async def _wait_ready_repair(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=30)
    async def dispute_timeout_loop(self):
        try:
            disputed = await asyncio.to_thread(cdb.list_by_status, cdb.DISPUTED)
        except Exception as exc:
            log.error("Dispute sweep could not list contracts: %s", exc)
            return

        now = datetime.utcnow()
        for c in disputed:
            deadline = ca.auto_fine_at(c)
            # None means the dispute predates the clock. expire_dispute stamps it and
            # returns without charging, so those get a full window from now.
            if deadline is not None and now < deadline:
                continue
            try:
                await ca.expire_dispute(int(c.get("guild_id") or 0), c["contract_id"])
            except Exception as exc:
                # One bad contract must not stop the sweep for every other one.
                log.error("Dispute sweep failed on %s: %s", c.get("contract_id"), exc)

    @dispute_timeout_loop.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    # ── Background: give MOD_REVIEW an ending ────────────────────────────────
    #
    # DISPUTED had a clock and MOD_REVIEW did not, so the state a contractor reaches by
    # pressing Sue — free, unilateral, and refusing every other transition once there —
    # locked the issuer's escrow and a contract slot for both parties until a moderator
    # acted, or forever if none did. That is the same "nothing ever happens" failure the
    # dispute clock exists to close, one hop later.
    #
    # Daily, not half-hourly, for the reason the overdue sweep is: the window is measured
    # in days, so a 30-minute cadence would re-read the same handful 48 times a day.
    @tasks.loop(hours=24)
    async def mod_review_timeout_loop(self):
        try:
            reviewing = await asyncio.to_thread(cdb.list_by_status, cdb.MOD_REVIEW)
        except Exception as exc:
            log.error("Mod-review sweep could not list contracts: %s", exc)
            return

        now = datetime.utcnow()
        for c in reviewing:
            deadline = ca.mod_review_deadline(c)
            # None means it predates the stamp; expire_mod_review stamps it and returns
            # without charging, so those get a full window from now.
            if deadline is not None and now < deadline:
                continue
            try:
                await ca.expire_mod_review(int(c.get("guild_id") or 0), c["contract_id"])
            except Exception as exc:
                # One bad contract must not stop the sweep for every other one.
                log.error("Mod-review sweep failed on %s: %s", c.get("contract_id"), exc)

    @mod_review_timeout_loop.before_loop
    async def _wait_ready_mod_review(self):
        await self.bot.wait_until_ready()

    # ── Background: start the clock on contracts nobody submitted ────────────
    #
    # DISPUTED was the only status ever swept, so an ACTIVE contract whose deadline
    # passed sat there forever — the contractor had stopped answering, and the issuer
    # could only withdraw it, which cost the contractor nothing. `expire_overdue`
    # charges nothing either; it moves the contract into dispute, where the contractor
    # still has every option and the existing auto-fine clock provides the ending.
    #
    # Daily rather than half-hourly: the grace period is measured in days, so a sweep
    # every 30 minutes would re-read every active contract in the system 48 times to
    # find the same handful.
    @tasks.loop(hours=24)
    async def overdue_loop(self):
        try:
            active = await asyncio.to_thread(cdb.list_by_status, cdb.ACTIVE)
        except Exception as exc:
            log.error("Overdue sweep could not list contracts: %s", exc)
            return

        for c in active:
            try:
                await ca.expire_overdue(int(c.get("guild_id") or 0), c["contract_id"])
            except Exception as exc:
                # One bad contract must not stop the sweep for every other one.
                log.error("Overdue sweep failed on %s: %s", c.get("contract_id"), exc)

    @overdue_loop.before_loop
    async def _wait_ready_overdue(self):
        await self.bot.wait_until_ready()

    # Quicksend payloads whose offer nobody ever answered. Their files are
    # otherwise only removed by the recipient's accept or decline, and the upload
    # quota is a daily rate, so an offer to an account that never polls was
    # storage that grew forever. Daily: one Storage list per thousand objects.
    @tasks.loop(hours=24)
    async def gift_sweep_loop(self):
        from data import imports as imp
        days = int(getattr(settings, "GIFT_FILE_MAX_AGE_DAYS", 30) or 30)
        try:
            await asyncio.to_thread(imp.sweep_stale_gift_files, days)
        except Exception as exc:
            log.error("Gift file sweep failed: %s", exc)

    @gift_sweep_loop.before_loop
    async def _wait_ready_gifts(self):
        await self.bot.wait_until_ready()

    # ── /submissions ────────────────────────────────────────────────────────
    #
    # The corp-channel post `api_server._discord_notify_issuer` makes is the primary
    # route to the ✅/❌ buttons, and it is a *message*: it can be deleted, buried in
    # scrollback, or never sent at all — which is exactly what happened for as long as
    # that function raised NameError one statement before the send. A persistent view
    # whose message never existed is unreachable forever, so the issuer needs a second
    # route derived from the contracts themselves rather than from a post.
    #
    # It deliberately re-uses `ContractReviewView` instead of growing its own buttons.
    # `contract_actions.review` is the transition — it re-reads the contract under
    # `contract_lock` and re-checks the status there — so a contract that moved on
    # between this listing and the click is refused by the button with the same
    # sentence every other front end shows. The snapshot below is a pre-filter, never
    # a decision.
    @app_commands.command(
        name="submissions",
        description="Review contract submissions waiting on your decision")
    async def submissions(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        uid = interaction.user.id
        await interaction.response.defer(ephemeral=True)

        # `interaction.user.id`, and deliberately not the resolved account id: this is
        # exactly what `contract_views._actor` hands `ca.review` when one of the
        # buttons below is pressed, so listing by anything else could draw a card
        # whose own buttons would then refuse it as somebody else's contract. For
        # every account that exists today the two are the same string (see
        # data/accounts.py); the case where they diverge is a known gap in *every*
        # Discord contract button, not one this command introduces.
        try:
            mine = await asyncio.to_thread(cdb.iter_user_contracts, gid, uid)
        except Exception as exc:
            log.error("/submissions could not list contracts for %s: %s", uid, exc)
            await interaction.followup.send(tp(gid, uid, "ct.rv.error"), ephemeral=True)
            return

        # SUBMITTED is the status `ca.review` will both accept and refuse from. A
        # DISPUTED contract is also approvable there, but it has already been refused
        # once and its live buttons are the contractor's dispute options — listing it
        # as "awaiting review" would offer the issuer a Refuse that the service
        # rejects. The issuer_id test is not redundant: `iter_user_contracts` returns
        # the contracts where the caller is *either* party.
        pending = [c for c in mine
                   if c.get("status") == cdb.SUBMITTED
                   and str(c.get("issuer_id")) == str(uid)]
        if not pending:
            await interaction.followup.send(tp(gid, uid, "ct.rv.none"), ephemeral=True)
            return

        # Oldest first: the person who has been waiting longest is reviewed first, and
        # it is the stable half of the list across the re-runs paging asks for.
        pending.sort(key=lambda c: str(c.get("submitted_at") or c.get("created_at") or ""))

        await interaction.followup.send(
            tp(gid, uid, "ct.rv.header", count=len(pending)), ephemeral=True)

        for c in pending[:REVIEW_CARDS_PER_PAGE]:
            # The contract's *origin* guild, not the one the command was typed in: a
            # contract runs between users who may be in different servers, and the
            # origin guild is what every other front end passes to `ca.review` for
            # notification and channel routing (see the sweeps above).
            cgid = int(c.get("guild_id") or gid or 0)
            try:
                await interaction.followup.send(
                    embed=cv.submission_review_embed(c, cgid),
                    view=cv.ContractReviewView(c["contract_id"], cgid),
                    ephemeral=True,
                )
            except discord.HTTPException as exc:
                # One unrenderable contract (an over-long field from a document
                # written before the caps) must not take the whole list down.
                log.error("/submissions could not render %s: %s", c.get("contract_id"), exc)

        left = len(pending) - REVIEW_CARDS_PER_PAGE
        if left > 0:
            await interaction.followup.send(
                tp(gid, uid, "ct.rv.more", count=left), ephemeral=True)

    # ── /rescues ────────────────────────────────────────────────────────────
    @app_commands.command(name="rescues", description="Show how many rescue missions a user has completed")
    @app_commands.describe(user="User to look up (defaults to yourself)",
                           username=targets.USERNAME_DESC)
    @targets.username_param
    async def rescues(self, interaction: discord.Interaction,
                      user: discord.Member | None = None,
                      username: str | None = None):
        gid = interaction.guild_id
        own = user is None and not (username or "").strip()
        await interaction.response.defer(ephemeral=own)
        try:
            tgt = await targets.resolve(interaction, user, username, default_self=True)
        except targets.TargetError as err:
            await targets.reject(interaction, err)
            return
        count = store.get_user(gid, tgt.account_id).get("rescues", 0)
        embed = discord.Embed(
            title=tp(gid, interaction.user.id, "rescue.stat.title", name=tgt.label),
            description=tp(gid, interaction.user.id, "rescue.stat.desc", count=count),
            color=discord.Color.blue(),
        )
        if tgt.avatar_url:
            embed.set_thumbnail(url=tgt.avatar_url)
        await interaction.followup.send(embed=embed, ephemeral=own)

    # ── /rescueboard ────────────────────────────────────────────────────────
    @app_commands.command(name="rescueboard", description="View the rescue-mission leaderboard")
    async def rescueboard(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        lb = [(uid, d) for uid, d in store.leaderboard(gid, key="rescues", limit=9999)
              if d.get("rescues", 0) > 0][:settings.LEADERBOARD_PAGE_SIZE]
        if not lb:
            await interaction.response.send_message(t(gid, "rescue.lb.empty"), ephemeral=True)
            return
        await targets.prefetch_names(uid for uid, _ in lb)

        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, (uid, data) in enumerate(lb):
            prefix = medals[i] if i < 3 else f"`{i + 1}.`"
            name = targets.board_name(interaction.guild, uid)
            lines.append(t(gid, "rescue.lb.line", prefix=prefix, name=name, count=data.get("rescues", 0)))

        embed = discord.Embed(
            title=t(gid, "rescue.lb.title"),
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Contracts(bot))
    # Register DynamicItem button classes — regex-matched, survives restarts
    from cogs.contract_views import ALL_DYNAMIC_ITEMS
    bot.add_dynamic_items(*ALL_DYNAMIC_ITEMS)

