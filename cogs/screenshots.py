"""
cogs/screenshots.py – KSP Screenshot Analysis via Gemini AI.

Two modes:
  1. /gk analyze            → auto-finds your most recent image message above
  2. /gk analyze image:...  → directly analyzes uploaded image(s)

Multiple images are processed individually. Already-reviewed messages are skipped.
Only the original poster can analyze via auto-detect mode.
"""

import json
import logging
import os
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from google import genai
from google.genai import types

from i18n import t, tp, S
import settings
from data.store import store
from cost_guard import guard
from cogs import perms

log = logging.getLogger(__name__)

# ── Gemini setup ─────────────────────────────────────────────────────────────

_GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")

if _GEMINI_KEY and _GEMINI_KEY != "YOUR_GEMINI_API_KEY_HERE":
    _client = genai.Client(api_key=_GEMINI_KEY)
    _MODEL = "gemini-3.1-flash-lite"
    log.info("Gemini AI configured (%s)", _MODEL)
else:
    _client = None
    _MODEL = None
    log.warning("GEMINI_API_KEY not set — screenshot analysis disabled")


def active_client():
    """The Gemini client, or None when AI should be skipped.

    Returns None if no key is configured OR the monthly Gemini budget is spent
    (cost_guard soft-degrade). Every Gemini call site goes through this so a
    blown budget transparently falls back to heuristics / "disabled". Import
    and call this instead of reading `_client` directly."""
    if _client is None:
        return None
    return _client if guard.gemini_ok else None


def record_gemini(response) -> None:
    """Meter one Gemini call's token cost (no-op if metadata is absent)."""
    guard.record_gemini(getattr(response, "usage_metadata", None))


# ── System prompt ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert Kerbal Space Program (KSP) and KSP2 screenshot analyst. You are an encyclopedia of every celestial body in KSP modding and stock.

Your job: analyze a screenshot and return ONLY a valid JSON object. No markdown, no commentary, just raw JSON.

## Celestial Bodies Knowledge Base

### Stock Kerbol System
- **Kerbol** (Sun/Star)
- **Moho** — innermost planet, no atmosphere, high gravity relative to size
- **Eve** — purple atmosphere, thick, ocean of Explodium, very hard to return from. Moon: **Gilly** (tiny asteroid)
- **Kerbin** — home planet, blue-green, KSC visible. Moons: **Mun** (grey, craters), **Minmus** (mint green, flats)
- **Duna** — red/rust, thin atmosphere. Moon: **Ike** (grey, tidally locked)
- **Dres** — asteroid belt dwarf planet, grey, canyon
- **Jool** — green gas giant, banded atmosphere. Moons: **Laythe** (ocean world, blue, atmosphere), **Vall** (icy, smooth), **Tylo** (large, no atmosphere, high gravity), **Bop** (tiny, dark, irregular), **Pol** (tiny, yellowish)
- **Eeloo** — distant dwarf planet, icy white/blue

### Outer Planets Mod (OPM)
- **Sarnus** — ringed yellow gas giant. Moons: **Hale**, **Ovok**, **Eeloo** (moved here), **Slate**, **Tekto** (atmosphere, orange)
- **Urlum** — blue-green ice giant, ringed. Moons: **Polta**, **Priax**, **Wal** (+ sub-moon **Tal**)
- **Neidon** — blue ice giant. Moons: **Thatmo** (atmosphere, retrograde), **Nissee**
- **Plock** — distant dwarf. Moon: **Karen**

### Kcalbeloh System (Interstellar mod)
- **Kcalbeloh** — black hole at center, accretion disk
- Orbiting bodies include: **Suluco**, **Yeldo**, **Noyreg**, **Efil**, **Otsol**, **Ambrosh**, many more
- Distinctive visual: black void with bright accretion ring, extreme gravitational lensing

### Real Solar System (RSS)
- Replaces Kerbol system with real solar system: Sun, Mercury, Venus, Earth, Mars, Jupiter, Saturn, Uranus, Neptune, Pluto, plus all real moons
- Earth replaces Kerbin, Moon replaces Mun, etc.

