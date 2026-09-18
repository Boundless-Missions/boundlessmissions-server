"""
cogs/corps.py – Corporation system.

Users can establish a corporation which creates a dedicated text channel.
Each user may own one corporation at a time. Data is persisted in Firestore.

Firestore path: guilds/{guild_id}/corps/{user_id}

**A corp is never created just because someone joined the Discord server.** A corp
channel is a private channel carrying that player's contract traffic, deliveries
and dispute hand-offs — creating one the moment a member arrives spends a channel
on everybody who only ever came to read, and it stores their name and avatar in a
channel topic and a pinned embed before they have agreed to anything. So the
trigger is the in-mod consent gate instead: the KSP client transmits *nothing*
until the player accepts the privacy policy and terms (`Consent.Accepted` /
`ApiClient.TransmissionBlocked`), which makes a completed link the server's proof
that they did. `ensure_corp_for_linked_user` is called from the link endpoints and
is the only automatic path left; `/corpsetup` (explicit) and `/admin corpsgenerate`
(backfill, linked members only) are the others.
"""

import asyncio
import re
import logging
import datetime
import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)

# Import the shared Firestore client
from data.store import _db
import settings
from cogs import perms
from data import guild_config
from i18n import t, tp
# gkchannels is imported lazily at each call site, not bound here: corps loads
# BEFORE cogs.gkchannels, and load_extension re-execs an already-imported module
# into a second object. Binding the functions now would bind them to that dead
# copy, whose _gk_channels cache is empty — so add/remove would read an empty set
# and _persist would overwrite the guild's whole gk_channels list with one entry.
# (cogs/tickets.py and cogs/weeklymissions.py import is_mod the same way.)


def _get_corp_ref(guild_id: int, user_id: int):
    """Get a Firestore document reference for a user's corporation."""
    return (
        _db.collection("guilds")
        .document(str(guild_id))
        .collection("corps")
        .document(str(user_id))
    )


def _corp_overwrites(
    guild: discord.Guild,
    owner: discord.Member | None,
    members: list[discord.Member] = (),
) -> dict:
    """The private-channel permission set: the corp, the mods, the bot, nobody else.

    Contract offers, dispute hand-offs and craft deliveries are posted into corp
    channels (`contract_actions.deliver_to_player`), so `view_channel=False` on
    @everyone is what keeps that traffic between the parties and the moderators.
    Guild administrators bypass overwrites, so an unmapped "mod" role hides the
    channel from mods without the administrator permission — map it with
    `/admin setrole` first.
    """
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    mod_role = guild_config.resolve_role(guild, "mod")
    if mod_role is not None:
        overwrites[mod_role] = discord.PermissionOverwrite(view_channel=True)
    if owner is not None:
        overwrites[owner] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True,
            manage_messages=True, manage_channels=False,
        )
    for m in members:
        if owner is None or m.id != owner.id:
            overwrites[m] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
    return overwrites


async def _create_corp_channel(
    guild: discord.Guild,
    owner: discord.Member,
    name: str,
) -> tuple[discord.TextChannel, discord.Message]:
    """Create the corp text channel (private to the corp + mods) and pin the
    establishment embed."""
    # Sanitise channel name (Discord auto-lowercases and replaces spaces with hyphens)
    cat_id = guild_config.get_channel_id(guild.id, "corp_category")
    category = guild.get_channel(cat_id) if cat_id else None
    channel = await guild.create_text_channel(
        name=name,
        category=category,
        topic=f"🏢 {name} · Founded by {owner.display_name}",
        overwrites=_corp_overwrites(guild, owner),
        reason=f"Corporation established by {owner}",
    )

    now = datetime.datetime.now(datetime.timezone.utc)
    gid = guild.id
    embed = discord.Embed(
        title=t(gid, "corps.setup.title", name=name),
        description=t(gid, "corps.setup.desc"),
        color=discord.Color.gold(),
        timestamp=now,
    )
    embed.add_field(name=t(gid, "corps.setup.founder"), value=owner.mention, inline=True)
    embed.add_field(
        name=t(gid, "corps.setup.established"),
        value=discord.utils.format_dt(now, style="F"),
        inline=True,
    )
    embed.set_thumbnail(url=owner.display_avatar.url)
    embed.set_footer(text=f"Corp ID: {owner.id}")

    msg = await channel.send(embed=embed)
    await msg.pin()

    return channel, msg


