"""
cogs/economy.py – Economy system (KCoins).

Commands for checking balance, paying other users, and mod tools
for giving/fining/setting balances.
"""

import logging
import discord
from discord import app_commands
from discord.ext import commands

import settings
from cogs import perms
from cogs import targets
from data import accounts
from data.store import store
from i18n import S, t, tp

log = logging.getLogger(__name__)

# Debt is shown wherever a balance is, and every garnished credit says so. A player
# whose rewards silently halve files a bug report rather than an appeal — the same
# reasoning that makes the marketplace rating floor tell the seller.
S.update({
    # A read failure, NOT "no such player". The two need different answers: this one
    # says wait, a missing player says retype. Paying into the wrong wallet cannot be
    # undone by the player, so a lookup that could not complete refuses the transfer
    # rather than guessing the snowflake is the account id.
    "eco.pay.lookup_failed": {"en": "❌ Couldn't check one of those accounts just now. "
                                    "Nothing was transferred. Try again in a moment."},
    "eco.balance.debt":     {"en": "Unpaid fines"},
    "eco.balance.debt_val": {"en": "**{amount}** ({pct}% of what you earn goes to it)"},
    "eco.pay.garnished":    {"en": "{amount} {currency} of this went to {name}'s unpaid fines."},
    # Said out loud because a moderator otherwise cannot tell a delivered notice
    # from an undeliverable one, and will not chase it anywhere else. The two
    # reasons are kept apart: closed DMs are the player's own setting, no Discord
    # at all is a website account and needs a different channel entirely.
    "eco.fine.no_discord":  {"en": "⚠️ Not notified: this account has no Discord to message. Tell them another way."},
    "eco.fine.dm_closed":   {"en": "⚠️ Not notified: their DMs are closed."},
})


# NOTE: kept only for a future *guild-local* economy command. Every command in
# this cog today writes the global `users/{id}` wallet and so uses
# `perms.global_records_mod_only` instead — see the tier note in cogs/perms.py.
# Do not reach for this one for anything that moves coins.
def mod_only():
    """User must have Kick Members or Administrator permission. Gates on the real
    invoker (mimic-safe)."""
    async def predicate(interaction: discord.Interaction) -> bool:
        u = perms.real_user(interaction)
        if isinstance(u, discord.Member):
            return (u.guild_permissions.kick_members
                    or u.guild_permissions.administrator)
        return False
    return app_commands.check(predicate)


