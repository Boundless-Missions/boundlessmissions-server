"""Contract interactive button views.

All buttons use DynamicItem with regex-matched custom_ids so they
survive bot restarts. Contract/guild info is encoded in the custom_id:
"prefix:contract_id:guild_id"

**These buttons do not implement contract transitions.** Every one of them calls
`contract_actions`, which is also what the KSP mod's HTTP endpoints call, and then
renders the outcome. Buttons decide what the message looks like; that module decides
what happens. See its docstring for why the two used to disagree.

Authorization is now passed explicitly rather than inferred from where the view was
delivered. That matters because not every view lands in a DM: `ContractWorkView` is
posted to a public corp channel by the weekly-mission flow, and `ModReviewView` lands
in a dispute ticket both parties can see.
"""
import logging
import re

import discord
from discord.ui import View, Button, DynamicItem, button
from i18n import t
import settings
from data import contracts as cdb
from data import mission_constraints as mc
from data import orbit_constraints as oc
import contract_actions as ca
from cogs import perms

log = logging.getLogger(__name__)

_LS_NAMES = {"usi": "USI-LS", "tac": "TAC-LS", "snacks": "Snacks", "kerbalism": "Kerbalism"}


def _crew_requirement_text(constraints: dict) -> str | None:
    """Human phrase for a contract's crew-aboard requirement — the head count, the
    professions it names, or both — or None.

    A profession from a mod gets the mod named on a second line. Nothing else in the
    embed can say it: `modlist` is the craft's parts, and a profession has no part to
    trace, so a contractor reading "2× Kolonist" would otherwise have to already know
    where Kolonists come from to know whether they can take the contract at all.
    """
    mn = constraints.get("min_crew")
    mx = constraints.get("max_crew")
    traits = ", ".join(mc.crew_trait_phrases(constraints))
    mods = mc.crew_trait_mod_requirements(constraints)
    mod_line = "\n🧩 needs " + "; ".join(mods) if mods else ""

    if mx == 0:
        count = "uncrewed — nobody aboard"
    elif mn and mx:
        count = f"exactly {mn} aboard" if mn == mx else f"{mn}–{mx} aboard"
    elif mx:
        count = f"up to {mx} aboard"
    elif mn:
        count = f"at least {mn} aboard"
    else:
        return (traits + mod_line) if traits else None
    return (f"{count} ({traits})" if traits else count) + mod_line


def _ls_endurance_text(c: dict, constraints: dict) -> str | None:
    """Min–max life-support endurance for the contract's crew range, when the contract
    carries craft LS info (populated at submission, or at creation for a rescue).
    Endurance is per-kerbal, so more crew = shorter; the range spans the required crew
    band (or 1..capacity)."""
    key = (c.get("life_support") or "none").lower()
    per_kerbal = float(c.get("ls_endurance_days") or 0.0)
    if key not in _LS_NAMES:
        return None
    name = _LS_NAMES[key]
    # Which LS mod the craft runs is worth saying even when the days aren't known — a
    # Kerbalism install whose profile rates couldn't be read reports 0. Silence here
    # would drop the flag entirely, which is what the marketplace embed avoids too.
    if per_kerbal <= 0:
        return f"{name} · endurance n/a"
    cap = int(c.get("ls_crew_capacity") or 0)
    # An uncrewed contract has no endurance to report — nobody is eating.
    if constraints.get("max_crew") == 0:
        return None
    lo = constraints.get("min_crew") or 1
    hi = constraints.get("max_crew") or cap or lo
    lo, hi = max(1, min(lo, hi)), max(1, max(lo, hi))
    longest = per_kerbal / lo
    shortest = per_kerbal / hi
    if hi > lo:
        return f"{name} · ~{shortest:.0f}–{longest:.0f} d for {lo}–{hi} kerbals"
    return f"{name} · ~{longest:.0f} d for {lo} kerbal" + ("s" if lo != 1 else "")

def _rescue_terms_text(c: dict) -> str | None:
    """What a rescue actually asks for, in one line: where to deliver, whether the
    wreck has to come too, and any delta-v the crew must be left with. None for
    anything that isn't a rescue."""
    if c.get("mission_type") != cdb.RESCUE:
        return None
    rt = c.get("rescue_target") or {}
    if not rt:
        return None

    body = rt.get("body") or "?"
    if (rt.get("mode") or "orbit").lower() == "surface":
        where = f"land at **{body}** {float(rt.get('lat') or 0):.1f}°, {float(rt.get('lon') or 0):.1f}°"
    else:
        where = (f"orbit **{body}** at "
                 f"{float(rt.get('ap') or 0) / 1000:.0f}×{float(rt.get('pe') or 0) / 1000:.0f} km")
        # The plane / regime, when the issuer asked for one. Ap/Pe alone don't say
        # which orbit this is, and matching the plane is the expensive half.
        orbit_req = oc.describe_target(rt.get("inc"), rt.get("margin_inc"),
                                       rt.get("orbit_types"))
        if orbit_req:
            where += f" (**{orbit_req}**)"

    bits = [where]
    bits.append("bring the **stranded vessel** back too"
                if (rt.get("recovery") or "crew").lower() == "vessel"
                else "the crew alone (the wreck may be left behind)")
    min_dv = float(rt.get("min_dv") or 0.0)
    if min_dv > 0:
        bits.append(f"arrive with **≥{min_dv:.0f} m/s** Δv left")
    return " · ".join(bits)


