"""
cogs/tickets.py – Private support / report tickets.

A persistent "📩 Open a Ticket" button lives in TICKET_PANEL_CHANNEL_ID. Pressing
it shows a reason dropdown (report a user / report a bug / other), then a short
modal. On submit, a private channel is created under TICKET_CATEGORY_ID that only
the filer and the mods (MOD_ROLE_ID) can see — no outside access.

Other flows reuse `create_ticket()` to open tickets programmatically:
  • KSP account-sharing reports  (cogs/ksp_bridge.py)
  • Contract "sue" escalations    (cogs/contract_views.py)
  • In-game bug reports           (api_server.py, /api/v1/bugreport) — these ping
    the `bug_report` role instead of the mods, see `notify_role_key` below.

Each ticket channel carries a "🔒 Close" button (mods or the opener may close).
The opener's id is stored in the channel topic so the close check survives a
bot restart.
"""

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import View, Button, DynamicItem, Select, Modal, TextInput

import settings
from config import cfg
from cogs import perms
from data.store import _db
from data import tickets as tdb
from data import guild_config

try:
    from firebase_admin import firestore as _fs
except Exception:  # pragma: no cover - firestore always present in prod
    _fs = None

log = logging.getLogger(__name__)

# ── Ticket kinds ──────────────────────────────────────────────────────────────
# key → (emoji, human label, modal title, [(field_label, placeholder, long?)])
# Discord's hard limit is 50 channels per category; stop a little short so a
# moderator has room to work while clearing the backlog.
TICKET_CATEGORY_SOFT_MAX = 45
# Bulk, non-urgent openings (announcements) stop well short, so a large role cannot
# consume the room moderation intake needs.
TICKET_CATEGORY_BULK_MAX = 30

# Ticket openings allowed per guild per hour, across every door. In-process, like
# every other limit here; see api_server._limit_ticket_open for the per-user and
# per-address halves, which stay at the endpoints because they need the caller.
TICKET_GUILD_PER_HOUR = 20
_GUILD_OPENINGS: dict[int, list[float]] = {}


# The category filling up is a TERMINAL state: nothing closes a ticket but the
# button on it, so once the ceiling is reached every intake door in the guild —
# moderation reports, contract reports, bug reports, the device-sharing report and
# the anti-cheat flag — answers "the ticket system isn't set up" until a human
# presses Close. That was only ever written to the log. Alerted once an hour per
# guild rather than once ever: the condition persists, so a single alert that gets
# missed leaves the guild down, while one per refusal would itself be the flood.
_CAPACITY_ALERT_INTERVAL = 3600.0
_CAPACITY_ALERTED: dict[int, float] = {}

# The Discord modal's own per-user allowance.
#
# `api_server._limit_ticket_open` is the budget every HTTP door shares — 3 an hour
# per user, plus the per-address bucket, plus the per-guild breaker — and its
# docstring states the invariant: the breaker "has to cover *every* door into that
# category, or the alts simply use the one without it." The public "Open a Ticket"
# button was that door. It reached `create_ticket` directly, so it skipped the
# per-user allowance entirely while still *spending* the guild-wide breaker: one
# member pressing it TICKET_GUILD_PER_HOUR times refused every other intake path in
# the server — moderation reports, contract reports, bug reports and the anti-cheat
# flag — for the rest of the hour.
#
# Kept here rather than reusing `_limit_ticket_open`: that one wants a `Request` for
# its per-address bucket, and an interaction has no address. Same allowance as the
# HTTP doors so neither is the cheap way in.
TICKET_MODAL_PER_USER_PER_HOUR = 3
_MODAL_OPENINGS: dict[int, list[float]] = {}


def _allow_modal_opening(user_id: int) -> bool:
    """Record a modal-opened ticket against this user's hourly allowance."""
    import time as _time
    now = _time.time()
    hits = [t for t in _MODAL_OPENINGS.get(user_id, []) if now - t < 3600.0]
    if len(hits) >= TICKET_MODAL_PER_USER_PER_HOUR:
        _MODAL_OPENINGS[user_id] = hits
        return False
    hits.append(now)
    _MODAL_OPENINGS[user_id] = hits
    return True


