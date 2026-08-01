import asyncio
import collections
from typing import Set

import aiosqlite
import discord
from beacon import beacon_commands
from discord import app_commands
from discord.ext import commands

from config import APDB_PATH


class ConnectionPool:

    def __init__(self, db_path: str, max_connections: int = 5):
        self.db_path = db_path
        self.max_connections = max_connections
        self.queue = asyncio.Queue(maxsize=max_connections)
        self.connections = []

    async def init_pool(self):
        for _ in range(self.max_connections):
            conn = await aiosqlite.connect(self.db_path)
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.execute("PRAGMA synchronous=NORMAL;")
            await conn.execute("PRAGMA busy_timeout=5000;")
            await conn.commit()

            self.connections.append(conn)
            await self.queue.put(conn)

    async def acquire(self) -> aiosqlite.Connection:
        return await self.queue.get()

    async def release(self, conn: aiosqlite.Connection):
        await self.queue.put(conn)

    async def close(self):
        for conn in self.connections:
            await conn.close()


class AutoPublish(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.pool = ConnectionPool(APDB_PATH, max_connections=5)
        self.cache: Set[int] = set()
        self.publish_deque = collections.deque(maxlen=5)
        self.new_item_event = asyncio.Event()
        self.queue_task = None

    async def cog_load(self):
        await self.pool.init_pool()

        conn = await self.pool.acquire()
        try:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS autopublish_channels (
                    channel_id INTEGER PRIMARY KEY,
                    guild_id INTEGER
                )
            """)
            await conn.commit()

            async with conn.execute("SELECT channel_id FROM autopublish_channels") as cursor:
                rows = await cursor.fetchall()
                self.cache = {row[0] for row in rows}
        finally:
            await self.pool.release(conn)
        self.queue_task = asyncio.create_task(self.publish_worker())

    async def cog_unload(self):
        if self.queue_task:
            self.queue_task.cancel()
        await self.pool.close()

    async def publish_worker(self):
        DELAY_BETWEEN_PUBLISHES = 365

        while True:
            if not self.publish_deque:
                self.new_item_event.clear()
                await self.new_item_event.wait()

            message = self.publish_deque.popleft()

            try:
                await message.publish()
                await asyncio.sleep(DELAY_BETWEEN_PUBLISHES)
            except discord.HTTPException as e:
                if e.status == 429:
                    await asyncio.sleep(3600)
                else:
                    from utils.discord_health import is_access_error, report_access_failure
                    if is_access_error(e) and message.guild:
                        await report_access_failure(
                            self.bot, message.guild.id, "autopublish", str(message.channel.id)
                        )
            except Exception:
                pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.id == self.bot.user.id:
            return

        if message.channel.id not in self.cache:
            return

        if message.channel.type == discord.ChannelType.news:
            self.publish_deque.append(message)

            self.new_item_event.set()

    autopublish_group = beacon_commands.Group(name="autopublish",
                                           description="Manage auto-publishing for announcement channels.")

    @autopublish_group.command(name="enable", description="Enable auto-publishing for a specific channel.")
    @app_commands.describe(channel="The announcement channel to enable auto-publish for.")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def ap_enable(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not channel.is_news():
            return await interaction.response.send_message(f"{channel.mention} is not an Announcement channel!",
                                                           ephemeral=True)

        if channel.id in self.cache:
            return await interaction.response.send_message(f"Auto-publish is already enabled for {channel.mention}!",
                                                           ephemeral=True)

        conn = await self.pool.acquire()
        try:
            await conn.execute(
                "INSERT OR IGNORE INTO autopublish_channels (channel_id, guild_id) VALUES (?, ?)",
                (channel.id, interaction.guild.id)
            )
            await conn.commit()

            self.cache.add(channel.id)

            await interaction.response.send_message(f"Auto-publish enabled for {channel.mention}.", ephemeral=True)
        except Exception as e:
            print(f"DB Error on enable: {e}")
            await interaction.response.send_message("A database error occurred.", ephemeral=True)
        finally:
            await self.pool.release(conn)

    @autopublish_group.command(name="disable", description="Disable auto-publishing for a channel.")
    @app_commands.describe(channel="The announcement channel to disable auto-publish for.")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def ap_disable(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if channel.id not in self.cache:
            return await interaction.response.send_message(f"Auto-publish is not enabled for {channel.mention}!",
                                                           ephemeral=True)

        conn = await self.pool.acquire()
        try:
            await conn.execute("DELETE FROM autopublish_channels WHERE channel_id = ?", (channel.id,))
            await conn.commit()

            self.cache.discard(channel.id)

            await interaction.response.send_message(f"Auto-publish disabled for {channel.mention}.", ephemeral=True)
        except Exception as e:
            print(f"DB Error on disable: {e}")
            await interaction.response.send_message("A database error occurred.", ephemeral=True)
        finally:
            await self.pool.release(conn)


    def data_features(self) -> list:
        from utils.data_protocol import DataFeatureMeta
        return [DataFeatureMeta(feature_id="autopublish", name="Auto Publish", guild_export=True, guild_delete=True)]

    async def data_export_user(self, user_id: int, *, guild_ids: list[int] | None):
        from utils.data_protocol import DataExportChunk
        return DataExportChunk(feature_id="autopublish")

    async def data_export_guild(self, guild_id: int):
        from utils.data_protocol import DataExportChunk
        chunk = DataExportChunk(feature_id="autopublish")
        conn = await self.pool.acquire()
        try:
            async with conn.execute(
                "SELECT channel_id FROM autopublish_channels WHERE guild_id = ?", (guild_id,)
            ) as cur:
                rows = await cur.fetchall()
            chunk.guild_data[guild_id] = {"channels": [r[0] for r in rows]}
        finally:
            await self.pool.release(conn)
        return chunk

    async def data_delete_user(self, user_id: int, *, guild_ids: list[int] | None, feature_id: str | None):
        from utils.data_protocol import DataDeleteResult
        return DataDeleteResult(feature_id="autopublish")

    async def data_delete_guild(self, guild_id: int, feature_id: str | None):
        from utils.data_protocol import DataDeleteResult
        conn = await self.pool.acquire()
        try:
            async with conn.execute(
                "SELECT channel_id FROM autopublish_channels WHERE guild_id = ?", (guild_id,)
            ) as cur:
                cids = [r[0] for r in await cur.fetchall()]
            cur = await conn.execute("DELETE FROM autopublish_channels WHERE guild_id = ?", (guild_id,))
            await conn.commit()
        finally:
            await self.pool.release(conn)
        for cid in cids:
            self.cache.discard(cid)
        return DataDeleteResult(feature_id="autopublish", deleted=True, rows_affected=cur.rowcount)

    async def data_monitor_guild(self, guild: discord.Guild):
        from utils.data_protocol import DataMonitorResult
        result = DataMonitorResult(feature_id="autopublish")
        conn = await self.pool.acquire()
        try:
            async with conn.execute(
                "SELECT channel_id FROM autopublish_channels WHERE guild_id = ?", (guild.id,)
            ) as cur:
                cids = [r[0] for r in await cur.fetchall()]
            for cid in cids:
                ch = guild.get_channel(cid)
                if not ch or not ch.permissions_for(guild.me).send_messages:
                    await conn.execute("DELETE FROM autopublish_channels WHERE channel_id = ?", (cid,))
                    self.cache.discard(cid)
                    result.actions.append(f"removed_channel_{cid}")
            await conn.commit()
        finally:
            await self.pool.release(conn)
        return result


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoPublish(bot))