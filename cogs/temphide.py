import asyncio
import codecs
import time
from typing import Optional, Dict, List, Any

import discord
from beacon import beacon_commands
from discord.ext import commands

from utils.data_handlers import export_table
from utils.data_protocol import DataDeleteResult, DataExportChunk, DataFeatureMeta, DataMonitorResult


class TempHideCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.message_cache: Dict[int, dict] = {}

    async def cog_load(self):
        await self.bot.db.wait_ready()
        await self.populate_caches()
        self.bot.add_view(RevealView(self, 0))

    async def populate_caches(self):
        self.message_cache.clear()
        rows = await self.bot.db.execute("SELECT * FROM temp_messages")
        for data in rows:
            self.message_cache[data["message_id"]] = data

    async def store_message(self, user_id: int, hidden_text: str, message_id: int, timestamp: float):
        data = {
            "message_id": message_id,
            "user_id": user_id,
            "hidden_text": hidden_text,
            "timestamp": timestamp
        }
        await self.bot.db.execute(
            'INSERT INTO temp_messages (message_id, user_id, hidden_text, timestamp) VALUES (?, ?, ?, ?)',
            (message_id, user_id, hidden_text, timestamp)
        )
        self.message_cache[message_id] = data

    async def delete_message(self, message_id: int):
        await self.bot.db.execute('DELETE FROM temp_messages WHERE message_id = ?', (message_id,))
        self.message_cache.pop(message_id, None)

    async def get_message(self, message_id: int) -> Optional[tuple[int, str]]:
        data = self.message_cache.get(message_id)
        if data:
            return (data["user_id"], data["hidden_text"])
        return None

    async def _resolve_message_guild_id(self, message_id: int) -> Optional[int]:
        for guild in self.bot.guilds:
            for channel in guild.text_channels:
                if not channel.permissions_for(guild.me).view_channel:
                    continue
                try:
                    await channel.fetch_message(message_id)
                    return guild.id
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    continue
        return None

    def data_features(self) -> list[DataFeatureMeta]:
        return [DataFeatureMeta(
            feature_id="temphide",
            name="TempHide",
            user_export=True,
            user_delete=True,
            guild_export=True,
            guild_delete=True,
        )]

    async def data_export_user(self, user_id: int, *, guild_ids: list[int] | None) -> DataExportChunk:
        chunk = DataExportChunk(feature_id="temphide")
        rows = await export_table(
            self.bot.db, "SELECT * FROM temp_messages WHERE user_id = ?", (user_id,))
        for row in rows:
            gid = await self._resolve_message_guild_id(row["message_id"])
            if gid is None:
                chunk.global_data.setdefault("messages", []).append(row)
            elif guild_ids is None or gid in guild_ids:
                chunk.guild_data.setdefault(gid, {}).setdefault("messages", []).append(row)
        return chunk

    async def data_export_guild(self, guild_id: int) -> DataExportChunk:
        chunk = DataExportChunk(feature_id="temphide")
        messages = []
        rows = await export_table(self.bot.db, "SELECT * FROM temp_messages", ())
        for row in rows:
            gid = await self._resolve_message_guild_id(row["message_id"])
            if gid == guild_id:
                messages.append(row)
        chunk.guild_data[guild_id] = {"messages": messages}
        return chunk

    async def data_delete_user(self, user_id: int, *, guild_ids: list[int] | None,
                               feature_id: str | None) -> DataDeleteResult:
        if feature_id and feature_id != "temphide":
            return DataDeleteResult(feature_id="temphide")
        rows_affected = 0
        if guild_ids is None:
            rows_affected = await self.bot.db.execute(
                "DELETE FROM temp_messages WHERE user_id = ?", (user_id,))
            for mid, data in list(self.message_cache.items()):
                if data["user_id"] == user_id:
                    del self.message_cache[mid]
        else:
            rows = await export_table(
                self.bot.db, "SELECT message_id FROM temp_messages WHERE user_id = ?", (user_id,))

            async with self.bot.db.acquire_db() as db:
                for row in rows:
                    gid = await self._resolve_message_guild_id(row["message_id"])
                    if gid not in guild_ids:
                        continue
                    await db.execute(
                        "DELETE FROM temp_messages WHERE message_id = ?", (row["message_id"],))
                    self.message_cache.pop(row["message_id"], None)
                    rows_affected += 1
                await db.commit()

        return DataDeleteResult(feature_id="temphide", deleted=True, rows_affected=rows_affected)

    async def data_delete_guild(self, guild_id: int, feature_id: str | None) -> DataDeleteResult:
        if feature_id and feature_id != "temphide":
            return DataDeleteResult(feature_id="temphide")
        rows_affected = 0
        rows = await export_table(self.bot.db, "SELECT message_id FROM temp_messages", ())

        async with self.bot.db.acquire_db() as db:
            for row in rows:
                gid = await self._resolve_message_guild_id(row["message_id"])
                if gid != guild_id:
                    continue
                await db.execute(
                    "DELETE FROM temp_messages WHERE message_id = ?", (row["message_id"],))
                self.message_cache.pop(row["message_id"], None)
                rows_affected += 1
            await db.commit()

        return DataDeleteResult(feature_id="temphide", deleted=True, rows_affected=rows_affected)

    async def data_monitor_guild(self, guild: discord.Guild) -> DataMonitorResult:
        return DataMonitorResult(feature_id="temphide")

    @staticmethod
    async def send_error_reply(interaction_or_ctx, embed=None, message=None, ephemeral=True):
        try:
            if hasattr(interaction_or_ctx, 'response') and not interaction_or_ctx.response.is_done():
                if embed:
                    await interaction_or_ctx.response.send_message(embed=embed, ephemeral=ephemeral)
                else:
                    await interaction_or_ctx.response.send_message(message, ephemeral=ephemeral)
            elif hasattr(interaction_or_ctx, 'send'):
                if embed:
                    await interaction_or_ctx.send(embed=embed)
                else:
                    await interaction_or_ctx.send(message)
            else:
                if embed:
                    await interaction_or_ctx.followup.send(embed=embed, ephemeral=ephemeral)
                else:
                    await interaction_or_ctx.followup.send(message, ephemeral=ephemeral)
        except Exception:
            pass

    async def handle_temphide(self, interaction_or_ctx, message_text: str):
        is_slash = hasattr(interaction_or_ctx, 'response')
        user = interaction_or_ctx.user if is_slash else interaction_or_ctx.author
        channel = interaction_or_ctx.channel

        if len(message_text.split()) > 1000:
            embed = discord.Embed(title="Message Too Long", description="Max 1000 words.", color=discord.Color.red())
            await self.send_error_reply(interaction_or_ctx, embed=embed)
            return

        current_time = time.time()
        encoded = await asyncio.to_thread(codecs.encode, message_text, 'rot13')
        view = RevealView(self, 0)

        try:
            content = f"{user.name}: {encoded}"
            sent_message = await interaction_or_ctx.followup.send(content,
                                                                  view=view) if is_slash else await channel.send(
                content, view=view)

            view.message_id = sent_message.id
            await self.store_message(user.id, message_text, sent_message.id, current_time)

            if is_slash:
                await interaction_or_ctx.followup.send("Hidden message created!", ephemeral=True)
        except Exception:
            embed = discord.Embed(title="Error", description="Failed to create message.", color=discord.Color.red())
            await self.send_error_reply(interaction_or_ctx, embed=embed)

    @beacon_commands.command(name="temphide", description="Send a hidden message that only you can reveal")
    async def temphide_slash(self, interaction: discord.Interaction, message: str):
        await self.handle_temphide(interaction, message)


class RevealView(discord.ui.View):
    def __init__(self, cog: TempHideCog, message_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.message_id = message_id

    @discord.ui.button(label='Reveal', style=discord.ButtonStyle.primary, custom_id='reveal_button')
    async def reveal_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        message_data = await self.cog.get_message(self.message_id)

        if not message_data:
            return await interaction.response.send_message("Already revealed or expired.", ephemeral=True)

        user_id, hidden_text = message_data
        if interaction.user.id != user_id:
            return await interaction.response.send_message("Not your message!", ephemeral=True)

        await interaction.response.defer()
        try:
            await interaction.message.edit(content=f"{interaction.user.name}: {hidden_text}", view=None)
            await self.cog.delete_message(self.message_id)
        except discord.NotFound:
            await self.cog.delete_message(self.message_id)
        except Exception:
            pass


async def setup(bot):
    await bot.add_cog(TempHideCog(bot))