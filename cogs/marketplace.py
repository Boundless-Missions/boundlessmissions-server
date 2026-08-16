"""
cogs/marketplace.py – Craft marketplace.

Players list crafts for sale from the KSP mod (see api_server.py
/api/v1/marketplace/list). Each listing is posted to MARKETPLACE_CHANNEL_ID as an
embed with a Buy button. Clicking Buy transfers KCoins from buyer to seller (seller
gets the full price) and DMs the buyer the .craft blueprint. Listings are
non-exclusive — they stay active after a sale so anyone can buy a copy.

Buttons use DynamicItem with regex-matched custom_ids so they survive bot restarts.
custom_id format: "prefix:listing_id:guild_id"
"""
import io
import logging

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import View, Button, DynamicItem

import settings
from data.store import store
from data import marketplace as mkt
from data import imports as imp
from data.contracts import download_url
from data import guild_config

log = logging.getLogger(__name__)

# listing_ids are 12-char hex, guild_ids are snowflakes
_ID_PATTERN = r"(?P<lid>[^:]+):(?P<gid>\d+)"


def _cid(prefix: str, listing_id: str, guild_id: int) -> str:
    return f"{prefix}:{listing_id}:{guild_id}"


_LS_NAMES = {
    "usi": "USI-LS", "tac": "TAC-LS", "snacks": "Snacks", "kerbalism": "Kerbalism",
}


def life_support_label(life_support: str, endurance_days: float, crew_capacity: int) -> str | None:
    """Human-readable life-support flag for a craft, or None when stock/empty.

    Endurance is sent per-kerbal, so the range is "longest at 1 kerbal, shortest at a
    full crew": e.g. "USI-LS · ~180 d solo · ~45 d for full crew of 4"."""
    key = (life_support or "none").lower()
    if key == "none" or key not in _LS_NAMES:
        return None
    name = _LS_NAMES[key]
    d = float(endurance_days or 0.0)
    cap = int(crew_capacity or 0)
    if d <= 0:
        return f"{name} · endurance n/a"
    if cap >= 2:
        return f"{name} · ~{d:.0f} d solo · ~{d / cap:.0f} d for full crew of {cap}"
    return f"{name} · ~{d:.0f} d / kerbal"


def listing_embed(listing: dict) -> discord.Embed:
    """Build the marketplace channel embed for a listing."""
    sym = settings.CURRENCY_SYMBOL
    delisted = listing.get("status") != mkt.ACTIVE
    e = discord.Embed(
        title=f"🛒 {listing['craft_name']}",
        description=("~~This craft is no longer for sale.~~" if delisted
                     else "Click **Buy** to purchase this craft. The blueprint will be sent to your DMs."),
        color=discord.Color.dark_grey() if delisted else discord.Color.blurple(),
    )
    e.add_field(name="Price", value=f"**{listing['price']:,}** {sym}", inline=True)
    e.add_field(name="Editor", value=listing.get("craft_type", "N/A"), inline=True)
    e.add_field(name="Parts", value=f"{listing.get('part_count', 0)}", inline=True)
    e.add_field(name="Mass", value=f"{listing.get('mass', 0):.1f} t", inline=True)
    e.add_field(name="Cost", value=f"{listing.get('cost', 0):,.0f}", inline=True)
    e.add_field(name="Seller", value=listing.get("seller_name", "N/A"), inline=True)
    ls = life_support_label(
        listing.get("life_support", "none"),
        listing.get("ls_endurance_days", 0.0),
        listing.get("ls_crew_capacity", 0),
    )
    if ls:
        e.add_field(name="🥫 Life Support", value=ls, inline=False)
    # The rendered blueprint is always shown publicly; the .craft file itself is
    # only delivered to the buyer's DMs after purchase.
    if listing.get("blueprint_url"):
        e.set_image(url=listing["blueprint_url"])
    e.set_footer(text=f"Listing {listing['listing_id']} · {listing.get('sales_count', 0)} sold")
    return e


# ══════════════════════════════════════════════════════════════════════════════
#  DynamicItem Buttons
# ══════════════════════════════════════════════════════════════════════════════