async def _alert_category_full(client, guild, category, kind: str, *,
                               reason: str = "category_full") -> None:
    """Tell somebody that ticket intake is refusing. Best effort, never raises —
    this runs on the path that is already failing.

    Two reasons reach here, and they need different words because they need
    different actions. `category_full` is terminal: nothing closes a ticket but the
    button on it, so intake stays down until a human presses Close. `budget_spent`
    is the hourly per-guild breaker, which clears by itself — but while it is spent
    every intake door is refused just the same, and it is the branch somebody
    *driving* the breaker actually hits, so it cannot stay log-only."""
    import time as _time
    now = _time.time()
    last = _CAPACITY_ALERTED.get(guild.id, 0.0)
    if now - last < _CAPACITY_ALERT_INTERVAL:
        return
    _CAPACITY_ALERTED[guild.id] = now

    if reason == "budget_spent":
        title = "⚠️ Ticket intake is rate limited"
        body = (f"**{guild.name}** has opened {TICKET_GUILD_PER_HOUR} tickets in the "
                f"last hour, which is the per-guild ceiling. Every ticket path in "
                f"that server is refused until the hour rolls, reports, bug reports "
                f"and the anti-cheat flag included; a `{kind}` ticket was just turned "
                f"away.\n\nThis clears by itself. If it keeps happening, somebody is "
                f"probably driving it: check who has been opening tickets.")
    else:
        title = "⚠️ Ticket system is full"
        body = (f"The ticket category **{getattr(category, 'name', '?')}** in "
                f"**{guild.name}** is full ({len(getattr(category, 'channels', ()))} "
                f"channels). Every ticket path in that server is now refused: reports, "
                f"bug reports and the anti-cheat flag included; a `{kind}` ticket was "
                f"just turned away.\n\nClose some tickets to bring intake back.")

    # The moderators of the affected guild are the ones who can fix it, so try
    # them first; the owner is told either way, since a guild with no mod channel
    # mapped would otherwise still fail silently.
    try:
        chan = guild_config.resolve_channel(client, guild.id, "contract_mod")
        mod_role = guild_config.resolve_role(guild, "mod")
        if chan is not None:
            await chan.send(
                content=(mod_role.mention if mod_role else None),
                embed=discord.Embed(
                    title=title,
                    description=body,
                    color=discord.Color.red()),
                allowed_mentions=discord.AllowedMentions(roles=True))
    except Exception as exc:
        log.warning("Could not post the ticket-capacity alert in guild %s: %s",
                    guild.id, exc)

    try:
        from api_server import _tell_owner
        await _tell_owner(title, body)
    except Exception as exc:
        log.warning("Could not DM the owner about the full ticket category: %s", exc)


def _allow_guild_opening(guild_id: int) -> bool:
    """Record an opening against this guild's hourly budget; False when spent."""
    import time as _time
    now = _time.time()
    hits = [t for t in _GUILD_OPENINGS.get(guild_id, []) if now - t < 3600.0]
    if len(hits) >= TICKET_GUILD_PER_HOUR:
        _GUILD_OPENINGS[guild_id] = hits
        return False
    hits.append(now)
    _GUILD_OPENINGS[guild_id] = hits
    return True

TICKET_KINDS = {
    "user": (
        "🚨", "Report a user", "🚨 Report a User",
        [
            ("Who are you reporting? (name / ID)", "e.g. SomeUser or 123456789012345678", False),
            ("What happened?", "Describe the issue, with any context…", True),
        ],
    ),
    "bug": (
        "🐛", "Report a bug / issue", "🐛 Report an Issue",
        [
            ("Short summary", "e.g. /g balance shows the wrong amount", False),
            ("Details / steps to reproduce", "What you did, what happened, what you expected…", True),
        ],
    ),
    "other": (
        "💬", "Something else", "💬 Open a Ticket",
        [
            ("Subject", "Short title for your ticket", False),
            ("Details", "Tell us what you need…", True),
        ],
    ),
}


# ── Ticket numbering ──────────────────────────────────────────────────────────

def _next_ticket_number(gid: int) -> int:
    """Atomically increment and return a per-guild ticket counter (Firestore txn)."""
    doc_ref = (_db.collection("guilds").document(str(gid))
               .collection("meta").document("tickets"))
    if _fs is None:
        # Fallback: best-effort read+write (single-process bot, rarely races).
        snap = doc_ref.get()
        cur = int((snap.to_dict() or {}).get("seq", 0)) if snap.exists else 0
        nxt = cur + 1
        doc_ref.set({"seq": nxt}, merge=True)
        return nxt

    txn = _db.transaction()

    @_fs.transactional
    def _run(transaction):
        snap = doc_ref.get(transaction=transaction)
        cur = int((snap.to_dict() or {}).get("seq", 0)) if snap.exists else 0
        nxt = cur + 1
        transaction.set(doc_ref, {"seq": nxt}, merge=True)
        return nxt

    return _run(txn)


