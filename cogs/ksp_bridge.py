"""
cogs/ksp_bridge.py – Discord ↔ KSP bridge commands.

Provides:
  /b linkcode — Generate a 6-digit code for KSP account linking
  Persistent "🎮 Link KSP" button in missions channel
"""

import asyncio
import logging
import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import View, Button, DynamicItem

import settings
from api_auth import (
    generate_link_code, resolve_approval, resolve_device_challenge,
    purge_ksp_user_data, request_device_ping, set_device_ticket_channel,
    get_linked_guild, logout_all_devices,
)
from data.store import store, _db
from data import accounts, guild_config, twofa
from i18n import S, tp

log = logging.getLogger(__name__)

# ── i18n ─────────────────────────────────────────────────────────────────────
S.update({
    "ksp.linkcode.title":  {"en": "🎮 KSP Link Code"},
    "ksp.linkcode.desc":   {"en": "Enter this code in KSP:\n\n# `{code}`\n\n⏰ Expires in 3 minutes."},
    "ksp.linkcode.footer": {"en": "Boundless Missions KSP Mod"},
    "ksp.linkcode.unavailable": {"en": "⚠️ Couldn't reach the account service to work out which account is yours. No code was issued. Try again in a moment."},
    "ksp.linked.title":    {"en": "✅ KSP Linked"},
    "ksp.linked.desc":     {"en": "Your KSP account has been linked successfully!"},
})


# ── KSP login-approval buttons ────────────────────────────────────────────────
#
# DM'd to the user when a KSP client enters their link code. Pressing "Log in"
# approves the waiting client; "Not me" denies it. Both use DynamicItem so they
# keep working across a bot restart; the challenge_id is carried in the custom_id
# (token_urlsafe → no ':' to clash with the separator). resolve_approval verifies
# the clicker actually owns the challenge before applying the decision.

async def _finish_approval(interaction: discord.Interaction, challenge_id: str, approve: bool):
    # Acknowledge before touching Firestore: resolve_approval is a blocking
    # round-trip that can outlast Discord's 3-second interaction window, which
    # left the decision applied but the prompt un-edited (10062 Unknown
    # interaction) with its buttons still live. A component defer() is a
    # deferred_message_update, so edit_original_response still edits the prompt.
    await interaction.response.defer()

    ok = await asyncio.to_thread(
        resolve_approval, challenge_id, str(interaction.user.id), approve)
    if not ok:
        msg = "⌛ This login request has expired or was already handled."
        color = discord.Color.greyple()
    elif approve:
        msg = "✅ Login approved. Switch back to KSP, it should link automatically."
        color = discord.Color.green()
    else:
        msg = "🚫 Login denied. If that wasn't you, your link code is now useless; generate a fresh one only when *you* want to link."
        color = discord.Color.red()
    e = discord.Embed(description=msg, color=color)
    # Replace the prompt so the buttons can't be pressed again.
    await interaction.edit_original_response(embed=e, view=None)


class KSPLoginButton(DynamicItem[Button], template=r"ksp_login:(?P<chid>[^:]+)"):
    def __init__(self, challenge_id: str):
        super().__init__(Button(label="✅ Log in", style=discord.ButtonStyle.green,
                                custom_id=f"ksp_login:{challenge_id}"))
        self.chid = challenge_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["chid"])

    async def callback(self, interaction: discord.Interaction):
        await _finish_approval(interaction, self.chid, approve=True)


class KSPDenyButton(DynamicItem[Button], template=r"ksp_deny:(?P<chid>[^:]+)"):
    def __init__(self, challenge_id: str):
        super().__init__(Button(label="🚫 Not me", style=discord.ButtonStyle.red,
                                custom_id=f"ksp_deny:{challenge_id}"))
        self.chid = challenge_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["chid"])

    async def callback(self, interaction: discord.Interaction):
        await _finish_approval(interaction, self.chid, approve=False)


class LinkApprovalView(View):
    """The Log-in / Not-me button pair attached to the approval DM."""
    def __init__(self, challenge_id: str):
        super().__init__(timeout=None)
        self.add_item(KSPLoginButton(challenge_id))
        self.add_item(KSPDenyButton(challenge_id))


