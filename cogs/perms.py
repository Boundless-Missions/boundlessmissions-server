"""
cogs/perms.py – Permission helpers that are mimic-safe.

The admin "mimic" system (see bot.py) swaps `interaction.user` to the mimicked
target *before* command/view checks run. If a permission check reads the swapped
`interaction.user`, an admin who mimics a higher-privileged user (e.g. the bot
owner) would borrow that user's authority — a privilege escalation.

`real_user()` unwraps that swap: permission checks MUST gate on the *real*
invoker, so mimic only changes business-logic identity, never the authority a
command is gated on. The real user is stashed by the mimic patch in
`interaction.extras["_mimic_real_user"]`.
"""

import logging

import discord
from discord import app_commands

from config import cfg

log = logging.getLogger(__name__)


def real_user(interaction: discord.Interaction):
    """The real invoker, unwrapping any mimic swap. Use this in every permission
    check (never the raw interaction.user, which may be a mimic target)."""
    extras = getattr(interaction, "extras", None) or {}
    return extras.get("_mimic_real_user") or interaction.user


def is_owner_user(interaction: discord.Interaction) -> bool:
    """True if the real invoker is the configured bot owner."""
    return getattr(real_user(interaction), "id", None) == cfg.OWNER_ID


def is_admin_user(interaction: discord.Interaction) -> bool:
    """True if the real invoker is the bot owner or holds this guild's mapped
    bot-admin role (key "admin", set per guild via /admin setrole).

    Guild administrators are still NOT auto-admins: the admin role must be
    mapped explicitly, and there is no settings.py fallback for it. This gate
    covers only the guild-scoped /admin commands (setchannel, setrole,
    announce, …); bot-wide commands (publishing, policy, link-as, mimic, …)
    stay behind is_owner_user, because a role granted in one guild must never
    carry authority over every guild the bot is in."""
    from data import guild_config
    u = real_user(interaction)
    if getattr(u, "id", None) == cfg.OWNER_ID:
        return True
    if not isinstance(u, discord.Member):
        return False
    admin_role = guild_config.resolve_role(u.guild, "admin")
    return bool(admin_role and u.get_role(admin_role.id))


def is_mod_user(interaction: discord.Interaction) -> bool:
    """True if the real invoker is a moderator: the bot owner, the guild's mapped
    mod role (per-guild via guild_config, falling back to settings.MOD_ROLE_ID),
    or a member with kick/administrator permission."""
    from data import guild_config
    u = real_user(interaction)
    if getattr(u, "id", None) == cfg.OWNER_ID:
        return True
    if not isinstance(u, discord.Member):
        return False
    mod_role = guild_config.resolve_role(u.guild, "mod")
    if mod_role and u.get_role(mod_role.id):
        return True
    return u.guild_permissions.kick_members or u.guild_permissions.administrator


def holds_authority_anywhere(client: discord.Client, user_id: int) -> bool:
    """True if this Discord id is the owner, or holds the mapped bot-admin or mod
    role, or kick/administrator permission, in ANY guild the bot is in.

    The per-interaction checks above answer "in this guild", which is right for a
    guild-scoped command and wrong for a decision about the *account*: the web
    console grants a guild admin their console from whichever guild the role is
    held in (`api_server._admin_role_guild_ids`), so an authority check that only
    looked at the guild an interaction happened in — or at nothing, in a DM —
    could be sidestepped by asking the role holder to act somewhere else."""
    from data import guild_config
    if user_id == cfg.OWNER_ID:
        return True
    for g in getattr(client, "guilds", []) or []:
        member = g.get_member(user_id)
        if member is None:
            continue
        for key in ("admin", "mod"):
            role = guild_config.resolve_role(g, key)
            if role is not None and member.get_role(role.id) is not None:
                return True
        if member.guild_permissions.kick_members or member.guild_permissions.administrator:
            return True
    return False


# ── The tier that writes GLOBAL records ──────────────────────────────────────
#
# `is_mod_user` above ends in `kick_members or administrator`, so anyone holding
# either in a guild the bot is in passes it with no bot-side configuration at all.
# That is right for what it was written for — kick, ban, mute, and the other
# guild-local actions in cogs/moderation.py, which act on guild membership and
# whose authority genuinely is a Discord permission.
#
# It is wrong for the five commands that write records the whole product shares:
# /givemoney, /fine, /setbalance (the global `users/{id}` wallet), /setxp and
# /contractreset (the global contract collection). There the fallback means the
# mod tier is *self-granted* — obtaining it takes no more than a server where you
# hold administrator, which until guild_gate landed meant any server at all.
#
# `moderatable_here` already bounds *who* such a moderator may act on: the target
# has to be a member of the guild they are moderating in. This bounds the other
# half — that the authority itself has to have been handed over deliberately,
# rather than inferred from a Discord permission nobody granted with the shared
# economy in mind. So: the bot owner, or a holder of the guild's **mapped** mod or
# admin role (/admin setrole), and nothing else.
#
# The admin role counts for the mod tier because it is strictly the higher of the
# two (`is_admin_user` gates the guild-scoped console surface); an admin who could
# not run a mod command would be a rung missing from the middle of the ladder.