### Other Common Mods
- **Galaxies Unbound** — multiple star systems
- **Beyond Home** — custom star system replacing stock
- **Parallax** — enhanced terrain (visible in surface shots)
- **EVE/Scatterer** — volumetric clouds, atmospheric scattering
- **Restock** — updated part textures

## Difficulty Rating Scale (1-10)
Rate how difficult the DEPICTED mission/achievement would be to accomplish AND RETURN safely:

1. **1** — Launch pad / runway scene, pre-launch
2. **2** — Suborbital flight, basic atmosphere flight
3. **3** — Kerbin orbit achieved
4. **4** — Mun/Minmus flyby or orbit
5. **5** — Mun/Minmus landing and return, interplanetary flyby
6. **6** — Duna/Eve orbit, inner system landings (Moho, Gilly)
7. **7** — Duna landing + return, Jool system operations, Eve orbit + return
8. **8** — Tylo landing, Eve ascent vehicle, large station construction, Jool-5
9. **9** — Grand tour, interstellar travel (Kcalbeloh), full colonization
10. **10** — Completing seemingly impossible feats: Eve SSTO return, full interstellar colonization, grand tour SSTO

## Achievement Levels (1-15)
Assign the highest applicable KSP achievement level from this exact list:
1. Kerbin Orbit
2. Mun Landing
3. Docking (Space Stations)
4. Duna Landing
5. RSS Earth Orbit
6. Eve Landing
7. Asteroid Redirect
8. RSS Moon Landing
9. Jool 5 (Land on all 5 Jool moons)
10. Interstellar Mission
11. RSS Mars
12. RSS Venus Landing
13. RSS Gas Giant
14. Kerbol Grand Tour to all planets at once
15. RSS Interstellar Mission

Set "ksp_level" to the corresponding integer (1-15). If the screenshot does not clearly depict one of these achievements, set it to 0. NOTE: Both crewed and uncrewed (probe) missions count equally for all achievements. A probe landing on Eve qualifies for Eve Landing.

### CRITICAL: Landing vs Orbit — do NOT confuse them
A "**Landing**" achievement (e.g. Mun Landing, Duna Landing, Eve Landing, RSS Moon/Venus Landing) requires the craft to be **physically on the surface** of that body — landing legs/wheels touching the ground, dust/terrain right under the craft, or a planted flag/EVA kerbal standing on the surface. The `situation` must be **landed** or **splashed**.
- A craft **orbiting**, doing a **flyby**, or hanging in **space above** a body does **NOT** qualify for that body's Landing achievement, no matter how close it looks. Being in orbit of the Mun is NOT "Mun Landing". Being in orbit of Duna is NOT "Duna Landing".
- If the craft is only in **orbit / suborbital / flying / in space** (not touching the surface), pick the highest achievement that IS satisfied by an orbit (e.g. "Kerbin Orbit" / "RSS Earth Orbit"), or set ksp_level to **0** if no orbit-level achievement in the list applies to that body.
- When unsure whether the craft is truly landed, treat it as NOT landed and do not award a Landing achievement.

## Crew Detection
- Look for crew portraits in bottom-right corner
- Look for IVA (interior) views showing kerbals
- EVA (spacewalk) scenes show a kerbal outside
- Probe cores = uncrewed

## Required JSON Schema
```json
{
  "approved": true,
  "location": {
    "celestial_body": "Name of planet/moon/star",
    "system": "stock/opm/kcalbeloh/rss/beyond_home/unknown",
    "situation": "prelaunch/launched/flying/suborbital/orbiting/suborbit_reentry/landed/splashed/escaping/docked",
    "biome": "Name if identifiable, else null",
    "altitude_estimate": "low orbit / high orbit / surface / atmosphere / deep space"
  },
  "craft": {
    "crewed": true,
    "crew_count_estimate": 1,
    "craft_type": "rocket/spaceplane/lander/rover/station/satellite/probe/ssto/shuttle/flag/eva/unknown",
    "notable_features": ["description of visible craft elements"]
  },
  "visual_mods": ["list of visual mods detected: EVE, Scatterer, Parallax, Restock, Waterfall, etc."],
  "difficulty_rating": 5,
  "difficulty_reason": "Brief explanation of why this rating",
  "ksp_level": 2,
  "description": "2-3 sentence description of what the screenshot shows",
  "mission_phase": "ascent/transfer/orbit_insertion/landing/surface_ops/return/docking/eva/construction/reentry/recovery"
}
```