# ── Regex pattern reused by all buttons ──────────────────────────────────────
# contract_ids are Firestore auto-IDs (alphanumeric), guild_ids are snowflakes
_ID_PATTERN = r"(?P<cid>[^:]+):(?P<gid>\d+)"


def _cid(prefix: str, contract_id: str, guild_id: int) -> str:
    return f"{prefix}:{contract_id}:{guild_id}"


def _actor(interaction: discord.Interaction) -> tuple[int, str]:
    """Who is performing the action, for the service layer's authorization check.

    This is deliberately `interaction.user` and *not* `perms.real_user`: swapping
    business identity is exactly what the admin mimic system exists to do, so an admin
    mimicking a player acts as that player here. Checks about *authority* — moderator
    powers — must still go through `perms`, which unwraps the swap.
    """
    return interaction.user.id, interaction.user.display_name


async def _require_mod(interaction: discord.Interaction) -> bool:
    """Gate for the moderator-resolution buttons.

    `ModReviewView` is posted into a dispute ticket that grants **both disputing
    parties** view and send access, so being able to see the buttons is not evidence of
    authority — the contractor could otherwise cancel their own fine. `contract_actions`
    cannot check this itself: moderator-ness is a Discord role fact.

    Goes through `perms`, which unwraps the admin mimic swap, so mimicking a moderator
    does not borrow their authority.
    """
    if perms.is_mod_user(interaction):
        return True
    await interaction.followup.send(
        "❌ Only moderators can resolve an escalated dispute.", ephemeral=True)
    return False


async def _reject(interaction: discord.Interaction, result: ca.Result) -> None:
    """Tell whoever pressed the button why nothing happened.

    Ephemeral, always: these views live in corp channels and dispute tickets as well as
    DMs, and a bystander's misfire should not rewrite what everyone else is looking at.
    """
    await interaction.followup.send(f"❌ {result.message}", ephemeral=True)


def _embed(c, guild_id):
    is_flag = c.get("mission_type") == cdb.FLAG_DESIGN
    title = f"🚩 {t(guild_id, 'ct.title')}" if is_flag else f"📜 {t(guild_id, 'ct.title')}"
    e = discord.Embed(title=title, color=discord.Color.gold())
    sym = settings.CURRENCY_SYMBOL
    e.add_field(name=t(guild_id, "ct.mission"), value=c["mission"], inline=False)
    e.add_field(name=t(guild_id, "ct.issuer"), value=c["issuer_name"], inline=True)
    e.add_field(name=t(guild_id, "ct.contractor"), value=c["contractor_name"], inline=True)
    e.add_field(name=t(guild_id, "ct.payment"), value=f"**{c['payment']}** {sym}", inline=True)
    e.add_field(name=t(guild_id, "ct.fine"), value=f"**{c['fine']}** {sym}", inline=True)
    e.add_field(name=t(guild_id, "ct.due"), value=c["due_date"], inline=True)
    e.add_field(name=t(guild_id, "ct.status"), value=f"`{c['status']}`", inline=True)

    # Crew-aboard requirement and (once a craft has been submitted) its min–max
    # life-support endurance for that crew. Constraints come off the contract when
    # present, else are derived from the mission text.
    constraints = c.get("constraints") or mc.extract_heuristic(c.get("mission", ""))
    crew_txt = _crew_requirement_text(constraints)
    if crew_txt:
        e.add_field(name="👨‍🚀 Crew", value=crew_txt, inline=True)
    ls_txt = _ls_endurance_text(c, constraints)
    if ls_txt:
        e.add_field(name="🥫 Life Support", value=ls_txt, inline=True)

    # An orbit the mission text names ("reach a polar orbit around Kerbin"). Enforced
    # at submit time either way; shown here because a requirement the contractor only
    # meets by accident is not a requirement they were told about.
    orbit_c = oc.extract_heuristic(c.get("mission", ""))
    if not oc.is_empty(orbit_c):
        e.add_field(name="🛰 Orbit",
                    value=", ".join(oc.label(r) for r in orbit_c["requirements"]),
                    inline=True)

    rescue_txt = _rescue_terms_text(c)
    if rescue_txt:
        e.add_field(name="🛟 Rescue Terms", value=rescue_txt, inline=False)

    if c.get("modlist"):
        # Truncate if necessary to fit in Discord's 1024 char limit for fields
        mod_text = c["modlist"]
        if len(mod_text) > 1000:
            mod_text = mod_text[:1000] + "..."
        e.add_field(name="Required Mods", value=f"```\n{mod_text}\n```", inline=False)

    # Flag-design contracts ride the watermarked preview along on every embed.
    # The clean full-res image stays gated until the contract completes.
    if is_flag and c.get("flag_preview_url"):
        e.set_image(url=c["flag_preview_url"])

    return e


