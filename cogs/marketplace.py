"""
cogs/marketplace.py – tombstone for the retired Discord craft marketplace.

The marketplace itself is very much alive; it just does not live here any more. It
runs on the website (browse, buy, vote, report) and in the KSP mod's Market panel
(listing a craft, which is the half a browser cannot do — it has to read the ship on
the build stage). What Discord used to add on top was a mirror of every listing into
every server's marketplace channel, with Buy / Delist / Load-to-KSP buttons and the
`/market` and `/delist` commands. All of that is gone: it duplicated the website's
grid with none of its filtering, and a listing's state had to be fanned out to every
mirrored message on every sale, delist, relist, price edit and deletion.

What is left is this file, and only because deleting it outright would break silently
rather than loudly. Every listing message ever mirrored is still sitting in some
server's channel with its buttons attached, and Discord answers a button whose
custom_id nothing claims with a bare "This interaction failed" — which reads as *the
marketplace is broken*, not as *the marketplace moved*. So the three old custom_ids
are still claimed here, and all they do is say where it went.

No cog, no commands, no listing posts: `setup` registers the tombstone buttons and
nothing else.
"""
import logging

import discord
from discord.ext import commands
from discord.ui import Button, DynamicItem

import settings

log = logging.getLogger(__name__)

# listing_ids are 12-char hex, guild_ids are snowflakes — unchanged, so the ids on
# messages posted by the old cog still match.
_ID_PATTERN = r"(?P<lid>[^:]+):(?P<gid>\d+)"

MOVED_NOTICE = (
    "🛒 The craft marketplace has moved to the website — browse, buy and manage your "
    f"listings at {settings.MARKETPLACE_WEB_URL}. Purchases land in your KSP import "
    "queue automatically, and you list a craft from the mod's **Market** panel with "
    "the ship open in the VAB or SPH.\n\n"
    "This message is an old listing mirror and no longer does anything."
)


class _Retired:
    """Answers one of the retired marketplace custom_ids with the notice above.

    A mixin rather than a base class: `DynamicItem` compiles its `template` in
    `__init_subclass__`, so every concrete subclass has to name its own pattern and
    an intermediate one would have nothing to give it."""

    _prefix: str = ""

    def __init__(self, listing_id: str, guild_id: int):
        super().__init__(Button(label="Moved to the website",
                                style=discord.ButtonStyle.grey,
                                custom_id=f"{self._prefix}:{listing_id}:{guild_id}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["lid"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(MOVED_NOTICE, ephemeral=True)


class RetiredBuyButton(_Retired, DynamicItem[Button], template=r"mk_buy:" + _ID_PATTERN):
    _prefix = "mk_buy"


class RetiredDelistButton(_Retired, DynamicItem[Button], template=r"mk_delist:" + _ID_PATTERN):
    _prefix = "mk_delist"


class RetiredLoadButton(_Retired, DynamicItem[Button], template=r"mk_load:" + _ID_PATTERN):
    _prefix = "mk_load"


RETIRED_DYNAMIC_ITEMS = [RetiredBuyButton, RetiredDelistButton, RetiredLoadButton]


async def setup(bot: commands.Bot) -> None:
    bot.add_dynamic_items(*RETIRED_DYNAMIC_ITEMS)