async def _establish_corp(
    guild: discord.Guild, member: discord.Member, name: str
) -> discord.TextChannel:
    """Create and persist a corporation: channel + GK registration + Firestore
    record + global ownership pointer. The one implementation behind /corpsetup,
    corp replacement, and auto-generation — callers only decide the name."""
    channel, pin_msg = await _create_corp_channel(guild, member, name)

    # Auto-register as GK channel (lazy import — see the note at the top imports)
    from cogs.gkchannels import add_gk_channel
    add_gk_channel(guild.id, channel.id)

    now = datetime.datetime.now(datetime.timezone.utc)
    _save_corp(guild.id, member.id, {
        "name": name,
        "owner_id": str(member.id),
        # display_name, not name: this is what the corp's owner is called
        # everywhere else (link codes, weekly missions, auction bids), and it is
        # the fallback the player pickers fall back *to* when the member cache
        # cannot answer — see list_corps.
        "owner_name": member.display_name,
        "channel_id": str(channel.id),
        "pin_message_id": str(pin_msg.id),
        "established_at": now.isoformat(),
        "members": [str(member.id)],
    })
    _set_owner_ptr(member.id, guild.id, channel.id, name)
    log.info("Established corporation '%s' for %s (channel: %s)", name, member, channel.id)
    return channel


def _auto_corp_name(member: discord.Member) -> str:
    """'{username} Space Agency' — the name auto-generated corps are given.
    Uses the display name, which is what the member is known as in the guild —
    the same name `owner_name` records for the owner themselves."""
    return f"{member.display_name} Space Agency"


# In-flight corp creations, by user id. Two KSP clients can link the same account
# seconds apart (running a second game instance is normal), and `find_user_corp`
# cannot see a channel that is still being created — so without this the second
# link would sail past the check and mint a duplicate corp.
_ensuring: set[str] = set()


def ensure_corp_record_for_account(guild_id, account_id, display_name: str,
                                   username: str = "") -> bool:
    """Give a player with no Discord a corporation — the record, without a channel.

    A corp is two things that had always arrived together: a Firestore record that
    says this player exists and can be hired, and a private Discord channel their
    paperwork lands in. A website account can have the first and not the second,
    and the first is the one that matters — it is what puts them in the in-game
    player picker (`/api/v1/corps/list`) and what `_get_corp` answers with when
    somebody offers them a contract. Without it a website player can play but can
    never be *hired*, which is most of the point of the game.

    `channel_id` is deliberately absent rather than empty: `deliver_to_player`
    tests it before posting, so a missing key means "no Discord surface" and the
    delivery falls through to the player's own notification feed, which every
    caller already writes to alongside. Returns True if a record was created.

    `username` is the Boundless handle the picker draws under the display name.
    Optional because only the caller that has just *claimed* one knows it for
    free — everyone else leaves it out and `api_server.list_corps` backfills it
    on the first picker open. Passing it here is only ever an optimisation, never
    the thing that makes the field appear.
    """
    aid = str(account_id)
    try:
        if _get_corp(int(guild_id), aid):
            return False
        now = datetime.datetime.now(datetime.timezone.utc)
        _save_corp(int(guild_id), aid, {
            "name": f"{display_name} Space Agency",
            "owner_id": aid,
            "owner_name": display_name,
            # Only when we actually have one. "" is the pre-claim state rather
            # than an answer, and writing it would tell `list_corps` the question
            # is settled — see the note above `_corp_usernames`.
            **({"owner_username": str(username)} if username else {}),
            # No channel_id and no pin_message_id: there is no channel. See above.
            "established_at": now.isoformat(),
            "members": [aid],
            "web_only": True,
            # Filled by `sync_web_corp_profile` when they set a picture. Stored as
            # a bucket path; the picker signs it at serve time.
            "avatar_url": "",
        })
        # The global pointer is what lets `_get_corp` find this corp from any
        # guild's session, which for an account with no guild of its own is the
        # only way it is ever found.
        _set_owner_ptr(aid, int(guild_id), 0, f"{display_name} Space Agency")
        log.info("Established channel-less corp for website account %s", aid)
        return True
    except Exception as exc:
        # Same contract as `ensure_corp_for_linked_user`: a corp is a convenience,
        # and failing to make one must never cost the player the link they were
        # completing.
        log.warning("Could not establish corp record for %s: %s", aid, exc)
        return False


