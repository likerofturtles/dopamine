import discord
from beacon import beacon_commands
from discord import app_commands
from discord.ext import commands


class BanningCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.banned_users_cache: set[int] = set()
        self.banned_guilds_cache: set[int] = set()

        self.bot.tree.interaction_check = self.global_ban_check

    async def cog_load(self):
        await self.bot.db.wait_ready()

        rows = await self.bot.db.execute("SELECT user_id FROM banned_users")
        for row in rows:
            self.banned_users_cache.add(row["user_id"])

        rows = await self.bot.db.execute("SELECT guild_id FROM banned_guilds")
        for row in rows:
            self.banned_guilds_cache.add(row["guild_id"])

    async def cog_unload(self):
        self.bot.tree.interaction_check = None

    async def ban_user_api(self, user_id: int, reason: str | None = None) -> bool:
        if user_id in self.banned_users_cache:
            return False

        self.banned_users_cache.add(user_id)
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO banned_users (user_id, reason) VALUES (?, ?)", (user_id, reason)
        )
        return True

    async def ban_guild_api(self, guild_id: int, reason: str | None = None) -> bool:
        if guild_id in self.banned_guilds_cache:
            return False

        self.banned_guilds_cache.add(guild_id)
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO banned_guilds (guild_id, reason) VALUES (?, ?)", (guild_id, reason)
        )

        guild = self.bot.get_guild(guild_id) or await self.bot.fetch_guild(guild_id)
        if guild:
            await guild.leave()

        return True

    async def global_ban_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id and interaction.guild_id in self.banned_guilds_cache:
            rows = await self.bot.db.execute(
                "SELECT reason FROM banned_guilds WHERE guild_id = ?", (interaction.guild_id,)
            )
            reason = rows[0]["reason"] if rows else "No reason provided."
            await interaction.response.send_message(
                f"This server is banned from using Dopamine for the reason given below. I will now leave the server. If you have any questions, email **Dopamine Studios** at dopaminediscordbot@gmail.com.\n\n**Reason:** {reason}"
            )
            if interaction.guild:
                await interaction.guild.leave()
            return False

        if interaction.user.id in self.banned_users_cache:
            rows = await self.bot.db.execute(
                "SELECT reason FROM banned_users WHERE user_id = ?", (interaction.user.id,)
            )
            reason = rows[0]["reason"] if rows else "No reason provided."
            await interaction.response.send_message(
                f"You are banned from using Dopamine for the reason given below. If you have any questions, email **Dopamine Studios** at dopaminediscordbot@gmail.com.",
            )
            return False

        return True

    @beacon_commands.command(name="dub", description=".", permissions_preset="bot_owner")
    @app_commands.describe(user_id="The ID of the user to ban", reason="The reason for the ban")
    async def devuserban(self, interaction: discord.Interaction, user_id: str, reason: str):
        try:
            target_id = int(user_id)
        except ValueError:
            return await interaction.response.send_message("Invalid ID format.", ephemeral=True)

        success = await self.ban_user_api(target_id, reason)
        if success:
            await interaction.response.send_message(f"✅ User `{target_id}` has been banned.", ephemeral=True)
            user = self.bot.get_user(target_id) or await self.bot.fetch_user(target_id)
            try:
                await user.send(
                    f"You have been **banned** from using Dopamine.\n\n**Reason:** {reason}\n-# If you have any questions, email Dopamine Studios at dopaminediscordbot@gmail.com.")
            except discord.Forbidden:
                pass
        else:
            await interaction.response.send_message(f"⚠️ User `{target_id}` is already banned.", ephemeral=True)

    @beacon_commands.command(name="dgb", description=".", permissions_preset="bot_owner")
    @app_commands.describe(guild_id="Select a guild to ban", reason="The reason for the ban")
    async def devguildban(self, interaction: discord.Interaction, guild_id: str, reason: str):
        try:
            target_id = int(guild_id)
        except ValueError:
            return await interaction.response.send_message("Invalid ID format.", ephemeral=True)

        success = await self.ban_guild_api(target_id, reason)
        if success:
            await interaction.response.send_message(
                f"✅ Guild `{target_id}` has been banned. The bot will leave if present.", ephemeral=True)
        else:
            await interaction.response.send_message(f"⚠️ Guild `{target_id}` is already banned.", ephemeral=True)

    @devguildban.autocomplete('guild_id')
    async def devguildban_autocomplete(self, interaction: discord.Interaction, current: str) -> list[
        app_commands.Choice[str]]:
        choices = []
        for guild in self.bot.guilds:
            if guild.id in self.banned_guilds_cache:
                continue
            if current.lower() in guild.name.lower():
                choices.append(app_commands.Choice(name=f"{guild.name} ({guild.id})", value=str(guild.id)))

        return choices[:25]


async def setup(bot: commands.Bot):
    await bot.add_cog(BanningCog(bot))