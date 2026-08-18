import asyncio

import aiohttp
from discord.ext import commands, tasks

from config import HEARTBEAT_URL


class StatusHeartbeat(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.heartbeat_url = HEARTBEAT_URL
        self.send_heartbeat.start()

    def cog_unload(self):
        self.send_heartbeat.cancel()

    @tasks.loop(minutes=1.0)
    async def send_heartbeat(self):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(self.heartbeat_url) as response:
                    if response.status != 200:
                        if hasattr(self.bot, 'logger') and self.bot.logger:
                            self.bot.logger.warning(f"Heartbeat HTTP status: {response.status}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if hasattr(self.bot, 'logger') and self.bot.logger:
                self.bot.logger.warning(f"Heartbeat failed (network offline): {e}")
        except Exception as e:
            if hasattr(self.bot, 'logger') and self.bot.logger:
                self.bot.logger.error(f"Unexpected heartbeat error: {e}")

    @send_heartbeat.before_loop
    async def before_heartbeat(self):
        await self.bot.wait_until_ready()

async def setup(bot):
    await bot.add_cog(StatusHeartbeat(bot))