# ══════════════════════════════════════════════════════════════════════════════
#  DynamicItem Button Classes
# ══════════════════════════════════════════════════════════════════════════════

# ── Offer View Buttons (Accept / Refuse) ─────────────────────────────────────

class AcceptOfferButton(DynamicItem[Button], template=r"ct_accept:" + _ID_PATTERN):
    def __init__(self, contract_id: str, guild_id: int):
        super().__init__(Button(label="✅ Accept", style=discord.ButtonStyle.green,
                                custom_id=_cid("ct_accept", contract_id, guild_id)))
        self.cid = contract_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        uid, name = _actor(interaction)
        r = await ca.accept(self.gid, self.cid, actor_id=uid, actor_name=name)
        if not r.ok:
            await _reject(interaction, r)
            return
        e = _embed(r.contract, self.gid)
        e.color = discord.Color.green()
        await interaction.edit_original_response(
            embed=e, view=ContractWorkView(self.cid, self.gid, r.contract.get("mission_type")))


class RefuseOfferButton(DynamicItem[Button], template=r"ct_refuse:" + _ID_PATTERN):
    def __init__(self, contract_id: str, guild_id: int):
        super().__init__(Button(label="❌ Refuse", style=discord.ButtonStyle.red,
                                custom_id=_cid("ct_refuse", contract_id, guild_id)))
        self.cid = contract_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        uid, name = _actor(interaction)
        r = await ca.cancel(self.gid, self.cid, actor_id=uid, actor_name=name)
        if not r.ok:
            await _reject(interaction, r)
            return
        e = _embed(r.contract, self.gid)
        e.color = discord.Color.red()
        await interaction.edit_original_response(embed=e, view=None)


# ── Work View Buttons (Give Up / Submit) ─────────────────────────────────────

class GiveUpButton(DynamicItem[Button], template=r"ct_giveup:" + _ID_PATTERN):
    def __init__(self, contract_id: str, guild_id: int):
        super().__init__(Button(label="🏳️ Give Up", style=discord.ButtonStyle.grey,
                                custom_id=_cid("ct_giveup", contract_id, guild_id)))
        self.cid = contract_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        uid, name = _actor(interaction)
        # Giving up costs the agreed fine — it always did over the API, and now it does
        # here too. This view is posted to a public corp channel for weekly missions,
        # so the contractor check inside the service is what stops a bystander from
        # closing someone else's contract.
        r = await ca.give_up(self.gid, self.cid, actor_id=uid, actor_name=name)
        if not r.ok:
            await _reject(interaction, r)
            return
        e = _embed(r.contract, self.gid)
        e.color = discord.Color.red()
        e.set_footer(text=r.message)
        await interaction.edit_original_response(embed=e, view=None)


# Submitting from Discord is retired for everything except a flag design.
#
# A craft submission is read against the game — the vessel, its mod list, its parts
# and its telemetry — none of which a channel upload carries, so the mod's submit
# window (`SubmissionSession`) is the only front end that can send a real one. This
# button survives for two reasons: a flag *is* just an image, which makes Discord the
# right place to hand one over (the mod's own contract form says so), and every work
# view already posted to a corp channel still has this custom_id, so the deprecated
# case has to answer with an explanation rather than a dead interaction.
SUBMIT_MOVED_NOTICE = (
    "📤 Submissions now happen in KSP. Open the sidebar's **Contracts** panel and "
    "press **Submit** there — the mod sends the craft, its mod list and the telemetry "
    "the review is judged against, which an upload here cannot carry."
)


