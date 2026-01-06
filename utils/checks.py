import discord
from discord.ext import commands
from discord import app_commands

# MOD CHECKS (legacy and Next):
def mod_check():
    """Check for moderation permissions (prefix commands)"""
    async def predicate(ctx):
        perms = ctx.author.guild_permissions
        if perms.moderate_members or perms.ban_members:
            return True
        raise commands.MissingPermissions(["moderate_members", "ban_members"])
    return commands.check(predicate)


async def slash_mod_check(interaction: discord.Interaction):
    """Check for moderation permissions (slash commands)"""
    if not interaction.guild:
        raise app_commands.MissingPermissions(["moderate_members", "ban_members"])

    perms = interaction.user.guild_permissions
    if perms.moderate_members or perms.ban_members:
        return True
    raise app_commands.MissingPermissions(["moderate_members", "ban_members"])


def guild_check():
    """Check that command is run in a guild (not DMs)"""
    async def predicate(interaction: discord.Interaction):
        if not interaction.guild:
            raise app_commands.CheckFailure("This command can only be used in a server.")
        return True
    return app_commands.check(predicate)