_MAPPED_TIERS = {"mod": ("mod", "admin"), "admin": ("admin",)}


def global_records_refusal(interaction, tier: str = "mod") -> str | None:
    """Why this invoker may NOT run a command that writes global records, or None.

    Returns a sentence rather than a bool because the two failure modes need
    different answers and the generic "you don't have permission" is wrong for one
    of them: an owner whose guild has no mapped role has a *configuration* problem,
    and telling them it is a permission problem sends them to the wrong screen.
    """
    if is_owner_user(interaction):
        return None
    guild = getattr(interaction, "guild", None)
    if guild is None:
        return ("That command has to be run in a server. It writes records shared "
                "across every server, so the authority for it has to come from one.")
    u = real_user(interaction)
    if not isinstance(u, discord.Member):
        return "That command can only be run by a member of this server."

    from data import guild_config
    keys = _MAPPED_TIERS.get(tier, _MAPPED_TIERS["mod"])
    mapped = [(k, guild_config.resolve_role(guild, k)) for k in keys]
    for _key, role in mapped:
        if role is not None and u.get_role(role.id) is not None:
            return None

    if not any(role is not None for _key, role in mapped):
        label = "bot-admin" if tier == "admin" else "bot-moderator or bot-admin"
        return (f"This server has no {label} role mapped, so nobody here can run "
                f"that command yet. It moves coins, XP and contracts that every "
                f"server shares, so it is deliberately not granted by a Discord "
                f"permission, the bot owner maps a role to it with "
                f"`/admin setrole`.")
    names = ", ".join(f"@{role.name}" for _k, role in mapped if role is not None)
    return (f"That command needs {names} in this server. It moves coins, XP and "
            f"contracts shared across every server, so holding Administrator or "
            f"Kick Members here is deliberately not enough.")


def is_global_records_user(interaction, tier: str = "mod") -> bool:
    """Bool form of `global_records_refusal`, for a UI that needs to hide a button."""
    return global_records_refusal(interaction, tier) is None


def global_records_mod_only():
    """Gate for a mod-tier command that writes global records. Mimic-safe."""
    async def predicate(interaction: discord.Interaction) -> bool:
        why = global_records_refusal(interaction, "mod")
        if why is not None:
            raise app_commands.CheckFailure(why)
        return True
    return app_commands.check(predicate)


def global_records_admin_only():
    """Gate for an admin-tier command that writes global records. Mimic-safe."""
    async def predicate(interaction: discord.Interaction) -> bool:
        why = global_records_refusal(interaction, "admin")
        if why is not None:
            raise app_commands.CheckFailure(why)
        return True
    return app_commands.check(predicate)


async def block_if_mod_only(interaction: discord.Interaction) -> bool:
    """Gate for gameplay commands the in-game KSP mod can perform itself.

    When `settings.MOD_ONLY_GAMEPLAY` is enabled these commands are disabled on
    Discord so the action can only be triggered from inside the game. Returns
    True (after replying ephemerally) when the command should abort; False when
    it may proceed. Call this BEFORE deferring, while the interaction response
    is still unused.
    """
    import settings
    from i18n import tp
    if not settings.MOD_ONLY_GAMEPLAY:
        return False
    await interaction.response.send_message(
        tp(interaction.guild_id, interaction.user.id, "common.mod_only"),
        ephemeral=True,
    )
    return True


# ── Roles the bot hands out on request ───────────────────────────────────────
#
# Two of the role keys /admin setrole maps are SELF-ASSIGNABLE: the notification
# ping role (cogs/roles._handle_notif — a public button, no eligibility check at
# all) and the 15 level titles (LevelSelector, gated only on the presser's own
# achievements). Those keys are cosmetic by design, and nothing downstream asks
# what the mapped role can actually do — so pointing one at a privileged role
# turned a bot-configuration grant into a Discord-permission grant: map
# `notifications` -> @Moderator, press "🔔 Enable notifications", and the bot
# assigns it. The mapping and the grant are checked separately on purpose: a
# mapping made before this existed, or a role given permissions after it was
# mapped, is only ever caught at grant time.