class SubmitButton(DynamicItem[Button], template=r"ct_submit:" + _ID_PATTERN):
    def __init__(self, contract_id: str, guild_id: int):
        super().__init__(Button(label="📤 Submit flag", style=discord.ButtonStyle.blurple,
                                custom_id=_cid("ct_submit", contract_id, guild_id)))
        self.cid = contract_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        c = cdb.get_contract(self.gid, self.cid)
        if not c or c["status"] != cdb.ACTIVE:
            return
        if c.get("mission_type") != cdb.FLAG_DESIGN:
            await interaction.followup.send(SUBMIT_MOVED_NOTICE, ephemeral=True)
            return
        # This view is not always in a DM — it was posted to public corp channels for
        # years and those messages are still there — so the presser is not necessarily
        # the contractor. Without this check a bystander could open the picker and
        # submit their own upload against someone else's contract.
        if str(interaction.user.id) != str(c.get("contractor_id")):
            await interaction.followup.send(
                "❌ This contract is not yours to submit.", ephemeral=True)
            return
        # Get the real user ID in case an admin is mimicking someone
        real_user = getattr(interaction, "extras", {}).get("_mimic_real_user", interaction.user)
        real_user_id = real_user.id if real_user else interaction.user.id

        # Scan channel backwards for recent files, stopping at contract msg
        files_found = []
        dm_msg_id = int(c.get("dm_message_id") or 0)
        async for msg in interaction.channel.history(limit=50):
            # Stop scanning if we hit the contract message
            if dm_msg_id and msg.id <= dm_msg_id:
                break

            if msg.author.id in (interaction.user.id, real_user_id):
                for att in reversed(msg.attachments):
                    files_found.append({"url": att.url, "filename": att.filename,
                                        "content_type": att.content_type or "application/octet-stream"})
        # Reverse so order is chronological. A flag design is an image and nothing
        # else, so anything that isn't one is not worth offering as a choice.
        files_found.reverse()
        files_found = [f for f in files_found if f["content_type"].startswith("image/")]
        if not files_found:
            await interaction.followup.send(
                "❌ No image found. Upload the flag image here first.", ephemeral=True)
            return
        view = FlagSubmitView(self.cid, self.gid, files_found)
        await interaction.followup.send(embed=view._generate_embed(), view=view, ephemeral=True)


# ── Review View Buttons (Issuer accepts/refuses submission) ──────────────────

class ReviewAcceptButton(DynamicItem[Button], template=r"ct_rv_acc:" + _ID_PATTERN):
    def __init__(self, contract_id: str, guild_id: int):
        super().__init__(Button(label="✅ Accept", style=discord.ButtonStyle.green,
                                custom_id=_cid("ct_rv_acc", contract_id, guild_id)))
        self.cid = contract_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        uid, name = _actor(interaction)
        # Payment, the rescue hand-back (craft + kerbals + the rescuer's stat) and the
        # flag delivery all happen inside the service — this button only reveals the
        # files that were gated until approval.
        r = await ca.review(self.gid, self.cid, actor_id=uid, actor_name=name, approve=True)
        if not r.ok:
            await _reject(interaction, r)
            return
        c = r.contract
        e = _embed(c, self.gid)
        e.color = discord.Color.green()
        # Reveal craft files (screenshots were already visible)
        files = c.get("submitted_files", [])
        craft_files = [s for s in files if s['filename'].lower().endswith('.craft')]
        if craft_files:
            # The craft object is private; this Discord link is the secondary
            # "also download here" convenience (the in-game import queue is the
            # primary, always-fresh path), so sign it with the 7-day max TTL.
            flist = "\n".join(
                f"🚀 [{s['filename']}]({cdb.sign_stored(s['url'], ttl=cdb.SIGNED_URL_MAX_TTL)})"
                for s in craft_files)
            e.add_field(name="📁 Craft Files", value=flist, inline=False)
        screenshots = [s for s in files if not s['filename'].lower().endswith('.craft')]
        if screenshots:
            flist = "\n".join(f"🖼️ [{s['filename']}]({s['url']})" for s in screenshots)
            e.add_field(name="🖼️ Screenshots", value=flist, inline=False)
        # Flag-design: reveal the clean full-res flag now that it's paid for. The
        # object is private; sign it (7-day max TTL) for the embed image + link, which
        # Discord fetches on post. The in-game flag-picker delivery is the durable path.
        if c.get("mission_type") == cdb.FLAG_DESIGN and c.get("flag_fullres_url"):
            fullres = cdb.sign_stored(c["flag_fullres_url"], ttl=cdb.SIGNED_URL_MAX_TTL)
            e.set_image(url=fullres)
            e.add_field(name="🚩 Flag (full-res)",
                        value=f"[Download]({fullres}); also queued to your "
                              "in-game flag picker.", inline=False)
        # The contractor's "accepted" DM is sent by the service, so every front end
        # produces it — not just this button.
        await interaction.edit_original_response(embed=e, view=None)


class ReviewRefuseButton(DynamicItem[Button], template=r"ct_rv_ref:" + _ID_PATTERN):
    def __init__(self, contract_id: str, guild_id: int):
        super().__init__(Button(label="❌ Refuse", style=discord.ButtonStyle.red,
                                custom_id=_cid("ct_rv_ref", contract_id, guild_id)))
        self.cid = contract_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        uid, name = _actor(interaction)
        # The service opens the dispute and DMs the contractor their options.
        r = await ca.review(self.gid, self.cid, actor_id=uid, actor_name=name, approve=False)
        if not r.ok:
            await _reject(interaction, r)
            return
        e = _embed(r.contract, self.gid)
        e.color = discord.Color.red()
        await interaction.edit_original_response(embed=e, view=None)


# ── Dispute View Buttons (Settle / More Time / Pay Fine / Sue) ───────────────