# ── New-device approval buttons ───────────────────────────────────────────────
#
# DM'd when an unrecognized device tries to use the account (a copied token, a
# reinstall, or a genuine second PC). "Yes, it's me" trusts the device; "No —
# report" rejects it and opens a moderation ticket. Same DynamicItem pattern as
# the login buttons so they survive a bot restart.

async def _post_device_base_ticket(client: discord.Client, data: dict, challenge_id: str):
    """Open a private ticket the moment a user reports an unrecognized device.
    Diagnostics (KSP.log) arrive as a follow-up once the offending client
    next checks in (see api_server.device_report) — posted into this same ticket.

    Falls back to CONTRACT_MOD_CHANNEL_ID if the ticket system is unconfigured."""
    # The username is the player's own string and this embed lands in a moderator
    # ticket, where a masked link is aimed at whoever holds the console. The device
    # id and IP sit in code spans, which markdown does not render.
    desc = (
        f"**User:** {discord.utils.escape_markdown(str(data.get('username') or ''))} "
        f"(`{data.get('user_id')}`)\n"
        f"**Unrecognized device:** `{data.get('device_id')}`\n"
        f"**IP:** `{data.get('client_ip') or 'unknown'}`\n\n"
        "The user reports this device isn't theirs. Awaiting the client's "
        "diagnostics (KSP.log)…"
    )
    guild = None
    gid = data.get("guild_id")
    try:
        from cogs.tickets import create_ticket
        if gid:
            guild = client.get_guild(int(gid))
        if guild is None:
            # Best-effort: find any guild that has a ticket category configured.
            for g in client.guilds:
                if guild_config.resolve_channel(client, g.id, "ticket_category"):
                    guild = g
                    break
        if guild is not None and guild_config.get_channel_id(guild.id, "ticket_category"):
            channel = await create_ticket(
                client, guild,
                opener_id=int(data["user_id"]),
                kind="user",
                title="Account-sharing report",
                description=desc,
                color=discord.Color.red(),
            )
            if channel is not None:
                await asyncio.to_thread(set_device_ticket_channel, challenge_id, channel.id)
                return
    except Exception as exc:
        log.warning("Could not open device-report ticket, falling back: %s", exc)

    # Fallback: the guild's contract-mod channel.
    fb_gid = guild.id if guild is not None else (int(gid) if gid else None)
    ch = guild_config.resolve_channel(client, fb_gid, "contract_mod") if fb_gid else None
    if ch is None:
        log.warning("Device report raised but no ticket category / mod channel set")
        return
    try:
        e = discord.Embed(title="🚨 Account-sharing report", description=desc,
                          color=discord.Color.red())
        await ch.send(embed=e)
    except Exception as exc:
        log.warning("Could not post device-report base ticket: %s", exc)


async def _finish_device(interaction: discord.Interaction, challenge_id: str, approve: bool):
    # Acknowledge first — same reason as _finish_approval above.
    await interaction.response.defer()

    data = await asyncio.to_thread(
        resolve_device_challenge, challenge_id, str(interaction.user.id), approve)
    if data is None:
        msg = "⌛ This device request has expired or was already handled."
        color = discord.Color.greyple()
    elif approve:
        msg = "✅ Device trusted. Switch back to KSP, it should connect now."
        color = discord.Color.green()
    else:
        msg = ("🚨 Reported to the moderators. As a precaution, run **/g logout** to "
               "sign every device out of your account, then re-link only your own PC.")
        color = discord.Color.red()
    e = discord.Embed(description=msg, color=color)
    await interaction.edit_original_response(embed=e, view=None)
    # Open the ticket after responding so the 3s interaction window is never at risk.
    if data is not None and not approve:
        await _post_device_base_ticket(interaction.client, data, challenge_id)


class KSPDeviceOkButton(DynamicItem[Button], template=r"ksp_dev_ok:(?P<chid>[^:]+)"):
    def __init__(self, challenge_id: str):
        super().__init__(Button(label="✅ Yes, it's me", style=discord.ButtonStyle.green,
                                custom_id=f"ksp_dev_ok:{challenge_id}"))
        self.chid = challenge_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["chid"])

    async def callback(self, interaction: discord.Interaction):
        await _finish_device(interaction, self.chid, approve=True)