def _ticket_opener_id(channel: discord.abc.GuildChannel) -> int | None:
    """Read the opener's user id back out of a ticket channel's topic."""
    topic = getattr(channel, "topic", None) or ""
    for part in topic.split("|"):
        if part.startswith("opener="):
            try:
                return int(part.split("=", 1)[1])
            except ValueError:
                return None
    return None


def _ticket_kind(channel: discord.abc.GuildChannel) -> str:
    """Read the ticket's kind back out of its channel topic (empty if unknown)."""
    topic = getattr(channel, "topic", None) or ""
    for part in topic.split("|"):
        if part.startswith("kind="):
            return part.split("=", 1)[1]
    return ""


# ── Programmatic ticket creation (shared by all flows) ────────────────────────

async def create_ticket(
    client: discord.Client,
    guild: discord.Guild,
    *,
    opener_id: int | None,
    kind: str,
    title: str,
    description: str = "",
    color: discord.Color | None = None,
    subject_user_id: int | None = None,
    extra_user_ids: list[int] | None = None,
    extra_embeds: list[discord.Embed] | None = None,
    extra_view: View | None = None,
    files: list[discord.File] | None = None,
    ping_mods: bool = True,
    reserve_capacity: bool = True,
    notify_role_key: str | None = None,
) -> discord.TextChannel | None:
    """Create a private ticket channel under TICKET_CATEGORY_ID and post the opening
    message. Visible only to @mods, the opener (if any), and any extra_user_ids.

    `opener_id=None` makes a **mods-only** ticket (used for auto-flagged anti-cheat
    reports where the suspect must NOT see it); only mods can then close it.
    `subject_user_id` is shown for context but is NOT granted access.

    `notify_role_key` names a *second* guild_config role (see its role registry) that
    is granted access and pinged — for a ticket whose audience isn't moderation, like
    an in-game bug report going to whoever maintains the mod. Such callers normally
    pass `ping_mods=False` too; if the named role isn't configured in this guild the
    mods are pinged after all, since an unread report is worse than a misrouted one.

    Returns the channel, or None if the ticket system is unconfigured / creation
    failed."""
    cat_id = guild_config.get_channel_id(guild.id, "ticket_category")
    if not cat_id:
        log.warning("create_ticket called but no ticket_category is configured for guild %s", guild.id)
        return None

    category = guild.get_channel(cat_id)
    if not isinstance(category, discord.CategoryChannel):
        try:
            category = await guild.fetch_channel(cat_id)
        except Exception as exc:
            log.warning("Ticket category %s not found: %s", cat_id, exc)
            category = None

    overwrites: dict = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True,
            manage_channels=True, manage_messages=True,
            embed_links=True, attach_files=True,
        ),
    }
    mod_role = guild_config.resolve_role(guild, "mod")
    notify_role = guild_config.resolve_role(guild, notify_role_key) if notify_role_key else None
    for role in (mod_role, notify_role):
        if role is not None:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, attach_files=True)

    # An opener id is an ACCOUNT id, and a website account has no Discord user to
    # grant channel access to. That is not a failure: the channel is the mods'
    # view of the ticket, and the opener's view is the thread on the website. So
    # a non-snowflake opener simply contributes no overwrite.
    def _snowflake(value):
        s = str(value or "")
        return int(s) if s.isdigit() else None

    member_ids = [v for v in
                  ([_snowflake(opener_id)] + [_snowflake(u) for u in (extra_user_ids or [])])
                  if v is not None]
    for uid in dict.fromkeys(member_ids):  # de-dupe, preserve order
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except Exception:
                member = None
        if member:
            overwrites[member] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, attach_files=True)

    # Discord allows 50 channels per CATEGORY (not 500 per guild), and every ticket
    # lands in this one — so the category, not the guild, is the ceiling that gets
    # hit. Past it `create_text_channel` simply 400s and every ticket path dies at
    # once: moderation reports, bug reports, the anti-cheat flag. Refusing a few
    # short of full turns "everything is silently broken" into a message a moderator
    # can act on, and leaves headroom to open the ones that matter while they clear
    # the backlog. The rate limits in api_server._limit_ticket_open are what stop
    # the category filling in the first place; this is the backstop.
    # `reserve_capacity=False` is for the bulk announcement path, which opens one
    # ticket per role member. It is not moderation intake, and letting it run to the
    # cap would leave the category full — at which point reports, bug reports and the
    # anti-cheat flag all fail until someone clears the backlog by hand. So it stops
    # earlier and leaves the last slots for the tickets that need a human.
    # The per-guild opening budget lives HERE, not at the endpoints, because it is a
    # property of the category rather than of any one door. It was applied at four
    # API endpoints and missed the two easiest — this modal (the primary, most-used
    # door: any guild member, one button and a form) and the sue escalation — so the
    # attack it was added against was still open through the front entrance. A
    # breaker one caller can be added without is not a breaker.
    #
    # Bulk openings are exempt: they have their own, lower ceiling below, and an
    # announcement is one authorised action rather than N unsolicited ones.
    if reserve_capacity and not _allow_guild_opening(guild.id):
        log.warning("Ticket budget for guild %s is spent; refusing a %s ticket.",
                    guild.id, kind)
        # Alerted, not just logged. This is the branch an attacker drives — spending
        # the hourly budget is far cheaper than filling the category — and it refuses
        # the anti-cheat flag along with everything else, so a log line nobody reads
        # was exactly the wrong amount of noise.
        await _alert_category_full(client, guild, category, kind, reason="budget_spent")
        return None

    limit = TICKET_CATEGORY_SOFT_MAX if reserve_capacity else TICKET_CATEGORY_BULK_MAX
    if len(getattr(category, "channels", ())) >= limit:
        log.error("Ticket category %r in guild %s is full (%d channels), refusing to "
                  "open a %s ticket. Close some tickets to restore the ticket system.",
                  getattr(category, "name", "?"), guild.id,
                  len(category.channels), kind)
        # Only moderation intake alerts. The bulk announcement path stops 15 short
        # of the real ceiling on purpose, so hitting *its* limit is the design
        # working, not the ticket system going down.
        if reserve_capacity:
            await _alert_category_full(client, guild, category, kind)
        return None

    num = await asyncio.to_thread(_next_ticket_number, guild.id)
    chan_name = f"ticket-{num:04d}"
    try:
        channel = await guild.create_text_channel(
            name=chan_name,
            category=category,
            overwrites=overwrites,
            topic=f"GKTicket|opener={opener_id or ''}|kind={kind}",
            reason=f"Ticket #{num:04d} ({kind}) opened",
        )
    except Exception as exc:
        log.error("Could not create ticket channel for %s: %s", opener_id, exc)
        return None

    emoji = TICKET_KINDS.get(kind, ("🎫",))[0]
    e = discord.Embed(
        title=f"{emoji} {title}",
        description=description or None,
        color=color or discord.Color.blurple(),
    )
    e.set_footer(text=f"Ticket #{num:04d}")

    # Who opened it. A Discord member answers this on their own; a website account
    # does not, and a ticket whose opener is a blank space is one a moderator
    # cannot act on — they cannot look the person up, check their history or
    # decide whether the report is credible. So the account record fills in for
    # the member object: its display name and username in the author line, its
    # uploaded avatar as the icon, and the ids spelled out in a field underneath
    # (a website account has no mention, and `<@a_…>` is broken text, not a link).
    opener_did = _snowflake(opener_id)
    opener = guild.get_member(opener_did) if opener_did else None
    acct = None
    if opener_id:
        try:
            from data import accounts as _accounts
            acct = await asyncio.to_thread(_accounts.get_account, opener_id)
        except Exception as exc:
            log.warning("Ticket: could not read account %s: %s", opener_id, exc)

    author_name = ""
    author_icon = None
    if acct:
        handle = str(acct.get("username") or "")
        shown = str(acct.get("display_name") or "") or handle or str(opener_id)
        author_name = f"{shown} (@{handle})" if handle else shown
        stored = acct.get("avatar_url") or ""
        if stored:
            try:
                from data.store import sign_stored, SIGNED_URL_MAX_TTL
                author_icon = sign_stored(stored, ttl=SIGNED_URL_MAX_TTL)
            except Exception:
                author_icon = None
    if opener:
        author_name = author_name or str(opener)
        author_icon = author_icon or getattr(opener.display_avatar, "url", None)
    if author_name:
        e.set_author(name=author_name[:256], icon_url=author_icon)

    if opener_id:
        who = f"<@{opener_did}>" if opener_did else "no Discord account"
        line = f"{who}\n`{opener_id}`"
        if acct and acct.get("username"):
            line += f"\nUsername: `{acct['username']}`"
        e.add_field(name="Opened by", value=line, inline=False)
    if subject_user_id:
        subj_did = _snowflake(subject_user_id)
        subj = guild.get_member(subj_did) if subj_did else None
        e.add_field(name="Reported user",
                    value=(f"{subj.mention} (`{subject_user_id}`)" if subj else f"`{subject_user_id}`"),
                    inline=False)

    ping_roles: list[discord.Role] = []
    if ping_mods and mod_role:
        ping_roles.append(mod_role)
    if notify_role_key:
        if notify_role is not None:
            ping_roles.append(notify_role)
        elif mod_role is not None:
            log.warning("Ticket role '%s' is not configured in guild %s, pinging the "
                        "mod role instead", notify_role_key, guild.id)
            ping_roles.append(mod_role)

    content_bits = []
    if opener:
        content_bits.append(opener.mention)
    content_bits += [r.mention for r in dict.fromkeys(ping_roles)]
    content = " ".join(content_bits) or None

    try:
        await channel.send(content=content, embed=e, view=TicketControlView(),
                           allowed_mentions=discord.AllowedMentions(roles=True, users=True))
        embeds = list(extra_embeds or [])
        # Attach the action view (e.g. a contract ModReviewView) to the final
        # embed so the buttons sit with their context; otherwise post it alone.
        if embeds:
            for emb in embeds[:-1]:
                await channel.send(embed=emb)
            await channel.send(embed=embeds[-1], view=extra_view,
                               files=files or [])
        elif extra_view is not None or files:
            await channel.send(view=extra_view, files=files or [])
    except Exception as exc:
        log.warning("Ticket %s created but opening post failed: %s", chan_name, exc)

    # Record the ticket. This is what makes it readable anywhere but here: the
    # channel above is the mods' view, and for a player with no Discord it is the
    # ONLY view unless the record exists. Written after the channel so it can
    # carry the channel id, and best-effort — a ticket a moderator can see and
    # answer is worth more than one that failed to be filed.
    ticket = None
    try:
        ticket = await asyncio.to_thread(
            tdb.create,
            guild_id=guild.id, opener_id=opener_id, kind=kind, title=title,
            description=description, number=num, channel_id=channel.id,
            subject_user_id=subject_user_id)
        if ticket:
            # Keep the id on the channel topic too. The topic is what survives a
            # Firestore outage, and it is how `get_by_channel` can be repaired by
            # hand if a record is ever lost.
            try:
                await channel.edit(
                    topic=f"GKTicket|opener={opener_id or ''}|kind={kind}"
                          f"|id={ticket['ticket_id']}")
            except Exception:
                pass
    except Exception as exc:
        log.warning("Ticket %s created but not recorded: %s", chan_name, exc)

    log.info("Opened ticket %s (kind=%s) for user %s in guild %s",
             chan_name, kind, opener_id, guild.id)
    return channel