class SettleButton(DynamicItem[Button], template=r"ct_settle:" + _ID_PATTERN):
    def __init__(self, contract_id: str, guild_id: int):
        super().__init__(Button(label="🤝 Settle", style=discord.ButtonStyle.grey,
                                custom_id=_cid("ct_settle", contract_id, guild_id)))
        self.cid = contract_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        uid, name = _actor(interaction)
        r = await ca.dispute(self.gid, self.cid, actor_id=uid, actor_name=name,
                             action="settle")
        if not r.ok:
            await _reject(interaction, r)
            return
        await interaction.followup.send(t(self.gid, "ct.settle_sent"), ephemeral=True)


class MoreTimeButton(DynamicItem[Button], template=r"ct_moretime:" + _ID_PATTERN):
    def __init__(self, contract_id: str, guild_id: int):
        super().__init__(Button(label="⏰ More Time", style=discord.ButtonStyle.grey,
                                custom_id=_cid("ct_moretime", contract_id, guild_id)))
        self.cid = contract_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        c = cdb.get_contract(self.gid, self.cid)
        # A human issuer has to agree to an extension, so that path opens a date modal
        # and the service turns it into a request. A bot issuer has nobody to ask, so
        # the service extends it on the spot — the only branch this button needs to
        # know about, because the two produce different UI.
        if not c or str(c["issuer_id"]) != str(interaction.client.user.id):
            await interaction.response.send_modal(MoreTimeModal(self.cid, self.gid))
            return

        await interaction.response.defer()
        uid, name = _actor(interaction)
        r = await ca.dispute(self.gid, self.cid, actor_id=uid, actor_name=name,
                             action="more_time")
        if not r.ok:
            await _reject(interaction, r)
            return

        c = r.contract
        e = _embed(c, self.gid)
        v = ContractWorkView(self.cid, self.gid, c.get("mission_type"))
        try:
            await interaction.edit_original_response(content=f"⏰ {r.message}", embed=e, view=v)
        except Exception:
            pass

        # Re-show the work view on the contract message if it wasn't the one just edited
        if c.get("dm_message_id") and (not interaction.message or interaction.message.id != int(c["dm_message_id"])):
            try:
                ch = interaction.channel or await interaction.client.fetch_channel(interaction.channel_id)
                orig = await ch.fetch_message(int(c["dm_message_id"]))
                await orig.edit(embed=e, view=v)
            except Exception:
                pass


class PayFineButton(DynamicItem[Button], template=r"ct_payfine:" + _ID_PATTERN):
    def __init__(self, contract_id: str, guild_id: int):
        super().__init__(Button(label="💰 Pay Fine", style=discord.ButtonStyle.red,
                                custom_id=_cid("ct_payfine", contract_id, guild_id)))
        self.cid = contract_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        uid, name = _actor(interaction)
        r = await ca.dispute(self.gid, self.cid, actor_id=uid, actor_name=name,
                             action="pay_fine")
        if not r.ok:
            await _reject(interaction, r)
            return
        e = _embed(r.contract, self.gid)
        e.color = discord.Color.dark_red()
        e.set_footer(text=t(self.gid, "ct.fine_paid"))
        await interaction.edit_original_response(embed=e, view=None)


class SueButton(DynamicItem[Button], template=r"ct_sue:" + _ID_PATTERN):
    def __init__(self, contract_id: str, guild_id: int):
        super().__init__(Button(label="⚖️ Sue", style=discord.ButtonStyle.blurple,
                                custom_id=_cid("ct_sue", contract_id, guild_id)))
        self.cid = contract_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        uid, name = _actor(interaction)
        # The service opens the ticket (or falls back to the mod channel) and only
        # moves the contract to mod_review once one of them actually took the case —
        # parking it there with nowhere to see it would strand the dispute.
        r = await ca.dispute(self.gid, self.cid, actor_id=uid, actor_name=name, action="sue")
        if not r.ok:
            await _reject(interaction, r)
            return
        await interaction.edit_original_response(content=t(self.gid, "ct.sued"), view=None)


# ── More Time Approval Buttons ───────────────────────────────────────────────

class MoreTimeApproveButton(DynamicItem[Button], template=r"ct_mt_y:" + _ID_PATTERN):
    def __init__(self, contract_id: str, guild_id: int, new_date: str = ""):
        super().__init__(Button(label="✅ Approve Extension", style=discord.ButtonStyle.green,
                                custom_id=_cid("ct_mt_y", contract_id, guild_id)))
        self.cid = contract_id
        self.gid = int(guild_id)
        self.new_date = new_date

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        # The service takes the date from the request stored on the contract, so
        # normally nothing needs passing. `new_date` here is only the legacy path: a
        # request made before requests were persisted has nothing on the contract, and
        # a bot restart rebuilds this button from its custom_id alone — so scrape the
        # embed as a last resort. A regex, not the last whitespace-token: the line ends
        # "New: **DATE**", and the old split stored the asterisks as part of the date.
        new_date = self.new_date
        if not new_date and interaction.message and interaction.message.embeds:
            found = re.findall(r"\d{4}-\d{2}-\d{2}",
                               interaction.message.embeds[0].description or "")
            new_date = found[-1] if found else ""

        uid, name = _actor(interaction)
        r = await ca.more_time_response(self.gid, self.cid, actor_id=uid, actor_name=name,
                                        approve=True, new_date=new_date)
        if not r.ok:
            await _reject(interaction, r)
            return
        await interaction.edit_original_response(
            content=f"✅ Deadline extended to **{r.data.get('new_date', new_date)}**. "
                    "Contract is active again.",
            embed=None, view=None,
        )
        # Hand the contractor their work view back so they can submit again.
        try:
            contractor = await interaction.client.fetch_user(int(r.contract["contractor_id"]))
            e = _embed(r.contract, self.gid)
            e.color = discord.Color.green()
            await contractor.send(
                embed=e,
                view=ContractWorkView(self.cid, self.gid, r.contract.get("mission_type")))
        except Exception:
            pass