class KSPDeviceReportButton(DynamicItem[Button], template=r"ksp_dev_no:(?P<chid>[^:]+)"):
    def __init__(self, challenge_id: str):
        super().__init__(Button(label="🚫 No, report it", style=discord.ButtonStyle.red,
                                custom_id=f"ksp_dev_no:{challenge_id}"))
        self.chid = challenge_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["chid"])

    async def callback(self, interaction: discord.Interaction):
        await _finish_device(interaction, self.chid, approve=False)


class KSPDevicePingButton(DynamicItem[Button], template=r"ksp_dev_ping:(?P<chid>[^:]+)"):
    """🔔 Pings the blocked PC so the owner can confirm it's in front of them
    before reporting. Keeps the approve/report buttons usable (ephemeral reply)."""
    def __init__(self, challenge_id: str):
        super().__init__(Button(label="🔔 Ping this PC", style=discord.ButtonStyle.grey,
                                custom_id=f"ksp_dev_ping:{challenge_id}"))
        self.chid = challenge_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["chid"])

    async def callback(self, interaction: discord.Interaction):
        # thinking=True so the defer is a new ephemeral message rather than an edit:
        # the approve/report prompt has to survive a ping.
        await interaction.response.defer(ephemeral=True, thinking=True)

        ok = await asyncio.to_thread(
            request_device_ping, self.chid, str(interaction.user.id))
        if ok:
            msg = ("🔔 **Ping sent.** Look at the PC that's trying to log in; within a "
                   "few seconds it should flash an *“Is this you?”* alert on its screen.\n\n"
                   "• If you see that alert on a PC in front of you, it's **you**, so press "
                   "**✅ Yes, it's me**.\n"
                   "• If no PC you can see lights up, it isn't you, so press **🚫 No, report it**.")
        else:
            msg = "⌛ This device request has expired or was already handled, so the ping couldn't be sent."
        await interaction.followup.send(msg, ephemeral=True)


class DeviceApprovalView(View):
    """The Yes-it's-me / Ping / No-report buttons attached to the new-device DM."""
    def __init__(self, challenge_id: str):
        super().__init__(timeout=None)
        self.add_item(KSPDeviceOkButton(challenge_id))
        self.add_item(KSPDevicePingButton(challenge_id))
        self.add_item(KSPDeviceReportButton(challenge_id))


# ── Data deletion (user "delete my data") ─────────────────────────────────────

def _delete_avatar(uid: str) -> None:
    """Drop the account's uploaded profile picture. The path is fixed by the
    uploader (`avatars/{account_id}` in api_server), so this needs no lookup; a
    player who never uploaded one simply has nothing to delete."""
    try:
        from data.contracts import delete_stored_file
        delete_stored_file(f"avatars/{uid}")
    except Exception as exc:
        log.warning("Could not delete avatar for %s: %s", uid, exc)


def _delete_part_catalogs_everywhere(uid: str) -> int:
    """Drop this player's uploaded part catalog in EVERY guild, not just one.

    The catalog is a full list of the mods installed on their machine, and the caller
    used to pass the guild the slash command happened to be run in — so a player in two
    servers deleted one copy and kept the other. Nothing about the catalog is
    guild-specific; only its storage path is.
    """
    n = 0
    try:
        for gdoc in _db.collection("guilds").stream():
            try:
                ref = gdoc.reference.collection("part_catalogs").document(uid)
                if ref.get().exists:
                    ref.delete()
                    n += 1
            except Exception as exc:
                log.warning("Could not delete part catalog for %s in %s: %s",
                            uid, gdoc.id, exc)
    except Exception as exc:
        log.warning("Could not enumerate guilds for part-catalog purge of %s: %s", uid, exc)
    return n


def _delete_subcollection(ref, *, batch: int = 300) -> int:
    """Delete every document under `ref`, in pages. Returns how many went."""
    n = 0
    while True:
        docs = list(ref.limit(batch).stream())
        if not docs:
            return n
        for d in docs:
            d.reference.delete()
            n += 1
        if len(docs) < batch:
            return n


