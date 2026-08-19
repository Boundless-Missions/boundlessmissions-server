"""
cogs/ksp_bridge.py – Discord ↔ KSP bridge commands.

Provides:
  /g linkcode — Generate a 6-digit code for KSP account linking
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
)
from data.store import store, _db
from data import guild_config
from i18n import S, tp

log = logging.getLogger(__name__)

# ── i18n ─────────────────────────────────────────────────────────────────────
S.update({
    "ksp.linkcode.title":  {"en": "🎮 KSP Link Code"},
    "ksp.linkcode.desc":   {"en": "Enter this code in KSP:\n\n# `{code}`\n\n⏰ Expires in 3 minutes."},
    "ksp.linkcode.footer": {"en": "Boundless Missions KSP Mod"},
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
    Diagnostics (MAC + KSP.log) arrive as a follow-up once the offending client
    next checks in (see api_server.device_report) — posted into this same ticket.

    Falls back to CONTRACT_MOD_CHANNEL_ID if the ticket system is unconfigured."""
    desc = (
        f"**User:** {data.get('username')} (`{data.get('user_id')}`)\n"
        f"**Unrecognized device:** `{data.get('device_id')}`\n"
        f"**IP:** `{data.get('client_ip') or 'unknown'}`\n\n"
        "The user reports this device isn't theirs. Awaiting the client's "
        "diagnostics (MAC address + KSP.log)…"
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

def _delete_part_catalog(gid: int, uid: int):
    try:
        _db.collection("guilds").document(str(gid)).collection(
            "part_catalogs").document(str(uid)).delete()
    except Exception as exc:
        log.warning("Could not delete part catalog for %s/%s: %s", gid, uid, exc)


class DeleteDataModal(discord.ui.Modal):
    """Confirmation gate: the user must type their exact Discord username before
    any data is erased, so deletion can never happen on a single misclick."""

    def __init__(self, gid: int, uid: int, expected_names: list[str], primary_name: str):
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
            await asyncio.to_thread(_delete_part_catalog, self.gid, self.uid)
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
            "Removed: your profile (XP, balance, levels, language preference), your "
            "KSP session & device bindings, and your installed-parts catalog. Every "
            "linked device has been logged out.\n\n"
            "Records that involve other members (contracts, corporation membership, "
            "marketplace listings) are kept for those members; ask a moderator if "
            "you need those removed too.",
            ephemeral=True,
        )
        log.warning("User %s self-deleted their data in guild %s", self.uid, self.gid)


class KSPBridge(commands.Cog, name="KSPBridge"):
    """Discord ↔ KSP mod integration commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

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

        # Run the blocking Firestore work off the event loop.
        code = await asyncio.to_thread(generate_link_code, gid, uid, username)

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
                "device's IP, MAC, and KSP.log for moderators.\n\n"
                "**Your controls:**\n"
                "• Delete everything → **`deletemydata`**\n"
                "• Log out every device → in-game logout"
            ),
        )
        if settings.PRIVACY_URL:
            e.add_field(name="Privacy Policy", value=settings.PRIVACY_URL, inline=False)
        if settings.TERMS_URL:
            e.add_field(name="Terms of Service", value=settings.TERMS_URL, inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)

    @app_commands.command(name="deletemydata",
                          description="Permanently delete all your Boundless Missions data")
    async def deletemydata(self, interaction: discord.Interaction):
        """Open a confirmation modal, then erase the user's data."""
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "Please run this in the server, not in DMs.", ephemeral=True)
            return
        u = interaction.user
        # Accept any of the user's visible names (handle / global name / nick).
        names = [u.name, getattr(u, "global_name", None), u.display_name]
        modal = DeleteDataModal(interaction.guild_id, u.id, names, u.name)
        await interaction.response.send_modal(modal)


async def setup(bot: commands.Bot):
    await bot.add_cog(KSPBridge(bot))
    # Register the login + device-approval buttons so DM'd prompts keep working
    # after a bot restart (custom_id carries the challenge_id).
    bot.add_dynamic_items(KSPLoginButton, KSPDenyButton,
                          KSPDeviceOkButton, KSPDevicePingButton, KSPDeviceReportButton)