class MoreTimeRefuseButton(DynamicItem[Button], template=r"ct_mt_n:" + _ID_PATTERN):
    def __init__(self, contract_id: str, guild_id: int):
        super().__init__(Button(label="❌ Refuse", style=discord.ButtonStyle.red,
                                custom_id=_cid("ct_mt_n", contract_id, guild_id)))
        self.cid = contract_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        uid, name = _actor(interaction)
        r = await ca.more_time_response(self.gid, self.cid, actor_id=uid, actor_name=name,
                                        approve=False)
        if not r.ok:
            await _reject(interaction, r)
            return
        await interaction.edit_original_response(
            content="❌ Extension refused.", embed=None, view=None,
        )
        try:
            contractor = await interaction.client.fetch_user(int(r.contract["contractor_id"]))
            await contractor.send("❌ Your time extension request was refused.")
        except Exception:
            pass


# ── Settle Approval Buttons ──────────────────────────────────────────────────

class SettleApproveButton(DynamicItem[Button], template=r"ct_stl_y:" + _ID_PATTERN):
    def __init__(self, contract_id: str, guild_id: int):
        super().__init__(Button(label="✅ Accept Settlement", style=discord.ButtonStyle.green,
                                custom_id=_cid("ct_stl_y", contract_id, guild_id)))
        self.cid = contract_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        uid, name = _actor(interaction)
        r = await ca.settle_response(self.gid, self.cid, actor_id=uid, actor_name=name,
                                     approve=True)
        if not r.ok:
            await _reject(interaction, r)
            return
        await interaction.edit_original_response(content=f"✅ {t(self.gid, 'ct.settled')}", embed=None, view=None)


class SettleRefuseButton(DynamicItem[Button], template=r"ct_stl_n:" + _ID_PATTERN):
    def __init__(self, contract_id: str, guild_id: int):
        super().__init__(Button(label="❌ Refuse", style=discord.ButtonStyle.red,
                                custom_id=_cid("ct_stl_n", contract_id, guild_id)))
        self.cid = contract_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        uid, name = _actor(interaction)
        r = await ca.settle_response(self.gid, self.cid, actor_id=uid, actor_name=name,
                                     approve=False)
        if not r.ok:
            await _reject(interaction, r)
            return
        await interaction.edit_original_response(content=f"❌ {t(self.gid, 'ct.settle_refused')}", embed=None, view=None)


# ── Mod Review Buttons ───────────────────────────────────────────────────────

class ModEnforceButton(DynamicItem[Button], template=r"ct_mod_f:" + _ID_PATTERN):
    def __init__(self, contract_id: str, guild_id: int):
        super().__init__(Button(label="✅ Enforce Fine", style=discord.ButtonStyle.green,
                                custom_id=_cid("ct_mod_f", contract_id, guild_id)))
        self.cid = contract_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if not await _require_mod(interaction):
            return
        uid, name = _actor(interaction)
        r = await ca.mod_resolve(self.gid, self.cid, actor_id=uid, actor_name=name,
                                 enforce=True)
        if not r.ok:
            await _reject(interaction, r)
            return
        await interaction.edit_original_response(content=f"✅ {r.message}", view=None)


class ModCancelButton(DynamicItem[Button], template=r"ct_mod_c:" + _ID_PATTERN):
    def __init__(self, contract_id: str, guild_id: int):
        super().__init__(Button(label="❌ Cancel Fine", style=discord.ButtonStyle.red,
                                custom_id=_cid("ct_mod_c", contract_id, guild_id)))
        self.cid = contract_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if not await _require_mod(interaction):
            return
        uid, name = _actor(interaction)
        r = await ca.mod_resolve(self.gid, self.cid, actor_id=uid, actor_name=name,
                                 enforce=False)
        if not r.ok:
            await _reject(interaction, r)
            return
        await interaction.edit_original_response(content=f"❌ {r.message}", view=None)


# ══════════════════════════════════════════════════════════════════════════════
#  View Classes (compose DynamicItem instances)
# ══════════════════════════════════════════════════════════════════════════════