# ── "Open a Ticket" panel button ──────────────────────────────────────────────

class OpenTicketButton(DynamicItem[Button], template=r"gk_ticket_open"):
    def __init__(self):
        super().__init__(Button(
            label="📩 Open a Ticket", style=discord.ButtonStyle.blurple,
            custom_id="gk_ticket_open"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "Tickets can only be opened inside the server.", ephemeral=True)
            return
        await interaction.response.send_message(
            "What is your ticket about?",
            view=_ReasonView(), ephemeral=True)


class _ReasonView(View):
    """Ephemeral reason dropdown shown after the panel button is pressed."""
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(_ReasonSelect())


class _ReasonSelect(Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=label, value=key, emoji=emoji)
            for key, (emoji, label, *_rest) in TICKET_KINDS.items()
        ]
        super().__init__(placeholder="Choose a reason…", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        kind = self.values[0]
        await interaction.response.send_modal(TicketModal(kind))


class TicketModal(Modal):
    """Collects the ticket subject + details, then opens the private channel."""
    def __init__(self, kind: str):
        self.kind = kind
        emoji, label, modal_title, fields = TICKET_KINDS.get(kind, TICKET_KINDS["other"])
        super().__init__(title=modal_title)
        self._inputs: list[TextInput] = []
        for flabel, placeholder, long in fields:
            ti = TextInput(
                label=flabel[:45],
                placeholder=placeholder[:100],
                style=discord.TextStyle.paragraph if long else discord.TextStyle.short,
                required=True,
                max_length=1000 if long else 200,
            )
            self.add_item(ti)
            self._inputs.append(ti)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        emoji, label, modal_title, fields = TICKET_KINDS.get(self.kind, TICKET_KINDS["other"])
        subject = self._inputs[0].value.strip()
        details = self._inputs[1].value.strip() if len(self._inputs) > 1 else ""

        # Escaped at the display layer, not on the way in: an embed description
        # renders full Discord markdown, masked links included, and this one is read
        # in a private channel by moderators — the audience a
        # `[boundlessmissions.com/admin](https://evil.tld)` is aimed at. Only the
        # rendering is changed; nothing here stores the raw text.
        first_label = fields[0][0]
        esc = discord.utils.escape_markdown
        desc = f"**{first_label}**\n{esc(subject)}\n\n"
        if len(fields) > 1:
            desc += f"**{fields[1][0]}**\n{esc(details)}"

        # Charged before the channel is opened, because opening one spends the
        # guild-wide breaker every other intake door depends on.
        if not _allow_modal_opening(interaction.user.id):
            await interaction.followup.send(
                f"⏳ You've opened {TICKET_MODAL_PER_USER_PER_HOUR} tickets in the last "
                "hour, which is the limit. Add anything else to a ticket you already "
                "have open, or wait a little and try again.", ephemeral=True)
            return

        channel = await create_ticket(
            interaction.client, interaction.guild,
            opener_id=interaction.user.id,
            kind=self.kind,
            title=label,
            description=desc,
            color=discord.Color.orange() if self.kind == "user" else discord.Color.blurple(),
        )
        if channel is None:
            await interaction.followup.send(
                "⚠️ Couldn't open a ticket right now; the ticket system may be "
                "misconfigured. Please ping a moderator directly.", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ Your ticket has been opened: {channel.mention}\n"
            "Only you and the moderators can see it.", ephemeral=True)


# ── Ticket close control ──────────────────────────────────────────────────────

class CloseTicketButton(DynamicItem[Button], template=r"gk_ticket_close"):
    def __init__(self):
        super().__init__(Button(
            label="🔒 Close ticket", style=discord.ButtonStyle.red,
            custom_id="gk_ticket_close"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        from cogs.gkchannels import is_mod
        from cogs.perms import real_user
        channel = interaction.channel
        opener_id = _ticket_opener_id(channel)
        member = interaction.user
        # Mod power gates on the REAL invoker (mimic-safe); the opener check stays on
        # the acting-as identity, which mimic legitimately changes.
        ru = real_user(interaction)
        # A bug ticket belongs to the bug-report role, not to moderation — they are
        # the ones who resolve it, so they can close it too. Same mimic-safety rule
        # as the mod check: it gates on the REAL invoker.
        bug_role = guild_config.resolve_role(interaction.guild, "bug_report")
        owns_bug_ticket = (
            _ticket_kind(channel) == "bug" and bug_role is not None
            and isinstance(ru, discord.Member) and bug_role in ru.roles
        )
        allowed = (isinstance(ru, discord.Member) and is_mod(ru)) \
            or owns_bug_ticket \
            or (opener_id is not None and member.id == opener_id)
        if not allowed:
            await interaction.response.send_message(
                "Only a moderator or the person who opened this ticket can close it.",
                ephemeral=True)
            return
        await interaction.response.send_message(
            embed=discord.Embed(
                description=f"🔒 Ticket closed by {member.mention}. Deleting in 5 seconds…",
                color=discord.Color.greyple()))
        # Close the RECORD before the channel is deleted. Deleting the channel is
        # what used to end a ticket, and once it is gone there is nothing left to
        # look the record up by — so this has to happen first, and the closing
        # note goes into the thread so the opener can read on the website why it
        # ended rather than watching it silently vanish.
        ticket = await asyncio.to_thread(tdb.get_by_channel, channel.id)
        if ticket:
            tid = ticket["ticket_id"]
            await asyncio.to_thread(
                tdb.add_message, tid,
                author_id=str(member.id), author_name=member.display_name,
                author_kind=tdb.AUTHOR_SYSTEM,
                body=f"Ticket closed by {member.display_name}.")
            await asyncio.to_thread(tdb.close, tid, str(member.id))

        await asyncio.sleep(5)
        try:
            await channel.delete(reason=f"Ticket closed by {member}")
        except Exception as exc:
            log.warning("Could not delete ticket channel %s: %s", channel.id, exc)


class TicketControlView(View):
    """Attached to a ticket's opening message: lets mods/opener close it."""
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(CloseTicketButton())


class TicketPanelView(View):
    """The persistent panel posted in TICKET_PANEL_CHANNEL_ID."""
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(OpenTicketButton())


def _panel_embed() -> discord.Embed:
    return discord.Embed(
        title="📩 Need help or want to report something?",
        description=(
            "Open a **private ticket** that only you and the moderators can see.\n\n"
            "🚨 **Report a user**: rule-breaking, account sharing, harassment…\n"
            "🐛 **Report a bug / issue**: something in the bot or KSP mod is broken.\n"
            "💬 **Something else**: questions, requests, anything.\n\n"
            "Press the button below and pick a reason."
        ),
        color=discord.Color.blurple(),
    )


async def _find_existing_panel(channel: discord.TextChannel, bot_user_id: int):
    """Return the bot's existing panel message in the channel (by its Open-Ticket
    button custom_id), or None — so we never post a duplicate panel on restart."""
    try:
        async for msg in channel.history(limit=50):
            if msg.author.id != bot_user_id:
                continue
            for row in msg.components:
                for comp in getattr(row, "children", []):
                    if getattr(comp, "custom_id", None) == "gk_ticket_open":
                        return msg
    except Exception as exc:
        log.warning("Could not scan ticket panel channel %s: %s", channel.id, exc)
    return None


# ── Cog ───────────────────────────────────────────────────────────────────────

class Tickets(commands.Cog, name="Tickets"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._panel_ensured = False

    def _resolve_panel_channel(self, guild: discord.Guild):
        """The ticket-panel channel configured for this specific guild (or None)."""
        if guild is None:
            return None
        return guild_config.resolve_channel(self.bot, guild.id, "ticket_panel")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Mirror a staff reply in a ticket channel into the ticket's thread.

        This is the half that makes a ticket two-way: without it a moderator's
        answer exists only in Discord, which is exactly where the person who
        needs it may not be.

        Four things are deliberately skipped. **Our own messages**, because the
        website's replies are posted into the channel BY the bot and would
        otherwise come straight back as duplicates — belt and braces with the
        `has_discord_message` check below, which also covers a restart mid-flight.
        **Anything outside a ticket channel**, resolved by one keyed lookup rather
        than a scan. **Empty messages** (a bare attachment still counts, so the
        test is on content *and* attachments). And **the opening post**, which the
        record already carries as its description.
        """
        if message.author.bot or message.guild is None:
            return
        if not (message.content or message.attachments):
            return
        # Cheap reject first: ticket channels are named ticket-NNNN, so anything
        # else costs nothing. The Firestore lookup only happens for a real one.
        if not str(getattr(message.channel, "name", "")).startswith("ticket-"):
            return

        try:
            ticket = await asyncio.to_thread(tdb.get_by_channel, message.channel.id)
            if not ticket or ticket.get("status") != tdb.OPEN:
                return
            tid = ticket["ticket_id"]
            if await asyncio.to_thread(tdb.has_discord_message, tid, message.id):
                return

            # Who is talking. The opener's own messages are theirs; everyone else
            # in a private ticket channel is there because they are staff.
            opener = str(ticket.get("opener_id") or "")
            kind = tdb.AUTHOR_OPENER if str(message.author.id) == opener else tdb.AUTHOR_STAFF

            atts = [{"name": a.filename, "url": a.url} for a in message.attachments][:10]
            await asyncio.to_thread(
                tdb.add_message, tid,
                author_id=str(message.author.id),
                author_name=message.author.display_name,
                author_kind=kind,
                body=message.content or "",
                discord_message_id=str(message.id),
                attachments=atts)

            # Tell the opener, in the feed they already have. The unread dot on
            # the account page only helps someone already looking at it; this
            # reaches a player who is in the game, which for a website-only
            # account is the only place they would otherwise find out at all.
            if kind == tdb.AUTHOR_STAFF and opener:
                try:
                    import api_server
                    api_server._create_notification(
                        int(ticket.get("guild_id") or 0), opener, "ticket_reply",
                        f"💬 Reply on ticket #{int(ticket.get('number', 0) or 0):04d}",
                        (message.content or "(attachment)")[:300],
                        {"ticket_id": tid})
                except Exception as exc:
                    log.warning("Could not notify %s of ticket reply: %s", opener, exc)
        except Exception as exc:
            log.warning("Could not mirror ticket message %s: %s", message.id, exc)

    @commands.Cog.listener()
    async def on_ready(self):
        # Auto-post the panel once per process so it's always present without an
        # admin having to run /ticketpanel. Idempotent: skips if one already exists.
        # Runs per guild so every server with a configured panel channel gets one.
        if self._panel_ensured:
            return
        self._panel_ensured = True
        # ... unless this instance does not own the panel. `_find_existing_panel`
        # only recognises a panel THIS bot user posted, so a dev bot sharing the
        # guild cannot see the live one and would post a duplicate under it — whose
        # button it would then serve, taking real tickets into the test bot.
        if not cfg.TICKET_PANEL_ENABLED:
            log.info("Ticket panel auto-post is off (TICKET_PANEL_ENABLED=false); "
                     "leaving the existing panel alone.")
            return
        for guild in self.bot.guilds:
            channel = self._resolve_panel_channel(guild)
            if channel is None:
                continue
            if await _find_existing_panel(channel, self.bot.user.id):
                log.info("Ticket panel already present in #%s", getattr(channel, "name", channel.id))
                continue
            try:
                await channel.send(embed=_panel_embed(), view=TicketPanelView())
                log.info("Auto-posted ticket panel in #%s", getattr(channel, "name", channel.id))
            except discord.Forbidden:
                log.warning("Missing permission to post the ticket panel in channel %s "
                            "(need View Channel + Send Messages + Embed Links)", channel.id)
            except Exception as exc:
                log.warning("Could not auto-post ticket panel: %s", exc)

    @app_commands.command(name="ticketpanel",
                          description="Post the 'Open a Ticket' panel in the ticket channel")
    @app_commands.default_permissions(administrator=True)
    @app_commands.check(lambda interaction: perms.is_admin_user(interaction))
    async def ticketpanel(self, interaction: discord.Interaction):
        """(Admin) Post a fresh ticket panel message.

        The `check` is the real gate: `default_permissions` is only what Discord
        shows by default, and a server admin can hand the command to any role."""
        # Gated by the same switch as the auto-post, and refused out loud rather than
        # quietly ignored. An admin running this on a bot that shares the guild with
        # the live one would stamp a second panel over the real one by hand, which is
        # the exact thing the switch exists to prevent — and a command that appears to
        # do nothing is worse than one that says why.
        if not cfg.TICKET_PANEL_ENABLED:
            await interaction.response.send_message(
                "❌ This bot instance does not own the ticket panel "
                "(`TICKET_PANEL_ENABLED=false`). Posting one here would sit under the "
                "live bot's panel and take its tickets. Run this on the live bot "
                "instead.", ephemeral=True)
            return
        ch_id = guild_config.get_channel_id(interaction.guild_id, "ticket_panel")
        if not ch_id:
            await interaction.response.send_message(
                "❌ No ticket panel channel is configured for this server. "
                "Set one with `/admin setchannel`.", ephemeral=True)
            return
        channel = self._resolve_panel_channel(interaction.guild)
        if channel is None:
            await interaction.response.send_message(
                f"❌ Could not find the configured panel channel (`{ch_id}`) in this server.", ephemeral=True)
            return
        try:
            await channel.send(embed=_panel_embed(), view=TicketPanelView())
        except discord.Forbidden:
            await interaction.response.send_message(
                f"❌ I don't have permission to post in {channel.mention} "
                "(need View Channel + Send Messages + Embed Links).", ephemeral=True)
            return
        await interaction.response.send_message(
            f"✅ Ticket panel posted in {channel.mention}.", ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction,
                                    error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure):
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ You don't have permission to use this command.", ephemeral=True)
        # Anything else is left to the bot-wide handler, which discord.py runs
        # after this one regardless (`CommandTree._dispatch_error`).


async def setup(bot: commands.Bot):
    await bot.add_cog(Tickets(bot))
    # Persistent components survive restarts via their custom_id.
    bot.add_dynamic_items(OpenTicketButton, CloseTicketButton)
