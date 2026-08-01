import logging

import aiohttp
import discord
from discord.ext import commands, tasks

from config import DBL_TOKEN
from config import OVERRIDE_VOTEWALL

logger = logging.getLogger("discord")
class DBLCommands(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.dbl_token = DBL_TOKEN
        self.update_dbl_commands.start()

    def cog_unload(self):
        self.update_dbl_commands.cancel()

    def format_command(self, command):
        cmd_type = getattr(command, 'type', discord.AppCommandType.chat_input)

        type_value = cmd_type.value if hasattr(cmd_type, 'value') else 1

        data = {
            "name": command.name,
            "type": type_value,
        }

        description = getattr(command, 'description', "No description provided.")
        data["description"] = description or "No description provided."

        if cmd_type == discord.AppCommandType.chat_input:
            options = getattr(command, 'options', [])
            if options:
                data["options"] = [opt.to_dict() for opt in options]
        else:
            data["description"] = ""

        return data

    @tasks.loop(hours=24)
    async def update_dbl_commands(self):
        if not OVERRIDE_VOTEWALL:
            await self.bot.wait_until_ready()

            all_commands = self.bot.tree.get_commands()

            payload = [self.format_command(cmd) for cmd in all_commands]

            url = f"https://discordbotlist.com/api/v1/bots/{self.bot.user.id}/commands"
            headers = {
                "Authorization": f"Bot {self.dbl_token}",
                "Content-Type": "application/json"
            }

            async with aiohttp.ClientSession() as session:
                try:
                    async with session.post(url, json=payload, headers=headers) as resp:
                        if resp.status != 200:
                            text = await resp.text()
                            logger.error(f"Failed to post commands to DBL. Status: {resp.status} - {text}")
                except Exception as e:
                    logger.error(f"Error posting to DBL: {e}")

    @update_dbl_commands.before_loop
    async def before_update(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(DBLCommands(bot))