class ContractOfferView(View):
    def __init__(self, contract_id: str = "", guild_id: int = 0):
        super().__init__(timeout=None)
        self.add_item(AcceptOfferButton(contract_id, guild_id))
        self.add_item(RefuseOfferButton(contract_id, guild_id))


class ContractWorkView(View):
    """The contractor's panel on an active contract.

    Submit is drawn only for a flag design — every other kind is submitted from the
    mod's submit window, so offering a button here that can only refuse would be worse
    than not offering one. `mission_type` is passed by callers that already hold the
    contract; when it is not, the view is built without Submit and the button's own
    check is what answers anyone who reaches the deprecated one on an older message."""

    def __init__(self, contract_id: str = "", guild_id: int = 0, mission_type: str | None = None):
        super().__init__(timeout=None)
        self.add_item(GiveUpButton(contract_id, guild_id))
        if mission_type == cdb.FLAG_DESIGN:
            self.add_item(SubmitButton(contract_id, guild_id))


class ContractReviewView(View):
    def __init__(self, contract_id: str = "", guild_id: int = 0):
        super().__init__(timeout=None)
        self.add_item(ReviewAcceptButton(contract_id, guild_id))
        self.add_item(ReviewRefuseButton(contract_id, guild_id))


class DisputeView(View):
    def __init__(self, contract_id: str = "", guild_id: int = 0):
        super().__init__(timeout=None)
        self.add_item(SettleButton(contract_id, guild_id))
        self.add_item(MoreTimeButton(contract_id, guild_id))
        self.add_item(PayFineButton(contract_id, guild_id))
        self.add_item(SueButton(contract_id, guild_id))


class MoreTimeApprovalView(View):
    def __init__(self, contract_id: str = "", guild_id: int = 0, new_date: str = ""):
        super().__init__(timeout=None)
        self.add_item(MoreTimeApproveButton(contract_id, guild_id, new_date))
        self.add_item(MoreTimeRefuseButton(contract_id, guild_id))


class SettleApprovalView(View):
    def __init__(self, contract_id: str = "", guild_id: int = 0):
        super().__init__(timeout=None)
        self.add_item(SettleApproveButton(contract_id, guild_id))
        self.add_item(SettleRefuseButton(contract_id, guild_id))


class ModReviewView(View):
    def __init__(self, contract_id: str = "", guild_id: int = 0):
        super().__init__(timeout=None)
        self.add_item(ModEnforceButton(contract_id, guild_id))
        self.add_item(ModCancelButton(contract_id, guild_id))


# ══════════════════════════════════════════════════════════════════════════════
#  Non-persistent Views (ephemeral, don't need DynamicItem)
# ══════════════════════════════════════════════════════════════════════════════