def _purge_player_records(uid: str) -> dict:
    """Everything that is unambiguously THIS player's and has no counterparty.

    The self-service and moderator delete paths had drifted in both directions — one
    removed the avatar and a single guild's part catalog, the other removed neither —
    so this is the one function both call, and the place to add anything new.

    What is deliberately NOT here: contracts, auctions, marketplace listings, tickets,
    reports and moderation records. Each has another party whose own history would be
    falsified by removing it, which is what the confirmation message tells the player.
    Their listings are DELISTED rather than deleted, so nothing new is sold on behalf
    of an account that is gone while existing buyers keep their downloads.
    """
    out = {"achievements": False, "votes": False, "catalogs": 0,
           "notifications": 0, "imports": 0, "corp": False, "listings_delisted": 0}

    # Achievement progress and marketplace votes: one document each, keyed by the
    # player, meaningful to nobody else. Both were unknown to every deletion path.
    for coll, key in (("ksp_achievements", "achievements"), ("marketplace_votes", "votes")):
        try:
            ref = _db.collection(coll).document(uid)
            if ref.get().exists:
                ref.delete()
                out[key] = True
        except Exception as exc:
            log.warning("Could not delete %s for %s: %s", coll, uid, exc)

    out["catalogs"] = _delete_part_catalogs_everywhere(uid)

    # The notification feed and the craft-import queue are per-guild subtrees. The feed
    # is unbounded (reads cap at 50, the collection does not) and holds a readable
    # history of who this player dealt with; the import queue holds pending deliveries.
    try:
        for gdoc in _db.collection("guilds").stream():
            for coll, key in (("ksp_notifications", "notifications"),
                              ("ksp_craft_imports", "imports")):
                try:
                    out[key] += _delete_subcollection(
                        gdoc.reference.collection(coll).document(uid).collection("items"))
                    gdoc.reference.collection(coll).document(uid).delete()
                except Exception as exc:
                    log.warning("Could not purge %s for %s in %s: %s", coll, uid, gdoc.id, exc)
    except Exception as exc:
        log.warning("Could not enumerate guilds for feed purge of %s: %s", uid, exc)

    # The corporation record carries their display name and avatar and is served to
    # every other player by the pickers, so it outlived them in other people's UI.
    # The CHANNEL is left to the caller, which has a bot instance to delete it with.
    try:
        from cogs import corps as _corps
        where = _corps.get_user_corp_global(int(uid)) if uid.isdigit() else None
        if where:
            _corps._delete_corp(int(where["guild_id"]), int(uid))
            _corps._owner_ref(int(uid)).delete()
            out["corp"] = True
    except Exception as exc:
        log.warning("Could not delete corp record for %s: %s", uid, exc)

    # Listings are delisted, never deleted: the ToS distinguishes stopping further
    # distribution from recalling copies already delivered, and a buyer's re-download
    # must keep working.
    try:
        from data import marketplace as _mkt
        for doc in _db.collection("marketplace").where("seller_id", "==", uid).stream():
            if (doc.to_dict() or {}).get("status") == "active":
                doc.reference.update({"status": "delisted"})
                out["listings_delisted"] += 1
    except Exception as exc:
        log.warning("Could not delist listings for %s: %s", uid, exc)

    return out


def _purge_player(uid: str) -> None:
    """The identity half of a deletion, run in a thread.

    `store.delete_user` and `purge_ksp_user_data` erase the *player* — the wallet,
    the XP, the session and the device bindings. They do not touch the **account**:
    the record holding the email address, the display name, the avatar, the username
    reservation, the friend graph, the crew hand-over ledger and the TOTP secret all
    survived a self-service deletion that told the player everything was gone. The
    moderator path (`api_server.admin_user_delete`) already did all of this and says
    in its own comment that it leaves no more behind than this one does; that is now
    true. Run AFTER the sessions are revoked, so there is no window where the account
    record is gone but a live token still resolves to it.
    """
    _purge_player_records(uid)         # achievements, votes, catalogs, feeds, corp, listings
    accounts.delete_account(uid)       # account doc, username, indexes, friends, crew ledger,
                                       # and the Firebase Authentication user (the email)
    twofa.purge(uid)                   # the second factor is part of the identity
    _delete_avatar(uid)                # the one item that lives in Storage, not Firestore


