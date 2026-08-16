import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional, Set

import discord
from beacon import beacon_commands
from discord.ext import commands

from utils.data_protocol import DataDeleteResult, DataExportChunk, DataFeatureMeta, DataMonitorResult


@dataclass
class CurrentAlert:
    id: int
    title: str
    description: str
    created_at: int
    read_count: int


class Alerts(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        self._current_alert: Optional[CurrentAlert] = None
        self._read_users: Set[int] = set()
        self._reminder_cooldowns: Dict[int, float] = {}

    async def cog_load(self):
        await self.bot.db.wait_ready()
        await self.populate_caches()

    async def cog_unload(self):
        self._reminder_cooldowns.clear()

    async def populate_caches(self):
        self._read_users.clear()
        self._reminder_cooldowns.clear()

        rows = await self.bot.db.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT 1")
        if rows:
            row = rows[0]
            self._current_alert = CurrentAlert(
                id=row["id"],
                title=row["title"],
                description=row["description"],
                created_at=row["created_at"],
                read_count=row["read_count"],
            )
            read_rows = await self.bot.db.execute(
                "SELECT user_id FROM alert_reads WHERE alert_id = ?",
                (self._current_alert.id,)
            )
            self._read_users = {r["user_id"] for r in read_rows}
        else:
            self._current_alert = None

    class PushAlertModal(discord.ui.Modal, title="Push New Alert"):
        def __init__(self, parent_cog: "Alerts"):
            super().__init__()
            self.parent_cog = parent_cog
            self.alert_title = discord.ui.TextInput(label="Alert Title", max_length=256)
            self.description = discord.ui.TextInput(
                label="Description",
                style=discord.TextStyle.paragraph,
                max_length=4000
            )
            self.add_item(self.alert_title)
            self.add_item(self.description)

        async def on_submit(self, interaction: discord.Interaction) -> None:
            title = str(self.alert_title.value).strip()
            desc = str(self.description.value).strip()
            now_ts = int(datetime.now(timezone.utc).timestamp())

            await self.parent_cog.bot.db.execute("DELETE FROM alert_reads")
            await self.parent_cog.bot.db.execute("DELETE FROM alerts")
            await self.parent_cog.bot.db.execute(
                "INSERT INTO alerts (title, description, created_at, read_count) VALUES (?, ?, ?, 0)",
                (title, desc, now_ts),
            )
            id_rows = await self.parent_cog.bot.db.execute("SELECT last_insert_rowid() AS id")
            new_id = int(id_rows[0]["id"]) if id_rows else 0

            self.parent_cog._current_alert = CurrentAlert(
                id=new_id, title=title, description=desc, created_at=now_ts, read_count=0
            )
            self.parent_cog._read_users.clear()
            self.parent_cog._reminder_cooldowns.clear()

            await interaction.response.send_message("Alert pushed and cache synced successfully!", ephemeral=True)

    @beacon_commands.command(name="pa", description=".", permissions_preset="bot_owner")
    async def push_alert(self, interaction: discord.Interaction):
        await interaction.response.send_modal(self.PushAlertModal(self))

    @beacon_commands.command(name="alert", description="Read the latest alert from the developer.")
    async def alert(self, interaction: discord.Interaction):
        if not self._current_alert:
            embed = discord.Embed(
                title="No Active Alerts",
                description="There are currently no active alerts.",
                color=discord.Color(0x944ae8),
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        user_id = interaction.user.id
        alert = self._current_alert
        position: Optional[int] = None

        if user_id in self._read_users:
            pos_rows = await self.bot.db.execute(
                "SELECT position FROM alert_reads WHERE alert_id = ? AND user_id = ?",
                (alert.id, user_id)
            )
            if pos_rows:
                position = pos_rows[0]["position"]

        if position is None:
            alert.read_count += 1
            await self.bot.db.execute(
                "UPDATE alerts SET read_count = ? WHERE id = ?",
                (alert.read_count, alert.id)
            )
            position = alert.read_count
            await self.bot.db.execute(
                "INSERT INTO alert_reads (alert_id, user_id, position) VALUES (?, ?, ?)",
                (alert.id, user_id, position)
            )
            self._read_users.add(user_id)

        embed = discord.Embed(
            title=alert.title,
            description=alert.description,
            color=0xFFFFFF
        )
        embed.set_footer(text=f"You are #{position} to read this alert!")
        embed.set_author(name="Alert from the Developer")
        embed.timestamp = datetime.fromtimestamp(alert.created_at)
        await interaction.response.send_message(embed=embed)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type is not discord.InteractionType.application_command:
            return
        if not self._current_alert or interaction.user.bot:
            return

        user_id = interaction.user.id
        now = time.time()

        if user_id in self._read_users:
            return

        expiry = self._reminder_cooldowns.get(user_id)
        if expiry and expiry > now:
            return

        self._reminder_cooldowns[user_id] = now + 300.0

        async def send_reminder():
            await asyncio.sleep(4.0)
            try:
                embed = discord.Embed(
                    title="Unread Alert!",
                    description="You have an unread alert. Use </alert:1473313910571536541> to read it!",
                    color=0xFFFFFF
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
            except:
                pass

        asyncio.create_task(send_reminder())

    def data_features(self) -> list[DataFeatureMeta]:
        return [DataFeatureMeta(
            feature_id="alerts",
            name="Alerts",
            user_export=True,
            user_delete=True,
        )]

    async def data_export_user(self, user_id: int, *, guild_ids: list[int] | None) -> DataExportChunk:
        chunk = DataExportChunk(feature_id="alerts")
        rows = await self.bot.db.execute(
            "SELECT alert_id, user_id, position FROM alert_reads WHERE user_id = ?",
            (user_id,),
        )
        if rows:
            chunk.global_data["alert_reads"] = rows
        return chunk

    async def data_export_guild(self, guild_id: int) -> DataExportChunk:
        return DataExportChunk(feature_id="alerts")

    async def data_delete_user(self, user_id: int, *, guild_ids: list[int] | None, feature_id: str | None) -> DataDeleteResult:
        if feature_id and feature_id != "alerts":
            return DataDeleteResult(feature_id="alerts")
        count_rows = await self.bot.db.execute(
            "SELECT COUNT(*) AS cnt FROM alert_reads WHERE user_id = ?", (user_id,)
        )
        rows_affected = int(count_rows[0]["cnt"]) if count_rows else 0
        await self.bot.db.execute("DELETE FROM alert_reads WHERE user_id = ?", (user_id,))
        self._read_users.discard(user_id)
        return DataDeleteResult(feature_id="alerts", deleted=True, rows_affected=rows_affected)

    async def data_delete_guild(self, guild_id: int, feature_id: str | None) -> DataDeleteResult:
        return DataDeleteResult(feature_id="alerts")

    async def data_monitor_guild(self, guild: discord.Guild) -> DataMonitorResult:
        return DataMonitorResult(feature_id="alerts")


async def setup(bot: commands.Bot):
    await bot.add_cog(Alerts(bot))
