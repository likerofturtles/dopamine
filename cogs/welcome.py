import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
import asyncio
from typing import Optional, Dict
from contextlib import asynccontextmanager

from config import WDB_PATH
from utils.checks import slash_mod_check


class Welcome(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.welcome_cache: Dict[int, dict] = {}
        self.db_pool: Optional[asyncio.Queue] = None

    async def cog_load(self):
        await self.init_pools()
        await self.init_db()
        await self.populate_caches()

    async def init_pools(self, pool_size: int = 5):
        if self.db_pool is None:
            self.db_pool = asyncio.Queue(maxsize=pool_size)
            for _ in range(pool_size):
                conn = await aiosqlite.connect(
                    WDB_PATH,
                    timeout=5,
                )
                await conn.execute("PRAGMA busy_timeout=5000")
                await conn.execute("PRAGMA journal_mode=WAL")
                await conn.execute("PRAGMA synchronous = NORMAL")
                await conn.commit()
                await self.db_pool.put(conn)

    @asynccontextmanager
    async def acquire_db(self):
        conn = await self.db_pool.get()
        try:
            yield conn
        finally:
            await self.db_pool.put(conn)

    async def init_db(self):
        async with self.acquire_db() as db:
            await db.execute('''
                             CREATE TABLE IF NOT EXISTS welcome_settings
                             (
                                 guild_id INTEGER PRIMARY KEY,
                                 channel_id INTEGER,
                                 custom_message TEXT,
                                 show_image INTEGER DEFAULT 0,
                                 image_url TEXT,
                                 embed_color TEXT
                             )
                             ''')

    async def populate_caches(self):
        self.welcome_cache.clear()
        async with self.acquire_db() as db:
            async with db.execute("SELECT * FROM welcome_settings") as cursor:
                rows = await cursor.fetchall()
                columns = [column[0] for column in cursor.description]
                for row in rows:
                    data = dict(zip(columns, row))
                    self.welcome_cache[data["guild_id"]] = data

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        data = self.welcome_cache.get(member.guild.id)
        if not data or not data.get("channel_id"):
            return

        channel = member.guild.get_channel(data["channel_id"])
        if channel:
            msg = data.get("custom_message") or f"Welcome to **{member.guild.name}**, {member.mention}!"
            try:
                await channel.send(msg)
            except discord.Forbidden:
                pass

    welcome = app_commands.Group(name="welcome", description="Welcome feature's commands.")
    @welcome.command(name="toggle", description="Enable/disable welcome messages or set the target channel.")
    @app_commands.check(slash_mod_check)
    @app_commands.describe(channel="Toggle welcome messages feature. Include channel to enable; leave blank to disable.")
    async def welcome(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        guild_id = interaction.guild.id

        if channel:
            async with self.acquire_db() as db:
                await db.execute("""
                                 INSERT INTO welcome_settings (guild_id, channel_id)
                                 VALUES (?, ?) ON CONFLICT(guild_id) DO
                                 UPDATE SET channel_id=excluded.channel_id
                                 """, (guild_id, channel.id))

            if guild_id not in self.welcome_cache:
                self.welcome_cache[guild_id] = {"guild_id": guild_id}
            self.welcome_cache[guild_id]["channel_id"] = channel.id

            embed = discord.Embed(
                description=f"Welcome messages have been **enabled**, sending them to {channel.mention}.",
                color=discord.Color(0x337fd5)
            )
        else:
            async with self.acquire_db() as db:
                await db.execute("DELETE FROM welcome_settings WHERE guild_id = ?", (guild_id,))
            self.welcome_cache.pop(guild_id, None)

            embed = discord.Embed(
                description="Welcome messages have been **disabled**.",
                color=discord.Color(0x337fd5)
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        embed = discord.Embed(
            description=(
                "### Thank you for inviting me!\n\n"
                "I'm a point-based moderation and utility bot. The moderation system is inspired by the core functionality of the moderation bot in the **teenserv** Discord server ([**__discord.gg/teenserv__**](https://www.discord.gg/teenserv)).\n\n"
                "**Use `/help` to get started! ^_^**\n\n"
                "-# [**__Vote__**](https://top.gg/bot/1411266382380924938/vote) • [**__Support Server__**](https://discord.gg/VWDcymz648)"
            ),
            color=discord.Color.purple()
        )

        embed.set_author(
            name="Dopamine — Advanced point-based Moderation Bot",
            icon_url=self.bot.user.display_avatar.url
        )

        target_channel = None
        keywords = ["general", "chat", "lounge"]
        for channel in guild.text_channels:
            if any(word in channel.name.lower() for word in keywords):
                if channel.permissions_for(guild.me).send_messages:
                    target_channel = channel
                    break

        if not target_channel:
            if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
                target_channel = guild.system_channel

        if not target_channel:
            for channel in guild.text_channels:
                if channel.permissions_for(guild.me).send_messages:
                    target_channel = channel
                    break

        if target_channel:
            await target_channel.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Welcome(bot))