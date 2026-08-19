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

import discord

from config import cfg


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
