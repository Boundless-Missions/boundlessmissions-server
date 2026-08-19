"""
cogs/costwatch.py – Keeps the cost guard fed, and tells the owner before the wall.

Two jobs, on one loop:

  • POLL. Every `settings.COST_METRICS_POLL_INTERVAL` seconds, fetch Google's own
    usage figures from Cloud Monitoring and hand them to `cost_guard.ingest_usage`
    as the authoritative baseline. This is what closes the estimator's blind
    spots — signed-URL egress, bytes at rest, and any usage that isn't this
    process (scripts, the console, a second instance on the same project).
    Far more slowly (`COST_BILLING_POLL_INTERVAL`, six hours) it also reads the
    BigQuery billing export for the actual invoice — display only, and quiet,
    since that source is hours behind and cannot be allowed near the brake.

  • ANNOUNCE. Every `_TICK` seconds, drain whatever threshold crossings the guard
    has queued and DM them to the owner. Previously the only sign a budget had
    blown was a log line, which meant finding out from users. Alerts are drained
    on a much shorter cycle than the poll, because a level can change instantly
    from the local meter and hearing about a freeze five minutes late is no good.

WHY THE GUARD QUEUES AND THIS COG DISPATCHES
`cost_guard` is imported by data/store.py and runs on whatever thread happens to
be doing a Firestore call — inside firebase-admin's synchronous internals, off
the event loop. That is no place to touch Discord. So the guard appends to a
list under its lock and this cog, which does live on the loop, drains it.
"""

import logging
import time

import discord
from discord.ext import commands, tasks

import settings
from config import cfg
from cost_guard import guard
from data import gcp_billing, gcp_metrics

log = logging.getLogger(__name__)

# How often alerts are drained. Deliberately much shorter than the metrics poll:
# the ladder can move the instant the local meter sees a runaway, and that is
# precisely the case where a prompt DM is worth something.
_TICK = 30

_LEVEL_STYLE = {
    "warning": (discord.Color.gold(), "⚠️", "Half the monthly budget is gone"),
    "degraded": (discord.Color.orange(), "🟠", "Budget nearly spent — uploads paused"),
    "frozen": (discord.Color.red(), "🛑", "Budget spent — Firebase is paused"),
}

_LEVEL_DETAIL = {
    "warning": (
        "Nothing has changed for users yet. This is the early notice so a bad "
        "month is visible while there is still time to do something about it."
    ),
    "degraded": (
        "New file uploads (craft listings, screenshots, contract attachments) are "
        "being refused. Everything else — reads, downloads, XP, contracts — still "
        "works normally."
    ),
    "frozen": (
        "Every Firestore and Storage operation is now refused, so the bot cannot "
        "persist anything until the budget resets on the 1st (UTC). Buffered "
        "writes were flushed before the freeze. Raise "
        "`FIREBASE_MONTHLY_BUDGET_USD` and restart, or flip the guard off from "
        "the admin console, to bring it back early."
    ),
}