class FlagSubmitView(View):
    """Pick which of the images uploaded in this channel is the flag being handed over.

    The only submission Discord still runs. It used to serve every contract type,
    which is why it is a file *picker* — a craft submission arrived as a craft file
    plus screenshots. A flag is one image, so the list it offers is images alone."""

    def __init__(self, contract_id: str, guild_id: int, files: list[dict]):
        super().__init__(timeout=120)
        self.cid = contract_id
        self.gid = guild_id
        self.files = files
        self.active_indices = set(range(len(files)))
        self.current_idx = 0

    def _generate_embed(self) -> discord.Embed:
        lines = []
        for i, f in enumerate(self.files):
            status = "✅" if i in self.active_indices else "❌"
            pointer = "▶️" if i == self.current_idx else "  "
            lines.append(f"{pointer} {status} 🖼️ `{f['filename']}`")

        desc = "\n".join(lines)
        return discord.Embed(title="🚩 Select the flag to submit", description=desc,
                             color=discord.Color.blue())

    @button(emoji="⬆️", style=discord.ButtonStyle.grey, row=0)
    async def up_btn(self, interaction: discord.Interaction, btn: Button):
        await interaction.response.defer()
        if self.files:
            self.current_idx = (self.current_idx - 1) % len(self.files)
        await interaction.edit_original_response(embed=self._generate_embed(), view=self)

    @button(emoji="⬇️", style=discord.ButtonStyle.grey, row=0)
    async def down_btn(self, interaction: discord.Interaction, btn: Button):
        await interaction.response.defer()
        if self.files:
            self.current_idx = (self.current_idx + 1) % len(self.files)
        await interaction.edit_original_response(embed=self._generate_embed(), view=self)

    @button(emoji="🔄", label="Toggle Active", style=discord.ButtonStyle.blurple, row=0)
    async def toggle_btn(self, interaction: discord.Interaction, btn: Button):
        await interaction.response.defer()
        if self.current_idx in self.active_indices:
            self.active_indices.remove(self.current_idx)
        else:
            self.active_indices.add(self.current_idx)
        await interaction.edit_original_response(embed=self._generate_embed(), view=self)

    @button(label="✅ Confirm & Send", style=discord.ButtonStyle.green, row=1)
    async def confirm(self, interaction: discord.Interaction, btn: Button):
        await interaction.response.defer()
        c = cdb.get_contract(self.gid, self.cid)
        if not c or c.get("status") != cdb.ACTIVE:
            await interaction.followup.send("❌ Contract already submitted or no longer active.", ephemeral=True)
            return

        selected_files = [f for i, f in enumerate(self.files) if i in self.active_indices]
        if not selected_files:
            await interaction.followup.send("❌ You must select at least one file.", ephemeral=True)
            return

        # The button that opens this view already refuses anything else, and the
        # contract can have changed hands in between.
        if c.get("mission_type") != cdb.FLAG_DESIGN:
            await interaction.followup.send(SUBMIT_MOVED_NOTICE, ephemeral=True)
            return

        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(view=self)

        await self._submit_flag(interaction, c, selected_files)

    async def _submit_flag(self, interaction: discord.Interaction, c: dict, selected_files: list[dict]):
        """Flag-design submission: keep the clean image gated, surface only a
        watermarked preview, and DM the issuer for review. Flag contracts are
        always human-issued, so there's no AI auto-review path."""
        from datetime import datetime
        import flag_preview

        img = next((f for f in selected_files if f["content_type"].startswith("image/")), None)
        if not img:
            await interaction.followup.send("❌ No image found to submit as the flag.", ephemeral=True)
            return

        try:
            raw = await cdb.download_url(img["url"])
        except Exception as exc:
            log.error("Flag download failed: %s", exc)
            await interaction.followup.send("❌ Could not read your uploaded flag. Try again.", ephemeral=True)
            return

        # Full-res stays gated: stored PRIVATE (a bare path), surfaced only through a
        # signed URL once the contract completes (the embed/preview serve points sign
        # it). Only the watermarked preview below is public and shown until accept.
        fullres_url = await cdb.upload_private_to_storage(self.cid, img["filename"], raw,
                                                          img.get("content_type", "image/png"))
        preview_url = await cdb.upload_to_storage(
            self.cid, "flag_preview.png", flag_preview.make_watermarked(raw), "image/png")

        cdb.update_contract(self.gid, self.cid, status=cdb.SUBMITTED,
                            submitted_files=[], flag_filename=img["filename"],
                            flag_fullres_url=fullres_url, flag_preview_url=preview_url,
                            submitted_at=datetime.utcnow().isoformat())
        c = cdb.get_contract(self.gid, self.cid)

        try:
            issuer = await interaction.client.fetch_user(int(c["issuer_id"]))
            e = _embed(c, self.gid)
            e.title = f"📬 {t(self.gid, 'ct.review_title')}"
            e.color = discord.Color.orange()
            e.add_field(
                name="🚩 Flag",
                value="Preview is watermarked; the full-res flag is delivered to your "
                      "in-game flag picker on acceptance.",
                inline=False)
            msg = await issuer.send(embed=e, view=ContractReviewView(self.cid, self.gid))
            cdb.update_contract(self.gid, self.cid, issuer_review_msg_id=str(msg.id))
        except Exception as exc:
            log.error("Could not DM issuer for flag review: %s", exc)

        # Update the designer's contract panel.
        if c.get("dm_message_id"):
            try:
                orig = await interaction.channel.fetch_message(int(c["dm_message_id"]))
                await orig.edit(embed=_embed(c, self.gid), view=None)
            except Exception:
                pass
        await interaction.followup.send("✅ Flag submitted for review!", ephemeral=True)


# ── More Time Modal ──────────────────────────────────────────────────────────

class MoreTimeModal(discord.ui.Modal, title="Extend Deadline"):
    new_date = discord.ui.TextInput(label="New due date (YYYY-MM-DD)", placeholder="2025-06-30")

    def __init__(self, contract_id: str, guild_id: int):
        super().__init__()
        self.cid = contract_id
        self.gid = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        uid, name = _actor(interaction)
        # Format and future-date validation both live in the service now, so the KSP
        # and website paths cannot accept a date this modal would have rejected.
        r = await ca.dispute(self.gid, self.cid, actor_id=uid, actor_name=name,
                             action="more_time", new_date=self.new_date.value)
        if not r.ok:
            await _reject(interaction, r)
            return
        await interaction.followup.send(
            f"⏰ Extension request sent ({r.data.get('new_date', self.new_date.value)}).",
            ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
#  All DynamicItem classes for registration
# ══════════════════════════════════════════════════════════════════════════════

ALL_DYNAMIC_ITEMS = [
    AcceptOfferButton, RefuseOfferButton,
    GiveUpButton, SubmitButton,
    ReviewAcceptButton, ReviewRefuseButton,
    SettleButton, MoreTimeButton, PayFineButton, SueButton,
    MoreTimeApproveButton, MoreTimeRefuseButton,
    SettleApproveButton, SettleRefuseButton,
    ModEnforceButton, ModCancelButton,
]
