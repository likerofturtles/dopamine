from __future__ import annotations

import discord
from discord.ext import commands, tasks


class StatusCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.index: int = 0
        self.change_status.start()

    def cog_unload(self) -> None:
        self.change_status.cancel()

    async def get_stats(self) -> list[str]:
        if not self.bot.application:
            await self.bot.application_info()

        guild_count = len(self.bot.guilds)
        user_installs = self.bot.application.approximate_user_install_count or 0
        total_members = sum(guild.member_count for guild in self.bot.guilds if guild.member_count)

        return [
            "✨ Geometry DASH",
            f"✨ Watching {guild_count} Servers",
            "✨ Watching downfall of GiveawayBot",
            f"✨ Watching {user_installs} User-installs",
            "✨ A charity case?",
            "✨ i got the best moderation bro",
            "✨ Am the open source underdog",
            "✨ boy are you a dopamine? cuz i wanna make you dopaMINE!",
            "✨ Powered by Beacon Framework!",
            f"✨ Watching {total_members} Members",
            "✨ Watching downfall of GiveawayBoat",
            "✨ girl are you dopamine? cuz damn youre dopaFINE!",
            "✨ It's so hard being the best!",
            "✨ moderator? i barely know her",
            "✨ Dash-da-da, dash-da-da, dash-da, like it's magnetic",
            "✨ Giving Sapphire a hug (aww!)",
        ]

    @tasks.loop(seconds=30)
    async def change_status(self) -> None:
        statuses = await self.get_stats()

        current_text = statuses[self.index]

        activity = discord.Streaming(
            name=current_text,
            url="https://www.twitch.tv/dopaminediscordbot"
        )

        await self.bot.change_presence(activity=activity)

        self.index = (self.index + 1) % len(statuses)

    @change_status.before_loop
    async def before_status_loop(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StatusCog(bot))