class CostWatch(commands.Cog, name="CostWatch"):
    """Polls Cloud Monitoring and reports budget thresholds to the owner."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._last_poll = 0.0
        self._last_billing_poll = 0.0
        # A metrics failure is announced once, not every 30 seconds. Cleared when
        # metrics start working again so a later failure is heard about.
        self._metrics_error_announced: str | None = None

    async def cog_load(self) -> None:
        self.watch.start()

    async def cog_unload(self) -> None:
        self.watch.cancel()

    # ── the loop ─────────────────────────────────────────────────────────────
    @tasks.loop(seconds=_TICK)
    async def watch(self) -> None:
        if time.time() - self._last_poll >= settings.COST_METRICS_POLL_INTERVAL:
            self._last_poll = time.time()
            await self._poll_metrics()
        # Much slower: the billing export itself only refreshes a few times a
        # day, so polling it faster buys nothing but spends the per-query floor.
        if time.time() - self._last_billing_poll >= settings.COST_BILLING_POLL_INTERVAL:
            self._last_billing_poll = time.time()
            await self._poll_billing()
        await self._dispatch_alerts()

    @watch.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    # ── tier 1 ───────────────────────────────────────────────────────────────
    async def _poll_metrics(self) -> None:
        snap = await gcp_metrics.fetch_usage()
        if snap.ok:
            guard.ingest_usage(snap)
            if self._metrics_error_announced:
                self._metrics_error_announced = None
                log.info("cost_guard: Cloud Monitoring is answering again.")
            return

        guard.note_metrics_error(snap.error)
        # Only worth telling the owner about once per distinct cause, and only
        # when polling is supposed to be on — an intentionally disabled poller
        # is not a fault.
        if settings.COST_METRICS_ENABLED and snap.error != self._metrics_error_announced:
            self._metrics_error_announced = snap.error
            log.warning("cost_guard: no authoritative usage — %s", snap.error)
            await self._dm_owner(discord.Embed(
                title="📉 Cost tracking is running on estimates only",
                color=discord.Color.greyple(),
                description=(
                    f"Cloud Monitoring could not be read:\n```{snap.error}```\n"
                    "The spending brake still works — it is running on the "
                    "in-process estimate alone, which cannot see direct-download "
                    "egress or bytes at rest, so the figures will read low.\n\n"
                    "If this is the IAM grant, give the service account "
                    "`roles/monitoring.viewer` on the project and it will pick "
                    "itself up from the admin console's retry button."
                ),
            ))

    # ── tier 2 ───────────────────────────────────────────────────────────────
    async def _poll_billing(self) -> None:
        """Read the invoice. Never alerts, and never touches the brake.

        Deliberately quiet compared to `_poll_metrics`: this is the display
        figure, and the two states it can be in that aren't success — export not
        yet loaded, IAM not yet granted — are both normal for days after setup
        and would be pure noise as DMs. They are visible in the Costs tab, which
        is where someone goes when they want to know.
        """
        snap = await gcp_billing.fetch_billing()
        if snap.ok:
            guard.ingest_billing(snap)
            log.info("cost_guard: billed %.4f %s month-to-date (%s)",
                     snap.total_usd, snap.currency, snap.invoice_month)
        else:
            guard.note_billing_error(snap.error)
            log.debug("cost_guard: no billing data — %s", snap.error)

    # ── alerts ───────────────────────────────────────────────────────────────
    async def _dispatch_alerts(self) -> None:
        for alert in guard.drain_alerts():
            if alert.get("level") == "frozen":
                await self._flush_before_the_freeze_bites()
            try:
                await self._dm_owner(self._render(alert))
            except Exception as exc:  # pragma: no cover - never break the loop
                log.error("cost_guard: could not deliver alert: %s", exc)

    async def _flush_before_the_freeze_bites(self) -> None:
        """Push store's memory buffer to Firestore while the grace pass is armed.

        The guard permits exactly one write pass after freezing, but nothing
        claims it until something tries to save — and the normal auto-save runs
        only every AUTO_SAVE_INTERVAL (300s). Waiting that long risks the process
        being restarted first, which is precisely how a freeze turns into lost
        XP and balances. This loop notices within `_TICK`, so it does it here.
        """
        try:
            from data.store import store
            await store.save_if_dirty()
        except Exception as exc:
            log.error("cost_guard: final flush after freeze failed: %s", exc)

    def _render(self, alert: dict) -> discord.Embed:
        if alert.get("kind") == "gemini":
            return discord.Embed(
                title="🤖 Gemini budget spent",
                color=discord.Color.gold(),
                description=(
                    f"**${alert['usd']:.4f}** of **${alert['budget']:.2f}** used in "
                    f"{alert['month']}.\n\nAI analysis has fallen back to the keyword "
                    "heuristics for the rest of the month. Nothing is broken — "
                    "screenshot analysis is off and classification is less accurate."
                ),
            )

        level = alert.get("level", "warning")
        color, icon, headline = _LEVEL_STYLE.get(
            level, (discord.Color.greyple(), "•", "Budget threshold crossed"))
        budget = alert.get("budget", 0.0)
        pct = (alert["usd"] / budget * 100) if budget else 0.0
        embed = discord.Embed(
            title=f"{icon} {headline}",
            color=color,
            description=(
                f"**${alert['usd']:.4f}** of **${budget:.2f}** "
                f"({pct:.0f}%) estimated for {alert['month']}.\n\n"
                f"{_LEVEL_DETAIL.get(level, '')}"
            ),
        )
        embed.set_footer(text=f"Level: {alert.get('previous', '?')} → {level}")
        return embed

    async def _dm_owner(self, embed: discord.Embed) -> None:
        """Deliver to the configured owner. Falls back to the application owner
        so a missing BOT_OWNER_ID cannot make a budget alert vanish."""
        user = None
        if cfg.OWNER_ID:
            user = self.bot.get_user(cfg.OWNER_ID)
            if user is None:
                try:
                    user = await self.bot.fetch_user(cfg.OWNER_ID)
                except discord.HTTPException:
                    user = None
        if user is None:
            try:
                info = await self.bot.application_info()
                user = info.owner
            except Exception:
                user = None
        if user is None:
            log.error("cost_guard: no owner to alert — %s", embed.title)
            return
        await user.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CostWatch(bot))
