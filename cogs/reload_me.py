from __future__ import annotations

import importlib
import os
import sys

import beacon
import discord
from beacon import beacon_commands
from discord.ext import commands
from dotenv import load_dotenv, set_key

import VERSION
import config


class Reload(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @beacon_commands.command(name="rs", description=".", permissions_preset="bot_owner")
    async def reload_beacon(self, interaction: discord.Interaction) -> None:
        load_dotenv(override=True)
        importlib.reload(config)
        importlib.reload(beacon)

        await interaction.response.send_message("👍️", ephemeral=True)

    @commands.command(name="rs")
    async def reload_prefix(self, ctx: commands.Context) -> None:
        if not await self.bot.is_owner(ctx.author):
            await ctx.send("🤫")
            return

        modules_to_purge = [
            'beacon',
            'beacon.core',
            'beacon.core.commands_registry',
            'beacon.core.dashboard',
            'beacon.core.preconditions',
            'beacon.core.errors',
            'beacon.core.beacon_commands',
            'beacon.ext',
            'beacon.ext.diagnostics',
            'beacon.ext.path',
            'beacon.ext.pic',
            'beacon.utils',
            'beacon.utils.checks',
            'beacon.utils.log',
            'beacon.utils.paginator',
            'beacon.utils.timeparser',
            'beacon.utils.views',
            'beacon.bot'
        ]

        try:
            for module in modules_to_purge:
                if module in sys.modules:
                    del sys.modules[module]

            importlib.import_module('beacon')
            load_dotenv(override=True)
            importlib.reload(config)
            importlib.reload(VERSION)
            await ctx.send("👍️")
        except Exception as e:
            await ctx.send(f"Error: {e}")

    @commands.command(name="url")
    async def update_url(self, ctx: commands.Context, new_url: str) -> None:
        if not await self.bot.is_owner(ctx.author):
            await ctx.send("🤫")
            return
        dotenv_path = '.env'

        try:
            set_key(dotenv_path, "COMPUTERURL", new_url)
            os.environ["COMPUTERURL"] = new_url
            await ctx.send(f"Successfully updated `COMPUTERURL` to: `{new_url}`", delete_after=10)
        except Exception as e:
            await ctx.send(f"Error: {e}")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Reload(bot))