## Rules
- If the image is NOT from KSP/KSP2, set `"approved": false` and set `difficulty_rating` to 0 and all other fields to null/empty
- ALWAYS return valid JSON only. No markdown fences, no explanation text
- Be specific about celestial bodies — don't guess randomly
- If unsure about a body, use the visual cues (color, terrain, atmosphere, rings)
- Rate based on the FULL mission difficulty including return, not just getting there
"""


# ── i18n strings ─────────────────────────────────────────────────────────────

S.update({
    "ss.analyzing":       {"en": "🔍 Analyzing screenshot…"},
    "ss.no_image":        {"en": "❌ No image found. Use this command below a screenshot or attach an image."},
    "ss.not_approved":    {"en": "❌ This is not a KSP screenshot."},
    "ss.error":           {"en": "💥 An error occurred during analysis."},
    "ss.no_api_key":      {"en": "❌ Gemini API key not configured."},
    "ss.ai_budget":       {"en": "❌ AI analysis is temporarily disabled (monthly budget reached). Try again next month."},
    "ss.cooldown":        {"en": "⏳ You're analyzing too fast. Try again in {seconds}s."},
    "ss.already_reviewed":{"en": "❌ This screenshot has already been analyzed."},
    "ss.not_yours":       {"en": "❌ You can only analyze screenshots that you posted."},
    "ss.title":           {"en": "🔭 KSP Screenshot Analysis"},
    "ss.location":        {"en": "📍 Location"},
    "ss.situation":       {"en": "🛰️ Situation"},
    "ss.craft":           {"en": "🚀 Craft"},
    "ss.difficulty":      {"en": "⭐ Difficulty"},
    "ss.mods":            {"en": "🎨 Visual Mods"},
    "ss.phase":           {"en": "📋 Mission Phase"},
    "ss.crewed":          {"en": "Crewed"},
    "ss.uncrewed":        {"en": "Uncrewed"},
    "ss.analyzed_by":     {"en": "Analyzed by {name}"},
    "ss.img_counter":     {"en": "📸 Image {n}/{total}"},
    "ss.reward":          {"en": "🎁 Reward"},
})


# ── Reviewed message tracking ───────────────────────────────────────────────
_reviewed_messages: set[int] = set()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _difficulty_bar(rating: int) -> str:
    filled = "🟩" if rating <= 3 else "🟨" if rating <= 6 else "🟧" if rating <= 8 else "🟥"
    return filled * rating + "⬛" * (10 - rating)




def _build_analysis_embed(
    data: dict, guild_id: int, user_name: str, image_url: str | None = None,
) -> discord.Embed:
    """Build a rich embed from the Gemini analysis JSON using server language."""
    loc = data.get("location", {}) or {}
    craft = data.get("craft", {}) or {}
    rating = data.get("difficulty_rating", 0)

    embed = discord.Embed(
        title=t(guild_id, "ss.title"),
        description=data.get("description", ""),
        color=discord.Color.from_rgb(40, 120, 200) if rating < 7 else discord.Color.from_rgb(200, 80, 40),
    )

    body = loc.get("celestial_body", "?")
    system = loc.get("system", "unknown")
    altitude = loc.get("altitude_estimate", "")
    biome = loc.get("biome") or ""
    loc_text = f"**{body}**"
    if system and system != "unknown":
        loc_text += f" ({system})"
    if biome:
        loc_text += f"\n🏔️ {biome}"
    if altitude:
        loc_text += f"\n📏 {altitude}"
    embed.add_field(name=t(guild_id, "ss.location"), value=loc_text, inline=True)

    situation_raw = loc.get("situation", "unknown")
    embed.add_field(name=t(guild_id, "ss.situation"), value=f"**{situation_raw}**", inline=True)

    is_crewed = craft.get("crewed", False)
    crew_label = t(guild_id, "ss.crewed") if is_crewed else t(guild_id, "ss.uncrewed")
    crew_text = f"{'👨‍🚀' if is_crewed else '🤖'} {crew_label}"
    if craft.get("crew_count_estimate"):
        crew_text += f" (×{craft['crew_count_estimate']})"
    craft_type_raw = craft.get("craft_type", "unknown")
    features = craft.get("notable_features", [])
    craft_text = f"{crew_text}\n🔧 **{craft_type_raw}**"
    if features:
        craft_text += "\n" + ", ".join(features[:3])
    embed.add_field(name=t(guild_id, "ss.craft"), value=craft_text, inline=True)

    bar = _difficulty_bar(rating)
    reason = data.get("difficulty_reason", "")
    embed.add_field(
        name=t(guild_id, "ss.difficulty"),
        value=f"{bar} **{rating}/10**\n{reason}",
        inline=False,
    )

    mods = data.get("visual_mods", [])
    if mods:
        embed.add_field(name=t(guild_id, "ss.mods"), value=", ".join(mods), inline=True)

    phase_raw = data.get("mission_phase", "")
    if phase_raw:
        embed.add_field(name=t(guild_id, "ss.phase"), value=f"**{phase_raw}**", inline=True)

    # Rewards
    xp_reward = rating * settings.SCREENSHOT_XP_PER_DIFFICULTY
    coin_reward = rating * settings.SCREENSHOT_COINS_PER_DIFFICULTY
    currency = settings.CURRENCY_SYMBOL
    embed.add_field(
        name=t(guild_id, "ss.reward"),
        value=f"**+{xp_reward} XP**  ·  **+{coin_reward}** {currency}",
        inline=False,
    )

    if image_url:
        embed.set_image(url=image_url)

    embed.set_footer(text=t(guild_id, "ss.analyzed_by", name=user_name))
    return embed


async def _extract_all_images(msg: discord.Message) -> list[tuple[str, bytes]]:
    """Extract ALL images from a message."""
    images = []
    for att in msg.attachments:
        if att.content_type and att.content_type.startswith("image/"):
            data = await att.read()
            images.append((att.url, data))

    if not images:
        for emb in msg.embeds:
            url = None
            if emb.image and emb.image.url:
                url = emb.image.url
            elif emb.thumbnail and emb.thumbnail.url:
                url = emb.thumbnail.url
            if url:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            images.append((url, await resp.read()))
    return images


async def _run_gemini(image_list: list[bytes], guild_id: int | None = None) -> dict:
    """Send one or more images to Gemini in a single call and parse the JSON response."""
    lang_instruction = "ALL text fields (description, difficulty_reason, notable_features) must be written in English."

    multi_note = ""
    if len(image_list) > 1:
        multi_note = (
            "\n\n## IMPORTANT: Multiple Images\n"
            f"You are receiving {len(image_list)} screenshots from the SAME mission. "
            "Analyze them together as a single mission and return ONE JSON object. "
            "Base the difficulty_rating on the overall mission difficulty."
        )

    prompt = SYSTEM_PROMPT + multi_note + "\n\n## CRITICAL: Response Language\n" + lang_instruction

    parts = [types.Part.from_text(text=prompt)]
    for img_bytes in image_list:
        parts.append(types.Part.from_bytes(data=img_bytes, mime_type="image/png"))

    client = active_client()
    if client is None:
        raise RuntimeError("Gemini unavailable (no key or monthly budget reached)")

    response = client.models.generate_content(
        model=_MODEL,
        contents=[types.Content(role="user", parts=parts)],
        config=types.GenerateContentConfig(
            temperature=0.2,
            max_output_tokens=2048,
        ),
    )
    record_gemini(response)

    raw_text = response.text.strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.split("\n", 1)[1] if "\n" in raw_text else raw_text[3:]
    if raw_text.endswith("```"):
        raw_text = raw_text[:-3]
    raw_text = raw_text.strip()

    return json.loads(raw_text)


async def _grant_rewards(gid: int, uid: int, rating: int) -> tuple[int, int]:
    """Grant XP + KCoins based on difficulty. Returns (xp_awarded, coins_awarded)."""
    xp_reward = rating * settings.SCREENSHOT_XP_PER_DIFFICULTY
    coin_reward = rating * settings.SCREENSHOT_COINS_PER_DIFFICULTY
    if xp_reward > 0:
        await store.set_xp(gid, uid, store.get_user(gid, uid)["xp"] + xp_reward)
    if coin_reward > 0:
        await store.add_balance(gid, uid, coin_reward)
    return xp_reward, coin_reward


# ── Cog ──────────────────────────────────────────────────────────────────────

class Screenshots(commands.Cog, name="Screenshots"):
    """KSP screenshot analysis via Gemini AI."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="analyze",
        description="Analyze a KSP screenshot (attach images or auto-detect above)",
    )
    # Each analysis is a paid Gemini call against the shared monthly budget, so
    # cap it per-user (keyed on guild+user) to stop one player draining the budget
    # for the whole community. A blown cooldown raises CommandOnCooldown, handled
    # below in cog_app_command_error before the command body (and any defer) runs.
    @app_commands.checks.cooldown(
        settings.SCREENSHOT_RATELIMIT_RATE,
        settings.SCREENSHOT_RATELIMIT_PER,
        key=lambda i: (i.guild_id, i.user.id),
    )
    @app_commands.describe(
        image1="Screenshot to analyze",
        image2="Second screenshot (optional)",
        image3="Third screenshot (optional)",
    )
    async def analyze(
        self,
        interaction: discord.Interaction,
        image1: discord.Attachment | None = None,
        image2: discord.Attachment | None = None,
        image3: discord.Attachment | None = None,
    ) -> None:
        gid = interaction.guild_id
        uid = interaction.user.id

        if await perms.block_if_mod_only(interaction):
            return

        if active_client() is None:
            # Distinguish "no key configured" from "budget reached" (soft degrade).
            msg = "ss.no_api_key" if _client is None else "ss.ai_budget"
            await interaction.response.send_message(
                tp(gid, uid, msg), ephemeral=True
            )
            return

        # Defer IMMEDIATELY — prevents interaction timeout
        await interaction.response.defer()

        # ── Mode 1: Direct image(s) uploaded with the command ────────────────
        direct = [a for a in (image1, image2, image3)
                  if a and a.content_type and a.content_type.startswith("image/")]

        if direct:
            all_bytes = [await a.read() for a in direct]
            first_url = direct[0].url

            try:
                data = await _run_gemini(all_bytes, gid)
            except Exception as exc:
                log.error("Gemini error on direct upload: %s", exc, exc_info=True)
                await interaction.followup.send(tp(gid, uid, "ss.error"), ephemeral=True)
                return

            if not data.get("approved", False):
                embed = discord.Embed(
                    title=t(gid, "ss.not_approved"),
                    description=data.get("description", ""),
                    color=discord.Color.red(),
                )
                embed.set_thumbnail(url=first_url)
                await interaction.followup.send(embed=embed)
                return

            embed = _build_analysis_embed(data, gid, interaction.user.display_name, first_url)
            rating = data.get("difficulty_rating", 0)
            xp_r, coin_r = await _grant_rewards(gid, uid, rating)

            # Note: level/title ROLES are no longer awarded here. Roles are earned
            # only through the verified in-game capture (POST /api/v1/achievement-photo).
            # This command still grants XP + KCoins for the analysis.

            await interaction.followup.send(embed=embed)
            log.info("%s analyzed %d direct upload(s) — difficulty %d (+%d XP, +%d coins)",
                     interaction.user, len(direct), rating, xp_r, coin_r)
            return

        # ── Mode 2: Auto-detect the most recent image message above ──────────
        target_msg = None
        async for msg in interaction.channel.history(limit=10, before=interaction.created_at):
            if msg.author.bot:
                continue
            has_image = any(
                a.content_type and a.content_type.startswith("image/")
                for a in msg.attachments
            )
            if not has_image:
                has_image = any(
                    (e.image and e.image.url) or (e.thumbnail and e.thumbnail.url)
                    for e in msg.embeds
                )
            if has_image:
                target_msg = msg
                break

        if target_msg is None:
            await interaction.followup.send(tp(gid, uid, "ss.no_image"), ephemeral=True)
            return

        if target_msg.author.id != uid:
            await interaction.followup.send(tp(gid, uid, "ss.not_yours"), ephemeral=True)
            return

        if target_msg.id in _reviewed_messages:
            await interaction.followup.send(tp(gid, uid, "ss.already_reviewed"), ephemeral=True)
            return

        images = await _extract_all_images(target_msg)
        if not images:
            await interaction.followup.send(tp(gid, uid, "ss.no_image"), ephemeral=True)
            return

        _reviewed_messages.add(target_msg.id)

        # Send ALL images in one Gemini call
        all_bytes = [img_bytes for _, img_bytes in images]
        first_url = images[0][0]

        try:
            data = await _run_gemini(all_bytes, gid)
        except json.JSONDecodeError as exc:
            log.error("Gemini returned invalid JSON: %s", exc)
            await interaction.followup.send(tp(gid, uid, "ss.error"), ephemeral=True)
            return
        except Exception as exc:
            log.error("Screenshot analysis error: %s", exc, exc_info=True)
            await interaction.followup.send(tp(gid, uid, "ss.error"), ephemeral=True)
            return

        if not data.get("approved", False):
            embed = discord.Embed(
                title=t(gid, "ss.not_approved"),
                description=data.get("description", ""),
                color=discord.Color.red(),
            )
            if first_url:
                embed.set_thumbnail(url=first_url)
            await target_msg.reply(embed=embed, mention_author=False)
            await interaction.followup.send("✅", ephemeral=True)
            return

        embed = _build_analysis_embed(data, gid, interaction.user.display_name, first_url)
        rating = data.get("difficulty_rating", 0)
        xp_r, coin_r = await _grant_rewards(gid, uid, rating)

        # Roles are earned only via the in-game capture flow now (see above); this
        # command grants XP + KCoins only.

        await target_msg.reply(embed=embed, mention_author=False)
        await interaction.followup.send("✅", ephemeral=True)

        log.info(
            "%s analyzed screenshot (msg %d, %d imgs): %s @ %s — difficulty %d (+%d XP, +%d coins)",
            interaction.user, target_msg.id, len(images),
            data.get("location", {}).get("celestial_body", "?"),
            data.get("location", {}).get("situation", "?"),
            rating, xp_r, coin_r
        )

    # ── Error handler ─────────────────────────────────────────────────────────
    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        # Cooldown isn't an error — tell the user how long to wait, no traceback.
        if isinstance(error, app_commands.CommandOnCooldown):
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    tp(interaction.guild_id, interaction.user.id, "ss.cooldown",
                       seconds=round(error.retry_after)),
                    ephemeral=True,
                )
            return
        log.error("Screenshots cog error: %s", error, exc_info=True)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                tp(interaction.guild_id, interaction.user.id, "common.error"),
                ephemeral=True,
            )


async def setup(bot: commands.Bot) -> None:
    # Skipping add_cog keeps /analyze out of the command tree entirely (hidden,
    # not just refused). The module still loads, so api_server's imports of
    # active_client/_run_gemini/_grant_rewards keep working for the in-game
    # achievement-photo path.
    if not settings.SCREENSHOT_ANALYSIS_ENABLED:
        log.info("Screenshot analysis disabled (settings.SCREENSHOT_ANALYSIS_ENABLED) — /analyze not registered")
        return
    await bot.add_cog(Screenshots(bot))