class DeleteDataModal(discord.ui.Modal):
    """Confirmation gate: the user must type their exact Discord username before
    any data is erased, so deletion can never happen on a single misclick.

    `uid` is an ACCOUNT id (a snowflake for most players, `a_…` for one who linked
    Discord onto a website account), resolved by the caller — never a raw snowflake,
    or this deletes an empty record and reports success.
    """

    def __init__(self, gid: int, uid: str, expected_names: list[str], primary_name: str):
        super().__init__(title="⚠️ Delete My Data")
        self.gid = gid
        self.uid = uid
        self._expected = {n.strip().lower() for n in expected_names if n}
        self.confirm = discord.ui.TextInput(
            label="Type your username to confirm",
            placeholder=f"Type exactly: {primary_name}",
            required=True,
            max_length=64,
        )
        self.add_item(self.confirm)

    async def on_submit(self, interaction: discord.Interaction):
        typed = str(self.confirm.value).strip().lstrip("@").lower()
        if typed not in self._expected:
            await interaction.response.send_message(
                "❌ That didn't match your username, so **nothing was deleted**. "
                "Run the command again and type your username exactly.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await store.delete_user(self.gid, self.uid)
            await asyncio.to_thread(purge_ksp_user_data, str(self.uid))
            # `_purge_player` now covers the part catalog in EVERY guild, so the
            # single-guild call that used to live here is gone rather than duplicated.
            await asyncio.to_thread(_purge_player, str(self.uid))
        except Exception as exc:
            log.error("delete-my-data failed for %s/%s: %s", self.gid, self.uid, exc)
            await interaction.followup.send(
                "⚠️ Something went wrong while deleting your data. Please contact a "
                "moderator so it can be done manually.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            "✅ **Your data has been deleted.**\n"
            "Removed: your profile (XP, balance, levels, language preference); your "
            "account record, including your email address, display name, username "
            "and profile picture; your sign-in credential itself; two-factor "
            "enrolment and recovery codes; your friend list (and your entry in other "
            "players' lists); the crew hand-over ledger; your KSP session & device "
            "bindings; your installed-parts catalog in every server; your achievement "
            "progress and marketplace votes; your notification history and pending "
            "craft deliveries; and your corporation record. Every linked device has "
            "been logged out, and your marketplace listings have been delisted so "
            "nothing new is sold.\n\n"
            "Kept, because they are also somebody else's record: contracts and "
            "auctions you were party to, support tickets, and craft files already "
            "bought by other players, a buyer's download has to keep working. Ask a "
            "moderator if you need any of those looked at.",
            ephemeral=True,
        )
        log.warning("User %s self-deleted their data in guild %s", self.uid, self.gid)


class KSPBridge(commands.Cog, name="KSPBridge"):
    """Discord ↔ KSP mod integration commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.abc.User):
        """A ban must also end API access. Session tokens live 30 days and bake in
        the guild they were linked through, so without this a banned player kept
        the marketplace, contracts and their wallet from in-game and the website
        for up to a month after the door closed in Discord.

        Fires for bot-issued and manual Discord bans alike. Scoped: sessions are
        revoked only when the banned guild is the one the session's authority came
        from (see api_auth.get_linked_guild) — a ban in some unrelated server the
        bot also sits in must not log a player out of their own community."""
        def _revoke() -> bool:
            # Resolve the snowflake to the ACCOUNT id first. For almost everyone
            # those are the same string, but a player who linked Discord onto a
            # website account they already had is keyed on `a_…` — and there the
            # raw snowflake names a session document that does not exist, so
            # get_linked_guild returned None, this returned early, and the ban
            # revoked nothing at all. The player kept the marketplace, contracts
            # and their wallet for the rest of the token's 30 days, which is
            # exactly the window this hook exists to close. Same correction
            # /linkcode and /linkas already had.
            uid = accounts.account_for_discord(user.id) or str(user.id)
            if get_linked_guild(uid) != str(guild.id):
                return False
            logout_all_devices(uid)
            return True

        try:
            if await asyncio.to_thread(_revoke):
                log.info("Revoked KSP/web sessions for banned user %s (guild %s)",
                         user.id, guild.id)
        except Exception as exc:
            log.warning("Could not revoke sessions for banned user %s: %s", user.id, exc)

    @app_commands.command(name="linkcode", description="Generate a 6-digit code to link your KSP game")
    async def linkcode(self, interaction: discord.Interaction):
        """Generate a link code for KSP account linking."""
        # Acknowledge immediately: generate_link_code makes blocking Firestore
        # calls (query + deletes + write) that can exceed Discord's 3-second
        # interaction window and otherwise raise 10062 (Unknown interaction).
        await interaction.response.defer(ephemeral=True)

        gid = interaction.guild_id
        uid = interaction.user.id
        username = interaction.user.display_name

        # The code is minted for the ACCOUNT this Discord user signs in as, not
        # for the snowflake. For almost everyone they are the same string; they
        # differ for a player who linked Discord onto a website account that
        # already had history (`accounts.join_accounts` keeps the web side and
        # points `account_discord/{snowflake}` at it). A code minted on the
        # snowflake there gave the game a token for an orphan wallet the account
        # never reads, and a console suspension issued on the account id the
        # Users tab shows did not cover that token. A failed index read is
        # refused rather than guessed, for the reason `targets.resolve` gives.
        account_id = await asyncio.to_thread(accounts.account_for_discord, uid)
        if account_id is None:
            await interaction.followup.send(
                tp(gid, uid, "ksp.linkcode.unavailable"), ephemeral=True)
            return

        # Run the blocking Firestore work off the event loop.
        code = await asyncio.to_thread(generate_link_code, gid, account_id, username)

        embed = discord.Embed(
            title=tp(gid, uid, "ksp.linkcode.title"),
            description=tp(gid, uid, "ksp.linkcode.desc", code=code),
            color=discord.Color.from_rgb(0, 180, 100),
        )
        embed.set_footer(text=tp(gid, uid, "ksp.linkcode.footer"))
        embed.set_thumbnail(url="https://cdn.discordapp.com/emojis/1510200111253291258.webp")

        await interaction.followup.send(embed=embed, ephemeral=True)
        log.info("%s generated KSP link code", interaction.user)

    @app_commands.command(name="privacy",
                          description="How Boundless Missions uses your data, and how to delete it")
    async def privacy(self, interaction: discord.Interaction):
        """Show a privacy/terms summary and links."""
        e = discord.Embed(
            title="🔒 Privacy & Terms",
            color=discord.Color.blurple(),
            description=(
                "**What Boundless Missions stores about you:**\n"
                "• Your Discord ID and gameplay progress (XP, balance, levels, "
                "contracts, corp, marketplace).\n"
                "• KSP linking & security: a session token (on your device) and a "
                "**random device id** bound to your account.\n"
                "• Content you submit: screenshots, craft, telemetry, mod/part lists.\n\n"
                "**AI:** screenshots and mission text may be processed by Google's "
                "Gemini to provide features.\n"
                "**Moderation report:** only if *you* file one, it collects that "
                "device's IP and KSP.log for moderators.\n\n"
                "**Your controls:**\n"
                "• Delete your profile and account → **`deletemydata`**\n"
                "• Log out every device → in-game logout"
            ),
        )
        if settings.PRIVACY_URL:
            e.add_field(name="Privacy Policy", value=settings.PRIVACY_URL, inline=False)
        if settings.TERMS_URL:
            e.add_field(name="Terms of Service", value=settings.TERMS_URL, inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)

    @app_commands.command(name="deletemydata",
                          description="Permanently delete your profile, account and sign-in")
    async def deletemydata(self, interaction: discord.Interaction):
        """Open a confirmation modal, then erase the user's data."""
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "Please run this in the server, not in DMs.", ephemeral=True)
            return
        u = interaction.user
        # The data to erase hangs off the ACCOUNT id, which is the snowflake for
        # almost everybody but `a_…` for a player who linked Discord onto a website
        # account they already had. Deleting by snowflake there removed an empty
        # record and reported success while the real wallet, the real session and
        # every live token survived — a deletion request that deletes nothing is
        # worse than one that fails, because nobody comes back to check.
        account_id = await asyncio.to_thread(accounts.account_for_discord, u.id)
        if not account_id:
            await interaction.response.send_message(
                "❌ I couldn't look your account up just now, so **nothing was "
                "deleted**. Please try again in a moment.", ephemeral=True)
            return
        # Accept any of the user's visible names (handle / global name / nick).
        names = [u.name, getattr(u, "global_name", None), u.display_name]
        modal = DeleteDataModal(interaction.guild_id, account_id, names, u.name)
        await interaction.response.send_modal(modal)


async def setup(bot: commands.Bot):
    await bot.add_cog(KSPBridge(bot))
    # Register the login + device-approval buttons so DM'd prompts keep working
    # after a bot restart (custom_id carries the challenge_id).
    bot.add_dynamic_items(KSPLoginButton, KSPDenyButton,
                          KSPDeviceOkButton, KSPDevicePingButton, KSPDeviceReportButton)