class Economy(commands.Cog, name="Economy"):
    """KCoins economy system."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── /balance ──────────────────────────────────────────────────────────────
    @app_commands.command(name="balance", description="Check your or another user's KCoin balance")
    @app_commands.describe(member="Member to check (defaults to yourself)",
                           username=targets.USERNAME_DESC)
    @targets.username_param
    async def balance(self, interaction: discord.Interaction,
                      member: discord.Member | None = None,
                      username: str | None = None) -> None:
        gid = interaction.guild_id
        uid = interaction.user.id
        await interaction.response.defer()
        try:
            tgt = await targets.resolve(interaction, member, username, default_self=True)
        except targets.TargetError as err:
            await targets.reject(interaction, err)
            return
        user = store.get_user(gid, tgt.account_id)

        embed = discord.Embed(
            title=tp(gid, uid, "eco.balance.title", symbol=settings.CURRENCY_SYMBOL, name=tgt.label),
            color=discord.Color.green(),
        )
        if tgt.avatar_url:
            embed.set_thumbnail(url=tgt.avatar_url)
        embed.add_field(
            name=settings.CURRENCY_NAME,
            value=f"**{user['balance']:,}**",
            inline=True,
        )
        embed.add_field(name=tp(gid, uid, "xp.rank.level"), value=f"**{user['level']}**", inline=True)
        # Only to the person who owes it. Balance and level for another player are
        # already public (the leaderboards show them), but a debt is the consequence of
        # a PENALTY, and this response is a non-ephemeral channel post — so
        # `/balance member:@them` put "owes 4,200 · 25% of earnings garnished" in front
        # of the server. The design note for the debt ledger says it is "said out loud"
        # so the debtor is not surprised by a halved payout; that is an argument for
        # telling the debtor, not for telling everyone else. Moderators keep the view,
        # since acting on it is their job.
        owed = store.debt_total(gid, tgt.account_id)
        may_see_debt = (tgt.account_id == str(interaction.user.id)
                        or perms.is_mod_user(interaction))
        if owed > 0 and may_see_debt:
            embed.add_field(
                name=tp(gid, uid, "eco.balance.debt"),
                value=tp(gid, uid, "eco.balance.debt_val", amount=f"{owed:,}",
                         pct=store.garnish_percent(gid, tgt.account_id)),
                inline=False,
            )
        embed.set_footer(text=tp(gid, uid, "eco.balance.footer", currency=settings.CURRENCY_NAME))
        await interaction.followup.send(embed=embed)

    # ── /pay ──────────────────────────────────────────────────────────────────
    @app_commands.command(name="pay", description="Transfer KCoins to another user")
    @app_commands.describe(member="Who to pay", amount="Amount to transfer")
    async def pay(self, interaction: discord.Interaction, member: discord.Member, amount: int) -> None:
        gid = interaction.guild_id
        uid = interaction.user.id
        # Validation
        if member.bot:
            await interaction.response.send_message(tp(gid, uid, "eco.pay.cant_bot"), ephemeral=True)
            return
        if member.id == interaction.user.id:
            await interaction.response.send_message(tp(gid, uid, "eco.pay.cant_self"), ephemeral=True)
            return
        if amount < settings.MIN_TRANSFER:
            await interaction.response.send_message(
                tp(gid, uid, "eco.pay.min", min=f"{settings.MIN_TRANSFER:,}", currency=settings.CURRENCY_NAME),
                ephemeral=True,
            )
            return

        # Both ends resolved to ACCOUNT IDS before a coin moves.
        #
        # `/pay` was the only money command that did not do this — `/givemoney`,
        # `/fine`, `/setbalance`, `/setxp` and `/contractreset` all go through
        # `targets.resolve`, and `/pay` was left out because it is a player transfer
        # rather than a mod tool. But that exclusion was about offering a `username:`
        # option; resolving the id is a correctness requirement, not a convenience.
        #
        # For a player who linked Discord onto an account they already had, the
        # snowflake carries an `account_discord` row pointing elsewhere. Paying
        # `member.id` there minted and credited a `users/{snowflake}` document that
        # the game, the website and `/balance` never read: the sender was debited,
        # the recipient received nothing, and both were shown a green "Transfer
        # Complete". The mirror case is worse to diagnose — such a player running
        # `/pay` was told "insufficient funds" while `/balance` showed real money.
        #
        # A failed lookup is REFUSED rather than falling back to the snowflake,
        # which is the rule `cogs/targets.py` states: "wait" and "retype" are
        # different answers, and paying into the wrong wallet is unrecoverable.
        sender_acct = accounts.account_for_discord(interaction.user.id)
        target_acct = accounts.account_for_discord(member.id)
        if sender_acct is None or target_acct is None:
            await interaction.response.send_message(
                tp(gid, uid, "eco.pay.lookup_failed"), ephemeral=True)
            return
        # Re-check self-payment on the resolved ids: two different snowflakes can
        # resolve to one account (a linked Discord plus the account's own), and the
        # snowflake comparison above would miss it.
        if str(sender_acct) == str(target_acct):
            await interaction.response.send_message(tp(gid, uid, "eco.pay.cant_self"), ephemeral=True)
            return

        # Execute transfer. Atomic debit so two concurrent /pay calls can't both
        # pass the balance check on the same funds and transfer more than is held.
        if not await store.try_debit(gid, sender_acct, amount,
                                     category=store.TX_TRANSFER_OUT,
                                     counterparty=str(target_acct)):
            sender = store.get_user(gid, sender_acct)
            await interaction.response.send_message(
                tp(gid, uid, "eco.pay.insufficient", balance=f"{sender['balance']:,}", currency=settings.CURRENCY_NAME),
                ephemeral=True,
            )
            return

        # Garnishable: a transfer is the obvious way round a debt otherwise — sell
        # through an alt, or have a friend hand the coins over. The sender is told
        # below how much of it went to the recipient's creditors.
        new_receiver_bal, _garnished = await store.add_balance_gross(
            gid, target_acct, amount, garnishable=True,
            category=store.TX_TRANSFER_IN,
            counterparty=str(sender_acct))
        new_sender_bal = store.get_user(gid, sender_acct)["balance"]

        embed = discord.Embed(
            title=t(gid, "eco.pay.title", symbol=settings.CURRENCY_SYMBOL),
            description=t(gid, "eco.pay.desc",
                sender=interaction.user.display_name, receiver=member.display_name,
                amount=f"{amount:,}", currency=settings.CURRENCY_NAME),
            color=discord.Color.green(),
        )
        embed.add_field(
            name=f"{interaction.user.display_name}",
            value=f"`{new_sender_bal:,}`",
            inline=True,
        )
        embed.add_field(
            name=f"{member.display_name}",
            value=f"`{new_receiver_bal:,}`",
            inline=True,
        )
        taken = sum(a for _cid, a in _garnished)
        if taken:
            embed.add_field(
                name="\u200b",
                value=t(gid, "eco.pay.garnished", amount=f"{taken:,}",
                        currency=settings.CURRENCY_NAME, name=member.display_name),
                inline=False,
            )
        await interaction.response.send_message(embed=embed)
        log.info("%s paid %s %d KCoins", interaction.user, member, amount)

    # ── /richest ──────────────────────────────────────────────────────────────
    @app_commands.command(name="richest", description="View the wealthiest members")
    async def richest(self, interaction: discord.Interaction) -> None:
        gid = interaction.guild_id
        lb = store.leaderboard(gid, key="balance")
        if not lb:
            await interaction.response.send_message(t(gid, "eco.richest.empty"), ephemeral=True)
            return
        await targets.prefetch_names(uid for uid, _ in lb)

        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for i, (uid, data) in enumerate(lb):
            if data.get("balance", 0) == 0:
                continue
            prefix = medals[i] if i < 3 else f"`{i + 1}.`"
            name = targets.board_name(interaction.guild, uid)
            lines.append(f"{prefix} **{name}** · {settings.CURRENCY_SYMBOL} `{data['balance']:,}`")

        if not lines:
            await interaction.response.send_message(t(gid, "eco.richest.empty"), ephemeral=True)
            return

        embed = discord.Embed(
            title=t(gid, "eco.richest.title", symbol=settings.CURRENCY_SYMBOL),
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed)

    # ══════════════════════════════════════════════════════════════════════════
    #  MOD-ONLY COMMANDS
    # ══════════════════════════════════════════════════════════════════════════

    # ── /givemoney ────────────────────────────────────────────────────────────
    @app_commands.command(name="givemoney", description="Give KCoins to a user (Mod only)")
    @app_commands.describe(member="Who to give to", username=targets.USERNAME_DESC,
                           amount="Amount to give", reason="Reason (optional)")
    @app_commands.default_permissions(kick_members=True)
    @perms.global_records_mod_only()
    @targets.username_param
    async def givemoney(
        self, interaction: discord.Interaction, amount: int,
        member: discord.Member | None = None, username: str | None = None,
        reason: str = "No reason provided",
    ) -> None:
        gid = interaction.guild_id
        uid = interaction.user.id
        if amount <= 0:
            await interaction.response.send_message(tp(gid, uid, "common.amount_positive"), ephemeral=True)
            return
        await interaction.response.defer()
        try:
            tgt = await targets.resolve(interaction, member, username)
        except targets.TargetError as err:
            await targets.reject(interaction, err)
            return
        # Guild-local authority, global records: refuse a target this
        # moderator's server does not cover. See `perms.moderatable_here`.
        _no = perms.moderatable_here(interaction, tgt)
        if _no:
            await interaction.edit_original_response(content=_no)
            return

        new_bal = await store.add_balance(gid, tgt.account_id, amount,
                                          category=store.TX_ADMIN,
                                          detail=reason or "Granted by a moderator",
                                          counterparty=str(interaction.user.id))
        embed = discord.Embed(
            title=t(gid, "eco.give.title", symbol=settings.CURRENCY_SYMBOL),
            description=t(gid, "eco.give.desc",
                name=tgt.label, amount=f"{amount:,}",
                currency=settings.CURRENCY_NAME, reason=reason, balance=f"{new_bal:,}"),
            color=discord.Color.green(),
        )
        embed.set_footer(text=t(gid, "common.issued_by", name=interaction.user.display_name))
        await interaction.followup.send(embed=embed)
        log.info("%s gave %s (%s) %d KCoins: %s",
                 interaction.user, tgt.label, tgt.account_id, amount, reason)

    # ── /fine ─────────────────────────────────────────────────────────────────
    @app_commands.command(name="fine", description="Deduct KCoins from a user (Mod only)")
    @app_commands.describe(member="Who to fine", username=targets.USERNAME_DESC,
                           amount="Amount to deduct", reason="Reason (optional)")
    @app_commands.default_permissions(kick_members=True)
    @perms.global_records_mod_only()
    @targets.username_param
    async def fine(
        self, interaction: discord.Interaction, amount: int,
        member: discord.Member | None = None, username: str | None = None,
        reason: str = "No reason provided",
    ) -> None:
        gid = interaction.guild_id
        uid = interaction.user.id
        if amount <= 0:
            await interaction.response.send_message(tp(gid, uid, "common.amount_positive"), ephemeral=True)
            return
        await interaction.response.defer()
        try:
            tgt = await targets.resolve(interaction, member, username)
        except targets.TargetError as err:
            await targets.reject(interaction, err)
            return
        # Guild-local authority, global records: refuse a target this
        # moderator's server does not cover. See `perms.moderatable_here`.
        _no = perms.moderatable_here(interaction, tgt)
        if _no:
            await interaction.edit_original_response(content=_no)
            return

        new_bal = await store.add_balance(gid, tgt.account_id, -amount,
                                          category=store.TX_ADMIN,
                                          detail=reason or "Fined by a moderator",
                                          counterparty=str(interaction.user.id))
        embed = discord.Embed(
            title=t(gid, "eco.fine.title"),
            description=t(gid, "eco.fine.desc",
                name=tgt.label, amount=f"{amount:,}",
                currency=settings.CURRENCY_NAME, reason=reason, balance=f"{new_bal:,}"),
            color=discord.Color.red(),
        )
        embed.set_footer(text=t(gid, "common.issued_by", name=interaction.user.display_name))

        # Tell the fined player. A website-only account has no DM to send to, and
        # a moderator who assumes one was sent will not follow it up anywhere else
        # — so the embed says outright when nobody was told, rather than leaving
        # the silent case looking identical to the delivered one.
        told = await tgt.dm(t(gid, "eco.fine.dm",
            guild=interaction.guild.name, amount=f"{amount:,}",
            currency=settings.CURRENCY_NAME, reason=reason))
        if not told:
            embed.add_field(
                name="\u200b",
                value=t(gid, "eco.fine.dm_closed" if tgt.can_dm else "eco.fine.no_discord"),
                inline=False)
        await interaction.followup.send(embed=embed)
        log.info("%s fined %s (%s) %d KCoins: %s",
                 interaction.user, tgt.label, tgt.account_id, amount, reason)

    # ── /setbalance ───────────────────────────────────────────────────────────
    @app_commands.command(name="setbalance", description="Set a user's KCoin balance (Mod only)")
    @app_commands.describe(member="Target member", username=targets.USERNAME_DESC,
                           amount="New balance amount")
    @app_commands.default_permissions(kick_members=True)
    @perms.global_records_mod_only()
    @targets.username_param
    async def setbalance(self, interaction: discord.Interaction, amount: int,
                         member: discord.Member | None = None,
                         username: str | None = None) -> None:
        gid = interaction.guild_id
        uid = interaction.user.id
        if amount < 0:
            await interaction.response.send_message(tp(gid, uid, "common.amount_negative"), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            tgt = await targets.resolve(interaction, member, username)
        except targets.TargetError as err:
            await targets.reject(interaction, err)
            return
        # Guild-local authority, global records: refuse a target this
        # moderator's server does not cover. See `perms.moderatable_here`.
        _no = perms.moderatable_here(interaction, tgt)
        if _no:
            await interaction.edit_original_response(content=_no)
            return

        # As a delta through `add_balance`, the way the owner console's balance-set
        # does it — never a direct write. The ledger is written by the five
        # functions that move a balance, and a wallet assigned around them stops
        # adding up to its own history, which is the one thing the Finance tab
        # promises. Not garnishable: a correction is not earnings.
        current = store.get_user(gid, tgt.account_id).get("balance", 0)
        await store.add_balance(gid, tgt.account_id, amount - current,
                                category=store.TX_ADMIN,
                                detail="Balance set by a moderator",
                                counterparty=str(interaction.user.id))

        await interaction.followup.send(
            tp(gid, uid, "eco.setbal.done", name=tgt.label, amount=f"{amount:,}", currency=settings.CURRENCY_NAME),
            ephemeral=True,
        )
        log.info("%s set %s (%s) balance to %d",
                 interaction.user, tgt.label, tgt.account_id, amount)

    # ── Error handler ─────────────────────────────────────────────────────────
    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        gid = interaction.guild_id
        uid = interaction.user.id
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message(tp(gid, uid, "common.no_perm"), ephemeral=True)
        else:
            log.error("Economy cog error: %s", error, exc_info=True)
            if not interaction.response.is_done():
                await interaction.response.send_message(tp(gid, uid, "common.error"), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Economy(bot))