def sync_web_corp_profile(guild_id, account_id, *, display_name=None,
                          avatar_url=None) -> bool:
    """Keep a channel-less corp's cached identity in step with its account.

    A Discord corp needs nothing like this: the picker resolves the live member
    and its stored `owner_name` is only the fallback for someone who has left. A
    website account has no member to resolve, so what is stored IS what everyone
    sees — and it would otherwise be frozen at whatever the name was on the day
    the corp was made.

    The avatar is kept as a bucket path, not a signed URL: a signed URL expires,
    and the serve point signs it fresh anyway. Denormalised onto the corp rather
    than read per-row, so the player picker stays one scan instead of one extra
    Firestore read per website account on every open.
    """
    patch = {}
    if display_name:
        patch["owner_name"] = str(display_name)
    if avatar_url is not None:
        patch["avatar_url"] = str(avatar_url or "")
    if not patch:
        return False
    try:
        ref = _get_corp_ref(int(guild_id), str(account_id))
        if not ref.get().exists:
            return False
        ref.set(patch, merge=True)
        return True
    except Exception as exc:
        log.warning("Could not sync corp profile for %s: %s", account_id, exc)
        return False


async def ensure_corp_for_linked_user(bot, guild_id, user_id) -> "discord.TextChannel | None":
    """Give a freshly linked player their corporation, if they haven't got one.

    This is the *only* automatic corp creation left, and the link is what makes it
    legitimate: the KSP client refuses to transmit anything at all until the player
    has accepted the privacy policy and terms in-mod, so a link code reaching the
    server is the server's evidence that they did. Joining the Discord server is
    not — see the module docstring.

    Returns the new channel, or None if there was nothing to do (already has a corp,
    not a member of that guild any more, guild unknown) or if creation failed.
    Never raises: a corp is a convenience, and failing to make one must not cost the
    player the link they were completing.
    """
    uid = str(user_id)
    if uid in _ensuring:
        return None
    _ensuring.add(uid)
    try:
        guild = bot.get_guild(int(guild_id)) if bot else None
        if guild is None:
            log.warning("Cannot establish corp for %s: guild %s unknown", uid, guild_id)
            return None
        member = await _resolve_member(guild, uid)
        if member is None or member.bot:
            return None
        if await asyncio.to_thread(find_user_corp, guild.id, member.id):
            return None
        channel = await _establish_corp(guild, member, _auto_corp_name(member))
        log.info("Established corp for %s on link (consent accepted in-mod)", member)
        return channel
    except Exception as exc:
        log.warning("Could not establish corp for linked user %s: %s", uid, exc)
        return None
    finally:
        _ensuring.discard(uid)


async def _resolve_member(guild: discord.Guild, uid) -> discord.Member | None:
    """A member by id, or None for anyone who has left (or a bad id)."""
    try:
        return guild.get_member(int(uid)) or await guild.fetch_member(int(uid))
    except (discord.NotFound, discord.HTTPException, ValueError, TypeError):
        return None


async def _apply_corp_privacy(guild: discord.Guild, corp: dict, *,
                              reason: str, force: bool = False) -> str:
    """Bring one corp's channel up to spec: the private overwrite set, and the
    channel name without the retired `corp-` prefix.

    Returns "updated", "ok" (already compliant — skipped unless `force`, so the
    startup sweep costs zero API calls on a guild that is already migrated),
    or "missing" (channel gone). `force` re-asserts the overwrites even on a
    private channel, for when the mod role mapping or membership has changed;
    both fixes share one `edit` call, since channel edits are rate-limited.
    """
    channel = guild.get_channel(int(corp.get("channel_id") or 0))
    if channel is None:
        return "missing"
    edits: dict = {}
    if channel.name.startswith("corp-") and channel.name != "corp-":
        edits["name"] = channel.name[len("corp-"):]
    if force or channel.overwrites_for(guild.default_role).view_channel is not False:
        owner = await _resolve_member(guild, corp.get("owner_id"))
        members = [m for m in [await _resolve_member(guild, uid)
                               for uid in corp.get("members", [])] if m]
        edits["overwrites"] = _corp_overwrites(guild, owner, members)
    if not edits:
        return "ok"
    await channel.edit(reason=reason, **edits)
    return "updated"