def moderatable_here(interaction, target) -> str | None:
    """Why this moderator may NOT act on this target, or None if they may.

    The economy/XP/contract mod tools are gated on `kick_members` **in the guild the
    command was run in**, and the records they write are GLOBAL: `users/{id}` is one
    document the whole product reads (`data/store.py`: "guild_id is accepted for
    call-site compatibility but not used as a key"), and contracts live in one
    global collection. So a moderator of the smaller of two synced guilds could mint
    unbounded coins into the shared economy, and `/contractreset` could cancel and
    refund the in-flight work of a player they have never shared a server with.

    That is the invariant `is_admin_user` states in its own docstring — "a role
    granted in one guild must never carry authority over every guild the bot is in"
    — failing on the surfaces that pass a believable-looking check.

    The rule here is deliberately the narrow one that keeps the ordinary case
    working: a moderator may act on someone who is a member of the guild they are
    moderating in. That is the whole legitimate use. It is NOT a claim that the
    effect is guild-local — the wallet is global and that is by design — only that
    the authority has to come from somewhere the target actually is.

    Two deliberate exemptions, or this would be the "gate that assumes an identity
    everyone has" mistake:
      * the bot owner passes always (they hold every tier already);
      * a target with no Discord presence — a website-only account — cannot be a
        member of anybody's guild, so no guild moderator can establish authority
        over them. They are referred to the owner console rather than silently
        refused, because that is where the tool for them lives.
    """
    if is_owner_user(interaction):
        return None
    guild = getattr(interaction, "guild", None)
    if guild is None:
        return ("That command needs to be run in the server you moderate. "
                "It cannot be used in a DM.")
    member = getattr(target, "member", None)
    if member is not None and getattr(member, "guild", None) is not None \
            and member.guild.id == guild.id:
        return None
    # Re-check against the live member list: `target.member` is None for a website
    # account AND for a Discord user who is simply not cached here.
    tid = getattr(target, "account_id", None)
    if tid is not None and str(tid).isdigit() and guild.get_member(int(tid)) is not None:
        return None
    return ("That player is not a member of this server, so this server's "
            "moderator role does not cover them. The wallet, XP and contracts are "
            "shared across every server the bot is in, ask the bot owner, who can "
            "act on any account from the web console.")


def is_self_assignable_key(key: str) -> bool:
    """True for the role keys any member can have applied by pressing a button."""
    k = str(key or "")
    return k == "notifications" or k.startswith("level_")


def role_grants_authority(role) -> str | None:
    """Why `role` must not be handed out on request, or None if it is safe.

    A Discord role's power is its permission bits, so the test is exactly that:
    anything @everyone does not already carry is authority. Managed roles (bot and
    integration roles) are refused too — nobody can assign one, so a mapping to one
    is a silently dead feature rather than a grant.

    This deliberately does NOT walk channel overwrites — see
    `role_opens_private_channel`, which does, and is called only when a mapping is
    created rather than on every grant.
    """
    if role is None:
        return None
    if getattr(role, "is_default", None) and role.is_default():
        return "@everyone is not a role the bot can add or remove."
    if getattr(role, "managed", False):
        return f"@{role.name} is managed by an integration, so the bot cannot assign it."
    guild = getattr(role, "guild", None)
    base = guild.default_role.permissions.value if guild is not None else 0
    extra = role.permissions.value & ~base
    if not extra:
        return None
    try:
        import discord as _discord
        names = [n.replace("_", " ") for n, on in _discord.Permissions(extra) if on]
    except Exception:
        names = []
    shown = ", ".join(sorted(names)[:5]) or "extra permissions"
    return (f"@{role.name} carries permissions @everyone does not ({shown}). "
            f"A role members can give themselves must confer nothing.")


def role_opens_private_channel(guild, role) -> str | None:
    """Whether `role` is the key to a channel @everyone cannot see, or None.

    The shape `role_grants_authority` is blind to: a role can carry no permission
    bit at all and still be what admits you to a staff channel, through a
    per-channel overwrite. Self-assignable keys (`notifications`, `level_*`) are
    handed out by a button any member may press, so mapping such a role there would
    publish a private channel to the whole server.

    Called at MAP time only, never on a grant. It walks every channel in the guild,
    which is far too expensive to do each time somebody presses the notifications
    button, and it does not need to be: the mapping is what creates the exposure,
    and refusing it is what prevents one. A role whose overwrites change *after* it
    was mapped is not covered, which is the honest limit of a map-time check.

    "Private" means @everyone cannot already see it: an overwrite that allows view
    on a channel the whole server can read grants nothing, so it is not reported.
    """
    if guild is None or role is None:
        return None
    try:
        everyone = guild.default_role
        default_view = bool(getattr(everyone.permissions, "view_channel", False))
        unreadable = 0
        for ch in getattr(guild, "channels", ()) or ():
            try:
                ow = ch.overwrites_for(role)
            except Exception:
                # Fail CLOSED per channel, matching the outer handler. Skipping
                # meant "this role opens nothing here", which is a silent yes for
                # the one channel we could not inspect — the exact answer the outer
                # `except` refuses to give for the guild as a whole. The two
                # handlers were deciding the same question in opposite directions.
                unreadable += 1
                continue
            if ow.view_channel is not True and ow.read_messages is not True:
                continue          # the role does not open this channel
            base = ch.overwrites_for(everyone)
            everyone_sees = base.view_channel
            if everyone_sees is None:
                everyone_sees = base.read_messages
            if everyone_sees is None:
                everyone_sees = default_view
            if everyone_sees:
                continue          # already public; the role adds nothing
            return (f"@{role.name} is what grants access to #{ch.name}, which "
                    f"@everyone cannot see. A role members can give themselves "
                    f"must not open a private channel.")
        if unreadable:
            return (f"The bot could not check {unreadable} channel(s) for that role. "
                    "Refusing the mapping rather than guessing.")
    except Exception as exc:      # a lookup failure must not become a silent yes
        log.warning("Could not inspect channel overwrites for role %s: %s", role, exc)
        return ("The bot could not check which channels that role opens. "
                "Refusing the mapping rather than guessing.")
    return None