class BuyButton(DynamicItem[Button], template=r"mk_buy:" + _ID_PATTERN):
    def __init__(self, listing_id: str, guild_id: int):
        super().__init__(Button(label="🛒 Buy", style=discord.ButtonStyle.green,
                                custom_id=_cid("mk_buy", listing_id, guild_id)))
        self.lid = listing_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["lid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        gid, lid = self.gid, self.lid
        buyer_id = interaction.user.id

        listing = mkt.get_listing(gid, lid)
        if not listing or listing.get("status") != mkt.ACTIVE:
            await interaction.followup.send("❌ This craft is no longer for sale.", ephemeral=True)
            return

        seller_id = int(listing["seller_id"])
        if buyer_id == seller_id:
            await interaction.followup.send("❌ You can't buy your own listing.", ephemeral=True)
            return

        price = int(listing["price"])
        already_owned = str(buyer_id) in listing.get("buyers", [])

        # Repeat buyers get a free re-delivery — nothing is charged, so skip the
        # confirmation and re-send straight away.
        if already_owned:
            await _execute_purchase(interaction, gid, listing)
            return

        # First-time buyers see a confirmation showing the price and their balance
        # before/after, and must press Confirm before any coins move.
        balance = store.get_user(gid, buyer_id)["balance"]
        if balance < price:
            await interaction.followup.send(
                f"❌ You need **{price:,}** {settings.CURRENCY_SYMBOL} but only have "
                f"**{balance:,}**.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            embed=confirm_purchase_embed(listing, balance, price),
            view=ConfirmPurchaseView(lid, gid, buyer_id),
            ephemeral=True,
        )


def confirm_purchase_embed(listing: dict, balance_before: int, price: int) -> discord.Embed:
    """Pre-purchase confirmation: price plus the buyer's balance before and after."""
    sym = settings.CURRENCY_SYMBOL
    balance_after = balance_before - price
    e = discord.Embed(
        title="🛒 Confirm purchase",
        description=f"You're about to buy **{listing['craft_name']}**.",
        color=discord.Color.gold(),
    )
    e.add_field(name="Price", value=f"−{price:,} {sym}", inline=False)
    e.add_field(name="Balance now", value=f"{balance_before:,} {sym}", inline=True)
    e.add_field(name="Balance after", value=f"**{balance_after:,}** {sym}", inline=True)
    e.set_footer(text="Confirm within 60 seconds.")
    return e


class ConfirmPurchaseView(View):
    """Transient ephemeral confirmation shown before a first-time purchase. Not a
    persistent DynamicItem — it only needs to live for its 60s timeout, and the
    purchase is re-validated on confirm so a stale click can't double-charge."""

    def __init__(self, listing_id: str, guild_id: int, buyer_id: int):
        super().__init__(timeout=60)
        self.lid = listing_id
        self.gid = int(guild_id)
        self.buyer_id = int(buyer_id)

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.buyer_id:
            await interaction.response.send_message("❌ This isn't your purchase.", ephemeral=True)
            return
        listing = mkt.get_listing(self.gid, self.lid)
        if not listing or listing.get("status") != mkt.ACTIVE:
            await interaction.response.edit_message(
                content="❌ This craft is no longer for sale.", embed=None, view=None)
            return
        await interaction.response.edit_message(
            content="⏳ Processing your purchase…", embed=None, view=None)
        await _execute_purchase(interaction, self.gid, listing)

    @discord.ui.button(label="✖️ Cancel", style=discord.ButtonStyle.grey)
    async def cancel(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.buyer_id:
            await interaction.response.send_message("❌ This isn't your purchase.", ephemeral=True)
            return
        await interaction.response.edit_message(
            content="Purchase cancelled.", embed=None, view=None)


async def _execute_purchase(interaction: discord.Interaction, gid: int, listing: dict) -> None:
    """Charge a first-time buyer, deliver the blueprint via DM, and notify the seller.
    The interaction must already be deferred or responded to (so followup works).
    Reused by the confirm button and the free re-delivery path for repeat buyers."""
    lid = listing["listing_id"]
    buyer_id = interaction.user.id
    seller_id = int(listing["seller_id"])
    price = int(listing["price"])
    already_owned = str(buyer_id) in listing.get("buyers", [])

    # Charge only first-time buyers; repeat buyers get a free re-delivery.
    if not already_owned:
        # Atomic check-and-deduct so a double-click / concurrent buy can't pay
        # for the craft twice from the same balance (or overdraw via the clamp).
        if not await store.try_debit(gid, buyer_id, price):
            buyer = store.get_user(gid, buyer_id)
            await interaction.followup.send(
                f"❌ You need **{price:,}** {settings.CURRENCY_SYMBOL} but only have "
                f"**{buyer['balance']:,}**.",
                ephemeral=True,
            )
            return
        await store.add_balance(gid, seller_id, price)

    # Download the blueprint and DM it to the buyer.
    try:
        data = await download_url(listing["craft_url"])
    except Exception as exc:
        log.error("Failed to download craft for listing %s: %s", lid, exc)
        if not already_owned:  # refund — we charged but can't deliver
            await store.add_balance(gid, buyer_id, price)
            await store.add_balance(gid, seller_id, -price)
        await interaction.followup.send("❌ Could not fetch the craft file. You were not charged.", ephemeral=True)
        return

    craft_file = discord.File(io.BytesIO(data), filename=listing["craft_filename"])
    try:
        await interaction.user.send(
            content=(f"🛒 Here is **{listing['craft_name']}** that you purchased.\n"
                     f"Place the `.craft` file in your KSP `Ships/{listing.get('craft_type', 'VAB')}/` "
                     f"folder, or hit **Load to KSP** to auto-import it at the Space Center."),
            file=craft_file,
            view=DMImportView(lid, gid),
        )
    except discord.Forbidden:
        if not already_owned:  # refund — DMs closed, no delivery
            await store.add_balance(gid, buyer_id, price)
            await store.add_balance(gid, seller_id, -price)
        await interaction.followup.send(
            "❌ I couldn't DM you. Enable **Direct Messages** from server members and try again. "
            "You were not charged.",
            ephemeral=True,
        )
        return

    if already_owned:
        await interaction.followup.send("✅ Re-sent the blueprint to your DMs (free, since you already own it).", ephemeral=True)
        return

    # Record the sale and refresh every mirrored embed (sales count).
    mkt.record_purchase(gid, lid, buyer_id)
    listing = mkt.get_listing(gid, lid)
    await edit_all_mirrors(interaction.client, listing)

    new_balance = store.get_user(gid, buyer_id)["balance"]

    # Best-effort heads-up when the buyer's install can't load what they just bought.
    # Imported lazily: api_server imports this module too, so a module-level import
    # would be circular. Never allowed to break a completed purchase.
    compat_note = ""
    try:
        from api_server import _craft_compatibility
        compat = _craft_compatibility(gid, buyer_id, listing)
        if compat.known and not compat.compatible:
            blocking = [p for p in compat.missing_parts if p not in compat.substitutable_parts]
            shown = ", ".join(f"`{p}`" for p in blocking[:5])
            more = f" (+{len(blocking) - 5} more)" if len(blocking) > 5 else ""
            compat_note = (f"\n\n⚠️ Heads-up: your install is missing {len(blocking)} part(s) "
                           f"this craft uses — {shown}{more}. It won't load until you install "
                           "what provides them.")
    except Exception as exc:
        log.warning("Marketplace: compatibility check failed for %s: %s", lid, exc)

    await interaction.followup.send(
        f"✅ Purchased **{listing['craft_name']}** for **{price:,}** {settings.CURRENCY_SYMBOL}. "
        f"Your balance is now **{new_balance:,}** {settings.CURRENCY_SYMBOL}. "
        "Check your DMs for the blueprint!" + compat_note,
        ephemeral=True,
    )

    # Notify the seller.
    try:
        seller = await interaction.client.fetch_user(seller_id)
        await seller.send(
            f"💰 **{interaction.user.display_name}** bought your craft **{listing['craft_name']}** "
            f"for **{price:,}** {settings.CURRENCY_SYMBOL}."
        )
    except (discord.Forbidden, discord.HTTPException):
        pass
    log.info("Marketplace: %s bought listing %s for %d", interaction.user, lid, price)


class LoadToKspButton(DynamicItem[Button], template=r"mk_load:" + _ID_PATTERN):
    """Sent in the purchase DM — queues the bought craft for KSP auto-import."""
    def __init__(self, listing_id: str, guild_id: int):
        super().__init__(Button(label="🚀 Load to KSP", style=discord.ButtonStyle.blurple,
                                custom_id=_cid("mk_load", listing_id, guild_id)))
        self.lid = listing_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["lid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        listing = mkt.get_listing(self.gid, self.lid)
        if not listing:
            await interaction.followup.send("❌ This craft is no longer available.", ephemeral=True)
            return
        imp.enqueue(
            self.gid, interaction.user.id, "market", self.lid, listing["craft_name"],
            craft_url=listing["craft_url"], craft_filename=listing["craft_filename"],
        )
        await interaction.followup.send(
            "✅ Queued! Open KSP and it'll auto-import at the Space Center.", ephemeral=True)
        log.info("Marketplace: %s queued listing %s for KSP import", interaction.user, self.lid)


class DMImportView(View):
    """Attached to the purchase DM so the buyer can one-click load into KSP."""
    def __init__(self, listing_id: str, guild_id: int):
        super().__init__(timeout=None)
        self.add_item(LoadToKspButton(listing_id, guild_id))


class DelistButton(DynamicItem[Button], template=r"mk_delist:" + _ID_PATTERN):
    def __init__(self, listing_id: str, guild_id: int):
        super().__init__(Button(label="🗑️ Delist", style=discord.ButtonStyle.grey,
                                custom_id=_cid("mk_delist", listing_id, guild_id)))
        self.lid = listing_id
        self.gid = int(guild_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["lid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        listing = mkt.get_listing(self.gid, self.lid)
        if not listing:
            await interaction.followup.send("❌ Listing not found.", ephemeral=True)
            return

        if interaction.user.id != int(listing["seller_id"]):
            await interaction.followup.send("❌ Only the seller can delist this craft.", ephemeral=True)
            return

        if listing.get("status") == mkt.ACTIVE:
            mkt.update_listing(self.gid, self.lid, status=mkt.DELISTED)
            listing["status"] = mkt.DELISTED
        await edit_all_mirrors(interaction.client, listing)
        await interaction.followup.send("✅ Craft delisted.", ephemeral=True)
        log.info("Marketplace: %s delisted listing %s", interaction.user, self.lid)


class ListingView(View):
    """Persistent view attached to each listing message."""
    def __init__(self, listing_id: str, guild_id: int):
        super().__init__(timeout=None)
        self.add_item(BuyButton(listing_id, guild_id))
        self.add_item(DelistButton(listing_id, guild_id))


MARKETPLACE_DYNAMIC_ITEMS = [BuyButton, DelistButton, LoadToKspButton]


async def post_listing(bot: commands.Bot, listing: dict) -> list[dict]:
    """Mirror a listing into EVERY server that has a marketplace channel configured,
    recording each mirror so status edits can fan out. Returns the mirrors list."""
    lid = listing["listing_id"]
    mirrors: list[dict] = []
    for guild in bot.guilds:
        channel = guild_config.resolve_channel(bot, guild.id, "marketplace")
        if channel is None:
            continue
        try:
            msg = await channel.send(embed=listing_embed(listing), view=ListingView(lid, guild.id))
            mirrors.append({"guild_id": str(guild.id), "channel_id": str(channel.id),
                            "message_id": str(msg.id)})
        except discord.Forbidden:
            log.warning("No permission to post marketplace listing in guild %s", guild.id)
        except Exception as exc:
            log.warning("Failed to mirror listing %s into guild %s: %s", lid, guild.id, exc)
    if mirrors:
        mkt.update_listing(0, lid, mirrors=mirrors)
    else:
        log.warning("Listing %s created but no server has a marketplace channel set", lid)
    return mirrors


async def backfill_guild(bot: commands.Bot, guild_id: int) -> int:
    """Mirror every active listing into `guild_id`'s marketplace channel — used when
    a server configures its marketplace channel after listings already exist.
    Idempotent: skips listings already mirrored in this guild. Returns count posted."""
    channel = guild_config.resolve_channel(bot, guild_id, "marketplace")
    if channel is None:
        return 0
    posted = 0
    for listing in mkt.list_active(0):
        lid = listing["listing_id"]
        mirrors = listing.get("mirrors", []) or []
        if any(str(m.get("guild_id")) == str(guild_id) for m in mirrors):
            continue
        try:
            msg = await channel.send(embed=listing_embed(listing), view=ListingView(lid, guild_id))
        except Exception as exc:
            log.warning("Backfill: could not mirror listing %s into guild %s: %s", lid, guild_id, exc)
            continue
        mirrors.append({"guild_id": str(guild_id), "channel_id": str(channel.id),
                        "message_id": str(msg.id)})
        mkt.update_listing(0, lid, mirrors=mirrors)
        posted += 1
    if posted:
        log.info("Backfilled %d marketplace listings into guild %s", posted, guild_id)
    return posted


async def delete_all_mirrors(bot: commands.Bot, listing: dict) -> None:
    """Delete every mirrored marketplace message for a listing across servers — used
    when the seller permanently deletes a listing (vs. just delisting it)."""
    for m in listing.get("mirrors", []) or []:
        ch = bot.get_channel(int(m["channel_id"]))
        if ch is None:
            continue
        try:
            msg = await ch.fetch_message(int(m["message_id"]))
            await msg.delete()
        except (discord.NotFound, discord.HTTPException):
            pass


async def edit_all_mirrors(bot: commands.Bot, listing: dict) -> None:
    """Refresh every mirrored message for a listing (e.g. after a sale or delist).
    Drops the Buy/Delist view once the listing is no longer active."""
    lid = listing["listing_id"]
    active = listing.get("status") == mkt.ACTIVE
    for m in listing.get("mirrors", []) or []:
        ch = bot.get_channel(int(m["channel_id"]))
        if ch is None:
            continue
        try:
            msg = await ch.fetch_message(int(m["message_id"]))
            view = ListingView(lid, int(m["guild_id"])) if active else None
            await msg.edit(embed=listing_embed(listing), view=view)
        except (discord.NotFound, discord.HTTPException):
            pass


# ══════════════════════════════════════════════════════════════════════════════
#  Cog
# ══════════════════════════════════════════════════════════════════════════════

class Marketplace(commands.Cog, name="Marketplace"):
    """Craft marketplace commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="market", description="Browse crafts for sale")
    async def market(self, interaction: discord.Interaction) -> None:
        gid = interaction.guild_id
        listings = sorted(mkt.list_active(gid), key=lambda l: l.get("created_at", ""), reverse=True)
        if not listings:
            await interaction.response.send_message("🛒 No crafts are for sale right now.", ephemeral=True)
            return

        sym = settings.CURRENCY_SYMBOL
        lines = [
            f"**{l['craft_name']}** · {l['price']:,} {sym} · {l.get('part_count', 0)} parts "
            f"· by {l.get('seller_name', 'N/A')} · `{l['listing_id']}`"
            for l in listings[:25]
        ]
        embed = discord.Embed(
            title="🛒 Craft Marketplace",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        if guild_config.get_channel_id(gid, "marketplace"):
            embed.set_footer(text="Buy crafts from the marketplace channel.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="delist", description="Remove one of your craft listings")
    @app_commands.describe(listing_id="The listing ID (shown on the listing and in /market)")
    async def delist(self, interaction: discord.Interaction, listing_id: str) -> None:
        gid = interaction.guild_id
        listing = mkt.get_listing(gid, listing_id)
        if not listing:
            await interaction.response.send_message("❌ Listing not found.", ephemeral=True)
            return
        if interaction.user.id != int(listing["seller_id"]):
            await interaction.response.send_message("❌ Only the seller can delist this craft.", ephemeral=True)
            return

        if listing.get("status") == mkt.ACTIVE:
            mkt.update_listing(gid, listing_id, status=mkt.DELISTED)
            listing["status"] = mkt.DELISTED

        # Update every mirrored message across servers.
        await edit_all_mirrors(self.bot, listing)
        await interaction.response.send_message("✅ Craft delisted.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Marketplace(bot))
    bot.add_dynamic_items(*MARKETPLACE_DYNAMIC_ITEMS)