def _save_corp(guild_id: int, user_id: int, data: dict) -> None:
    """Write corporation data to Firestore."""
    ref = _get_corp_ref(guild_id, user_id)
    ref.set(data)
    # Ensure guild parent doc exists
    _db.collection("guilds").document(str(guild_id)).set(
        {"_exists": True}, merge=True
    )


def _get_corp(guild_id: int, user_id: int) -> dict | None:
    """Read a user's corporation. Checks the given guild first, then falls back to
    the user's GLOBAL corp (which may live in another server), so callers that pass
    a contract/session guild still find the one corp the user owns anywhere.
    The returned dict carries `guild_id` so callers can resolve its channel."""
    doc = _get_corp_ref(guild_id, user_id).get()
    if doc.exists:
        d = doc.to_dict()
        d.setdefault("guild_id", str(guild_id))
        return d
    ptr = get_user_corp_global(user_id)
    if ptr:
        og = int(ptr.get("guild_id", 0) or 0)
        if og and og != guild_id:
            d2 = _get_corp_ref(og, user_id).get()
            if d2.exists:
                d = d2.to_dict()
                d.setdefault("guild_id", str(og))
                return d
    # Last resort: this may be an ACCOUNT id where corps are keyed by the Discord
    # snowflake. `ensure_corp_for_linked_user` is handed an account id but resolves
    # a member and keys everything on `member.id`, so a joined account (`a_…`) owns
    # a corp that a lookup by account id cannot see. Callers that correctly resolve
    # the account id first — the weekly-mission buttons, `select_mission` — were
    # therefore told "you need a corporation first" by their own correction. Resolve
    # back and retry once; for everybody else `discord_for_account` returns the id
    # unchanged and this costs one cheap read that was already going to miss.
    try:
        from data import accounts
        did = accounts.discord_for_account(str(user_id))
    except Exception as exc:
        log.warning("Corp lookup could not resolve account %s to a snowflake: %s",
                    user_id, exc)
        return None
    if did and str(did) != str(user_id):
        return _get_corp(guild_id, did)
    return None


def find_user_corp(guild_id: int, user_id: int) -> dict | None:
    """Find the corp a user belongs to, as owner or member. None if they're in none.

    Corps are keyed by the owner's id, so an owner is a direct (now global) lookup;
    members are found via the `members` array within the given guild.
    """
    own = _get_corp(guild_id, user_id)
    if own:
        return own
    col = _db.collection("guilds").document(str(guild_id)).collection("corps")
    for doc in col.where("members", "array_contains", str(user_id)).stream():
        d = doc.to_dict()
        d.setdefault("guild_id", str(guild_id))
        return d
    return None


def _delete_corp(guild_id: int, user_id: int) -> None:
    """Delete a corporation record from Firestore."""
    _get_corp_ref(guild_id, user_id).delete()


# ── Global corp ownership (one corp per user across ALL servers) ─────────────
# The per-guild corp doc still lives at guilds/{gid}/corps/{uid} (its channel is in
# a specific server), but a global pointer records the ONE server a user owns a
# corp in, so establishing a corp anywhere replaces a corp owned elsewhere.

def _owner_ref(user_id: int):
    return _db.collection("corp_owners").document(str(user_id))


def get_user_corp_global(user_id: int) -> dict | None:
    """Where (if anywhere) this user owns a corp: {guild_id, channel_id, name}."""
    snap = _owner_ref(user_id).get()
    return snap.to_dict() if snap.exists else None


def _set_owner_ptr(user_id: int, guild_id: int, channel_id: int, name: str) -> None:
    _owner_ref(user_id).set({
        "user_id": str(user_id),
        "guild_id": str(guild_id),
        "channel_id": str(channel_id),
        "name": name,
    })


def _clear_owner_ptr(user_id: int) -> None:
    _owner_ref(user_id).delete()


# ── Confirmation UI ──────────────────────────────────────────────────────────

class CorpReplaceView(discord.ui.View):
    """DM view asking if the user wants to replace their existing corporation."""

    def __init__(self, cog: "Corps", guild: discord.Guild, owner: discord.Member, new_name: str):
        super().__init__(timeout=120)
        self.cog = cog
        self.guild = guild
        self.owner = owner
        self.new_name = new_name
        self.result: bool | None = None

    @discord.ui.button(label="Replace", style=discord.ButtonStyle.danger, emoji="🔄")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = True
        self.stop()
        await interaction.response.edit_message(
            content=t(self.guild.id, "corps.replace.confirming"), view=None
        )
        await self.cog._replace_corp(self.guild, self.owner, self.new_name)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = False
        self.stop()
        await interaction.response.edit_message(
            content=t(self.guild.id, "corps.replace.cancelled"), view=None
        )

    async def on_timeout(self) -> None:
        self.result = False


