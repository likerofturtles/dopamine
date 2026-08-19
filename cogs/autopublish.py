from __future__ import annotations

import asyncio
import collections
from typing import Set

import discord
from beacon import beacon_commands
from discord import app_commands
from discord.ext import commands


class AutoPublish(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cache: Set[int] = set()
        self.publish_deque = collections.deque(maxlen=5)
        self.new_item_event = asyncio.Event()
        self.queue_task = None

    async def cog_load(self):
        await self.bot.db.wait_ready()
        rows = await self.bot.db.execute("SELECT channel_id FROM autopublish_channels")
        self.cache = {row["channel_id"] for row in rows}
        self.queue_task = asyncio.create_task(self.publish_worker())

    async def cog_unload(self):
        if self.queue_task:
            self.queue_task.cancel()

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

    autopublish_group = beacon_commands.Group(
        name="autopublish",
        description="Manage auto-publishing for announcement channels."
    )

    @autopublish_group.command(name="enable", description="Enable auto-publishing for a specific channel.")
    @app_commands.describe(channel="The announcement channel to enable auto-publish for.")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def ap_enable(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not channel.is_news():
            return await interaction.response.send_message(
                f"{channel.mention} is not an Announcement channel!", ephemeral=True
            )

        if channel.id in self.cache:
            return await interaction.response.send_message(
                f"Auto-publish is already enabled for {channel.mention}!", ephemeral=True
            )

        try:
            await self.bot.db.execute_write(
                "INSERT OR IGNORE INTO autopublish_channels (channel_id, guild_id) VALUES (?, ?)",
                (channel.id, interaction.guild.id if interaction.guild else 0)
            )
            self.cache.add(channel.id)
            await interaction.response.send_message(f"Auto-publish enabled for {channel.mention}.", ephemeral=True)
        except Exception as e:
            print(f"DB Error on enable: {e}")
            await interaction.response.send_message("A database error occurred.", ephemeral=True)

    @autopublish_group.command(name="disable", description="Disable auto-publishing for a channel.")
    @app_commands.describe(channel="The announcement channel to disable auto-publish for.")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def ap_disable(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if channel.id not in self.cache:
            return await interaction.response.send_message(
                f"Auto-publish is not enabled for {channel.mention}!", ephemeral=True
            )

        try:
            await self.bot.db.execute_write(
                "DELETE FROM autopublish_channels WHERE channel_id = ?", (channel.id,)
            )
            self.cache.discard(channel.id)
            await interaction.response.send_message(f"Auto-publish disabled for {channel.mention}.", ephemeral=True)
        except Exception as e:
            print(f"DB Error on disable: {e}")
            await interaction.response.send_message("A database error occurred.", ephemeral=True)

    def data_features(self) -> list:
        from utils.data_protocol import DataFeatureMeta
        return [DataFeatureMeta(feature_id="autopublish", name="Auto Publish", guild_export=True, guild_delete=True)]

    async def data_export_user(self, user_id: int, *, guild_ids: list[int] | None):
        from utils.data_protocol import DataExportChunk
        return DataExportChunk(feature_id="autopublish")

    async def data_export_guild(self, guild_id: int):
        from utils.data_protocol import DataExportChunk
        chunk = DataExportChunk(feature_id="autopublish")
        rows = await self.bot.db.execute(
            "SELECT channel_id FROM autopublish_channels WHERE guild_id = ?", (guild_id,)
        )
        chunk.guild_data[guild_id] = {"channels": [r["channel_id"] for r in rows]}
        return chunk

    async def data_delete_user(self, user_id: int, *, guild_ids: list[int] | None, feature_id: str | None):
        from utils.data_protocol import DataDeleteResult
        return DataDeleteResult(feature_id="autopublish")

    async def data_delete_guild(self, guild_id: int, feature_id: str | None):
        from utils.data_protocol import DataDeleteResult
        rows = await self.bot.db.execute(
            "SELECT channel_id FROM autopublish_channels WHERE guild_id = ?", (guild_id,)
        )
        cids = [r["channel_id"] for r in rows]
        await self.bot.db.execute_write("DELETE FROM autopublish_channels WHERE guild_id = ?", (guild_id,))
        for cid in cids:
            self.cache.discard(cid)
        return DataDeleteResult(feature_id="autopublish", deleted=True, rows_affected=len(cids))

    async def data_monitor_guild(self, guild: discord.Guild):
        from utils.data_protocol import DataMonitorResult
        result = DataMonitorResult(feature_id="autopublish")
        rows = await self.bot.db.execute(
            "SELECT channel_id FROM autopublish_channels WHERE guild_id = ?", (guild.id,)
        )
        cids = [r["channel_id"] for r in rows]
        for cid in cids:
            ch = guild.get_channel(cid)
            if not ch or not ch.permissions_for(guild.me).send_messages:
                await self.bot.db.execute_write("DELETE FROM autopublish_channels WHERE channel_id = ?", (cid,))
                self.cache.discard(cid)
                result.actions.append(f"removed_channel_{cid}")
        return result


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoPublish(bot))
