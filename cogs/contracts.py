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
from data.store import store, _db
from data import contracts as cdb
from i18n import t, tp, S
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
    # Rescue stats / leaderboard
    "rescue.stat.title":  {"en": "🛟 {name}'s Rescues"},
    "rescue.stat.desc":   {"en": "Completed rescue missions: **{count}**"},
    "rescue.lb.title":    {"en": "🛟 Rescue Leaderboard"},
    "rescue.lb.empty":    {"en": "No rescues completed yet. Be the first to bring someone home!"},
    "rescue.lb.line":     {"en": "{prefix} **{name}** · `{count}` rescue(s)"},
})


class Contracts(commands.Cog, name="Contracts"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.dispute_timeout_loop.start()

    def cog_unload(self):
        self.dispute_timeout_loop.cancel()

    @app_commands.command(name="contractreset", description="[MOD] Cancel all active contracts for a user")
    @app_commands.describe(user="The user whose contracts should be cancelled")
    @app_commands.default_permissions(manage_guild=True)
    async def contractreset(self, interaction: discord.Interaction, user: discord.Member):
        gid = interaction.guild_id
        await interaction.response.defer(ephemeral=True)

        active_statuses = {cdb.PENDING, cdb.ACTIVE, cdb.SUBMITTED, cdb.DISPUTED, cdb.MOD_REVIEW}
        cancelled = 0
        refunded = 0

        for c in cdb.iter_user_contracts(gid, user.id):
            if c.get("status") in active_statuses:
                cdb.update_contract(gid, c["contract_id"], status=cdb.CANCELLED)
                # Refund escrow to issuer (if issuer is not the bot)
                if str(c.get("issuer_id")) != str(interaction.client.user.id):
                    await store.add_balance(gid, int(c["issuer_id"]), c["payment"])
                    refunded += c["payment"]
                # A rescue's wreck was deleted from the issuer's save when the contract
                # was created. Wiping the contract without this leaves their ship gone
                # for good, with nothing left pointing at the snapshot that could
                # restore it. This bulk tool cancels statuses `contract_actions.cancel`
                # deliberately refuses (submitted, disputed, mod_review), so it cannot
                # simply delegate — but it owes the same clean-up.
                await ca.restore_rescue(gid, c["contract_id"], c)
                cancelled += 1

        # Also clear weekly mission selections for this user
        sel_col = _db.collection("guilds").document(str(gid)).collection("weekly_selections")
        selections_cleared = 0
        for doc in sel_col.stream():
            d = doc.to_dict()
            if d.get("user_id") == str(user.id):
                doc.reference.delete()
                selections_cleared += 1

        sym = settings.CURRENCY_SYMBOL
        await interaction.followup.send(
            f"✅ Cancelled **{cancelled}** contract(s) for {user.mention}. "
            f"Refunded **{refunded}** {sym}. Cleared **{selections_cleared}** mission selection(s).",
            ephemeral=True,
        )
        log.info("%s reset contracts for %s: %d cancelled, %d refunded, %d selections cleared",
                 interaction.user, user, cancelled, refunded, selections_cleared)

    # ── Background: close disputes nobody resolved ───────────────────────────
    #
    # A dispute is the one state with no natural end. Every other status is driven
    # forward by somebody who wants something — but a contractor who owes a fine wants
    # exactly nothing to happen, and before this loop nothing ever did.
    #
    # Half-hourly rather than by-the-minute: the deadline is measured in days, so the
    # worst case is being fined 30 minutes late, and a tighter loop would just re-query
    # Firestore for no one's benefit.
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

    # ── /rescues ────────────────────────────────────────────────────────────
    @app_commands.command(name="rescues", description="Show how many rescue missions a user has completed")
    @app_commands.describe(user="User to look up (defaults to yourself)")
    async def rescues(self, interaction: discord.Interaction, user: discord.Member | None = None):
        gid = interaction.guild_id
        target = user or interaction.user
        count = store.get_user(gid, target.id).get("rescues", 0)
        embed = discord.Embed(
            title=tp(gid, interaction.user.id, "rescue.stat.title", name=target.display_name),
            description=tp(gid, interaction.user.id, "rescue.stat.desc", count=count),
            color=discord.Color.blue(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        await interaction.response.send_message(embed=embed, ephemeral=user is None)

    # ── /rescueboard ────────────────────────────────────────────────────────
    @app_commands.command(name="rescueboard", description="View the rescue-mission leaderboard")
    async def rescueboard(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        lb = [(uid, d) for uid, d in store.leaderboard(gid, key="rescues", limit=9999)
              if d.get("rescues", 0) > 0][:settings.LEADERBOARD_PAGE_SIZE]
        if not lb:
            await interaction.response.send_message(t(gid, "rescue.lb.empty"), ephemeral=True)
            return

        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, (uid, data) in enumerate(lb):
            prefix = medals[i] if i < 3 else f"`{i + 1}.`"
            member = interaction.guild.get_member(int(uid))
            name = member.display_name if member else f"User {uid}"
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