# ── Cog ──────────────────────────────────────────────────────────────────────

def _is_admin():
    """Mapped bot-admin role or owner (`perms.is_admin_user`); guild
    administrators are not auto-admins. `@default_permissions` above each
    command is only Discord's *default* — a server admin can widen it to any
    role in Integrations, and until this check existed the bot then ran the
    command with no gate of its own."""
    async def predicate(interaction: discord.Interaction) -> bool:
        return perms.is_admin_user(interaction)
    return app_commands.check(predicate)


class Corps(commands.Cog, name="Corps"):
    """Corporation management system."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._swept = False  # the startup sweep runs once, not on every reconnect

    # ── /corpsetup ────────────────────────────────────────────────────────────
    @app_commands.command(
        name="corpsetup",
        description="Establish a new corporation with its own text channel",
    )
    @app_commands.describe(name="Name for your corporation")
    # Bounded and throttled. This is self-service guild-CHANNEL creation: `name`
    # went verbatim into `create_text_channel`, where anything over Discord's
    # 100-character limit is a 400 — and because the handler has already deferred,
    # the cog's error handler saw `is_done()` and sent nothing at all, leaving the
    # user on a permanent "thinking…". The replace flow is a delete plus a create
    # per invocation, on a Discord bucket that is severe and shared bot-wide with
    # ticket intake, so one member looping it stalled channel operations for the
    # whole deployment.
    @app_commands.checks.cooldown(2, 600.0, key=lambda i: (i.guild_id, i.user.id))
    async def corpsetup(
        self, interaction: discord.Interaction,
        name: app_commands.Range[str, 1, 32],
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                tp(None, interaction.user.id, "common.server_only"), ephemeral=True
            )
            return

        guild = interaction.guild
        member = interaction.user
        gid = guild.id

        # Collapse whitespace and drop anything that is not plausibly a name. The
        # Range above bounds the length; this bounds the content, because the value
        # is published into the channel topic, the pinned embed and the in-game
        # player pickers that other people read.
        name = re.sub(r"\s+", " ", name).strip()
        name = re.sub(r"[^\w \-'&.]", "", name, flags=re.UNICODE).strip()
        if not name:
            await interaction.response.send_message(
                "❌ That corporation name has no usable characters in it.",
                ephemeral=True)
            return

        # One corp per user GLOBALLY — check whether they own one in any server.
        existing = get_user_corp_global(member.id)

        if existing:
            # User already owns a corp somewhere — DM them for confirmation. The
            # replace flow deletes the old one wherever it lives, then creates the
            # new one here.
            old_name = existing.get("name", "Unknown")
            old_guild = self.bot.get_guild(int(existing.get("guild_id", 0) or 0))
            old_guild_name = old_guild.name if old_guild else "another server"
            old_channel_id = existing.get("channel_id")

            view = CorpReplaceView(self, guild, member, name)

            try:
                dm_embed = discord.Embed(
                    title=t(gid, "corps.replace.title"),
                    description=t(gid, "corps.replace.desc",
                        guild=old_guild_name, old=old_name,
                        channel=old_channel_id, new=name),
                    color=discord.Color.orange(),
                )
                await member.send(embed=dm_embed, view=view)
                await interaction.response.send_message(
                    tp(gid, member.id, "corps.replace.check_dm"),
                    ephemeral=True,
                )
            except discord.Forbidden:
                await interaction.response.send_message(
                    tp(gid, member.id, "corps.replace.no_dm"),
                    ephemeral=True,
                )
            return

        # No existing corp — create one
        await interaction.response.defer(ephemeral=True)

        channel = await _establish_corp(guild, member, name)

        await interaction.followup.send(
            tp(gid, member.id, "corps.setup.done", name=name, channel=channel.mention),
            ephemeral=True,
        )
        log.info("%s established corporation '%s' (channel: %s)", member, name, channel.id)

    async def _replace_corp(
        self, guild: discord.Guild, owner: discord.Member, new_name: str
    ) -> None:
        """Delete the user's existing corp WHEREVER it lives (possibly another
        server), then create a replacement in `guild`."""
        existing = get_user_corp_global(owner.id)
        if existing:
            old_gid = int(existing.get("guild_id", 0) or 0)
            old_channel_id = existing.get("channel_id")
            old_guild = self.bot.get_guild(old_gid)

            # Delete the old channel + GK registration in whichever guild it was in.
            if old_channel_id and old_guild:
                old_channel = old_guild.get_channel(int(old_channel_id))
                if old_channel:
                    try:
                        await old_channel.delete(reason=f"Corporation replaced by {owner}")
                    except discord.Forbidden:
                        log.warning("No permission to delete old corp channel %s", old_channel_id)
                from cogs.gkchannels import remove_gk_channel
                remove_gk_channel(old_gid, int(old_channel_id))

            # Delete the old per-guild record + clear the global pointer.
            if old_gid:
                _delete_corp(old_gid, owner.id)
            _clear_owner_ptr(owner.id)

        # Create new corp in the requesting guild.
        channel = await _establish_corp(guild, owner, new_name)

        try:
            await owner.send(t(guild.id, "corps.replace.done",
                name=new_name, guild=guild.name, channel=channel.id))
        except discord.Forbidden:
            pass

        log.info("%s replaced corporation with '%s' (channel: %s)", owner, new_name, channel.id)

    # ── Startup sweep ─────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Bring every guild's existing corp channels up to spec on boot:
        privatize the ones created before privacy existed and drop the retired
        `corp-` prefix. Idempotent and cheap when there is nothing to do —
        an already-private channel is detected from the local permission cache
        (zero API calls).

        It deliberately creates **nothing**. This sweep used to also mint a corp
        for every member without one, which is the server-join auto-creation in
        another shape: on the next boot after a member joined it would create the
        channel the join listener no longer does. Members who have not linked a
        KSP client have not consented to anything yet, and the one whose consent
        arrives later gets a corp from the link itself (`ensure_corp_for_linked_user`).
        """
        if self._swept:  # on_ready re-fires on every reconnect
            return
        self._swept = True

        for guild in self.bot.guilds:
            privatized = 0
            col = _db.collection("guilds").document(str(guild.id)).collection("corps")
            for doc in await asyncio.to_thread(lambda: list(col.stream())):
                d = doc.to_dict() or {}
                d.setdefault("owner_id", doc.id)
                try:
                    if await _apply_corp_privacy(
                            guild, d, reason="Corp privacy startup sweep") == "updated":
                        privatized += 1
                except Exception as exc:
                    log.warning("Startup privacy sweep failed for corp %s in %s: %s",
                                doc.id, guild.id, exc)

            if privatized:
                log.info("Corp startup sweep in %s: %d channel(s) updated "
                         "(privacy/rename)", guild.id, privatized)

    # ── /admin corpsgenerate ──────────────────────────────────────────────────
    @app_commands.command(
        name="corpsgenerate",
        description="Create a corporation for every linked member that doesn't have one",
    )
    @app_commands.default_permissions(administrator=True)
    @_is_admin()
    async def corpsgenerate(self, interaction: discord.Interaction) -> None:
        """Backfill for players who linked before corps were created on link.

        It covers **linked** members only. Running it over the whole member list
        would re-introduce the server-join auto-creation by hand: a corp channel
        publishes a member's name and avatar and carries their contract traffic,
        and someone who has never accepted the in-mod terms has not asked for
        either. Unlinked members are counted back so an admin can see the
        difference rather than wonder why the number is small.

        Channel creation is heavily rate-limited by Discord, so this paces
        itself; on a large guild it can take a while. Safe to re-run — members
        who already have a corp (here or anywhere) are skipped.
        """
        if not interaction.guild:
            await interaction.response.send_message(
                tp(None, interaction.user.id, "common.server_only"), ephemeral=True)
            return
        guild = interaction.guild
        await interaction.response.defer(ephemeral=True)

        from api_auth import linked_user_ids
        linked = await asyncio.to_thread(linked_user_ids)

        created, failed, skipped, unlinked = 0, [], 0, 0
        for member in list(guild.members):
            if member.bot:
                continue
            if str(member.id) not in linked:
                unlinked += 1
                continue
            try:
                if find_user_corp(guild.id, member.id):
                    skipped += 1
                    continue
                await _establish_corp(guild, member, _auto_corp_name(member))
                created += 1
                await asyncio.sleep(2)  # be gentle with the channel-create rate limit
            except Exception as exc:
                log.warning("corpsgenerate: could not create corp for %s: %s", member, exc)
                failed.append(member.display_name)

        lines = [f"🏢 Created **{created}** corporation(s); {skipped} member(s) already had one."]
        if unlinked:
            lines.append(f"⏭️ Skipped **{unlinked}** member(s) who haven't linked KSP yet; "
                         "they get a corp automatically once they accept the terms in-mod.")
        if failed:
            lines.append(f"⚠️ Failed for: {', '.join(failed[:10])}"
                         + (f" (+{len(failed) - 10} more)" if len(failed) > 10 else ""))
        try:
            await interaction.followup.send("\n".join(lines), ephemeral=True)
        except discord.HTTPException:
            # A very large backfill can outlive the interaction token (15 min).
            log.info("corpsgenerate in %s: %d created, %d skipped, %d unlinked, %d failed",
                     guild.id, created, skipped, unlinked, len(failed))

    # ── /admin corpsprivacy ───────────────────────────────────────────────────
    @app_commands.command(
        name="corpsprivacy",
        description="Make every existing corporation channel private (corp + mods only)",
    )
    @app_commands.default_permissions(administrator=True)
    @_is_admin()
    async def corpsprivacy(self, interaction: discord.Interaction) -> None:
        """One-time migration: apply the private overwrites `_create_corp_channel`
        now sets to corp channels created before privacy existed. Discord
        permissions are not retroactive, so old channels stay public until this
        sweep runs. Safe to re-run — it just re-asserts the same overwrites."""
        if not interaction.guild:
            await interaction.response.send_message(
                tp(None, interaction.user.id, "common.server_only"), ephemeral=True)
            return
        guild = interaction.guild
        await interaction.response.defer(ephemeral=True)

        updated, failed, missing = [], [], []
        col = _db.collection("guilds").document(str(guild.id)).collection("corps")
        for doc in await asyncio.to_thread(lambda: list(col.stream())):
            d = doc.to_dict() or {}
            d.setdefault("owner_id", doc.id)
            name = d.get("name", doc.id)
            try:
                # force=True: re-asserts the set even on already-private channels,
                # so this command also repairs a changed mod-role mapping.
                outcome = await _apply_corp_privacy(
                    guild, d, reason=f"Corp privacy migration by {interaction.user}",
                    force=True)
            except discord.Forbidden:
                failed.append(name)
                continue
            if outcome == "missing":
                missing.append(name)
            else:
                updated.append(name)

        lines = [f"🔒 Made **{len(updated)}** corp channel(s) private."]
        if failed:
            lines.append(f"⚠️ No permission to edit: {', '.join(failed[:10])}")
        if missing:
            lines.append(f"👻 Channel gone (skipped): {', '.join(missing[:10])}")
        if guild_config.resolve_role(guild, "mod") is None:
            lines.append("⚠️ No `mod` role is mapped (`/admin setrole`); only "
                         "administrators can see the channels until one is.")
        await interaction.followup.send("\n".join(lines), ephemeral=True)
        log.info("%s ran corpsprivacy in %s: %d updated, %d failed, %d missing",
                 interaction.user, guild.id, len(updated), len(failed), len(missing))

    # ── Error handler ─────────────────────────────────────────────────────────
    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        # `is_done()` is True after a DEFER as well as after a reply, so the old
        # "only speak if nothing was sent" test stayed silent for exactly the
        # commands that had deferred — leaving the user on a permanent "thinking…"
        # with no error at all. Follow up instead when the response is already used.
        async def _say(msg: str) -> None:
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
            except Exception:            # the error path must not raise its own
                log.debug("Could not deliver the corps error message", exc_info=True)

        if isinstance(error, app_commands.CommandOnCooldown):
            await _say(f"⏳ Slow down, try again in {error.retry_after:.0f}s.")
            return
        if isinstance(error, app_commands.CheckFailure):
            await _say(tp(interaction.guild_id, interaction.user.id, "common.no_perm"))
            return
        log.error("Corps cog error: %s", error, exc_info=True)
        await _say(t(interaction.guild_id, "common.error"))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Corps(bot))
