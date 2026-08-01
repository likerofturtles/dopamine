import asyncio
import datetime
import time

import aiosqlite
import discord
from beacon import PrivateLayoutView, beacon_commands, preconditions
from discord import app_commands
from discord.ext import commands, tasks

from config import SPDB_PATH
from utils.data_protocol import DataDeleteResult, DataExportChunk, DataFeatureMeta, DataMonitorResult


class ConfirmationView(PrivateLayoutView):
    def __init__(self, user, cog, title_text: str, body_text: str):
        super().__init__(user, timeout=120)
        self.value = None
        self.cog = cog
        self.title_text = title_text
        self.body_text = body_text
        self.color = None
        self.message: discord.Message = None
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container(accent_color=self.color)
        container.add_item(discord.ui.TextDisplay(f"### {self.title_text}"))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(self.body_text))

        if self.value is None:
            action_row = discord.ui.ActionRow()
            cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.grey)
            confirm = discord.ui.Button(label="Confirm", style=discord.ButtonStyle.green)

            cancel.callback = self.cancel_callback
            confirm.callback = self.confirm_callback

            action_row.add_item(cancel)
            action_row.add_item(confirm)
            container.add_item(discord.ui.Separator())
            container.add_item(action_row)

        self.add_item(container)

    async def update_view(self, interaction: discord.Interaction, title: str, color: discord.Color):
        self.title_text = title
        self.body_text = f"~~{self.body_text}~~"
        self.color = color
        self.build_layout()

        if interaction.response.is_done():
            await interaction.edit_original_response(view=self)
        else:
            await interaction.response.edit_message(view=self)
        self.stop()

    async def cancel_callback(self, interaction: discord.Interaction):
        self.value = False
        await self.update_view(interaction, "Action Canceled", discord.Color(0xdf5046))

    async def confirm_callback(self, interaction: discord.Interaction):
        self.value = True
        guild_id = interaction.guild_id

        self.cog.cache_settings[guild_id] = True
        await self.cog.db.execute("INSERT OR REPLACE INTO guild_settings (guild_id, enabled) VALUES (?, 1)", (guild_id,))
        await self.update_view(interaction, "Action Confirmed", discord.Color.green())

    async def on_timeout(self, interaction: discord.Interaction):
        if self.value is None and self.message:
            await self.update_view(interaction, "Timed Out", discord.Color(0xdf5046))
            self.stop()

class SQLitePool:

    def __init__(self, db_path: str, size: int = 5):
        self.db_path = db_path
        self.size = size
        self.queue = asyncio.Queue()

    async def initialize(self):
        for _ in range(self.size):
            conn = await aiosqlite.connect(self.db_path)
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.execute("PRAGMA synchronous=NORMAL;")
            await conn.commit()

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    enabled INTEGER
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS scheduled_purges (
                    guild_id INTEGER,
                    user_id INTEGER,
                    execute_at REAL,
                    PRIMARY KEY (guild_id, user_id)
                )
            """)
            await conn.commit()
            await self.queue.put(conn)

    async def execute(self, query: str, params: tuple = ()):
        conn = await self.queue.get()
        try:
            await conn.execute(query, params)
            await conn.commit()
        finally:
            self.queue.put_nowait(conn)

    async def fetchall(self, query: str, params: tuple = ()):
        conn = await self.queue.get()
        try:
            async with conn.execute(query, params) as cursor:
                return await cursor.fetchall()
        finally:
            self.queue.put_nowait(conn)

    async def close(self):
        while not self.queue.empty():
            conn = self.queue.get_nowait()
            await conn.close()


class SelfPurge(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = SQLitePool(SPDB_PATH, size=5)

        self.cache_settings = {}
        self.cache_purges = {}

    async def cog_load(self):
        await self.db.initialize()

        settings = await self.db.fetchall("SELECT guild_id, enabled FROM guild_settings")
        for guild_id, enabled in settings:
            self.cache_settings[guild_id] = bool(enabled)

        purges = await self.db.fetchall("SELECT guild_id, user_id, execute_at FROM scheduled_purges")
        for guild_id, user_id, execute_at in purges:
            self.cache_purges[(guild_id, user_id)] = execute_at

        self.purge_scheduler.start()

    async def cog_unload(self):
        self.purge_scheduler.cancel()
        await self.db.close()

    purge_group = beacon_commands.Group(name="selfpurge", description="Manage self-message purges.")

    @purge_group.command(name="disable", description="Disable self-purges for the server.")
    @preconditions.has_permissions(manage_messages=True)
    async def disable(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id

        if not self.cache_settings[guild_id]:
            return await interaction.response.send_message("Self purge feature is already disabled!", ephemeral=True)
        self.cache_settings.pop(guild_id)
        await self.db.execute("DELETE FROM guild_settings WHERE guild_id = ?", (guild_id,))

        await interaction.response.send_message("Self-purge has been disabled for this server.", ephemeral=True)

    @purge_group.command(name="enable", description="Enable self-purges for the server.")
    @preconditions.has_permissions(manage_messages=True)
    async def enable(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        if self.cache_settings.get(guild_id, False):
            return await interaction.response.send_message("Self purge feature is already enabled!", ephemeral=True)
        view = ConfirmationView(interaction.user, cog=self, title_text="Pending Confirmation", body_text="""**Are you sure you want to enable Self Purge feature?** This allows members without mod permissions to delete their own messages less than 14 days old using `/selfpurge start` that starts a 24-hour buffer period before deleting the messages.\n\nDopamine is NOT responsible for: Malicious use of this feature, lost evidence or messages, harassment, etc.\n\nBy enabling this feature, you agree that you and your staff team are responsible for providing users with the option to bulk delete their own messages by enabling this opt-in feature, accepting that you have the control to cancel any user's scheduled deletion using `/selfpurge modcancel`, and clicking the "Confirm" button.""")
        await interaction.response.send_message(view=view)

    @purge_group.command(name="start", description="Schedule a purge of your messages to happen in 24 hours.")
    async def start(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id

        if not self.cache_settings.get(guild_id, False):
            return await interaction.response.send_message(
                "The `/selfpurge` feature is currently disabled on this server.", ephemeral=True)

        user_id = interaction.user.id
        execute_time = time.time() + 86400

        self.cache_purges[(guild_id, user_id)] = execute_time
        await self.db.execute(
            "INSERT OR REPLACE INTO scheduled_purges (guild_id, user_id, execute_at) VALUES (?, ?, ?)",
            (guild_id, user_id, execute_time))

        await interaction.response.send_message(
            f"{interaction.user.mention} has scheduled a self-message purge. All of their messages less than 14 days old will be deleted after a 24-hour buffer period.\n\nIf you're a staff member and want to cancel this, use `/selfpurge modcancel`."
        )

    @purge_group.command(name="cancel", description="Cancel your scheduled self-message purge.")
    async def cancel(self, interaction: discord.Interaction):
        key = (interaction.guild_id, interaction.user.id)

        if key in self.cache_purges:
            del self.cache_purges[key]
            await self.db.execute("DELETE FROM scheduled_purges WHERE guild_id=? AND user_id=?", key)
            await interaction.response.send_message("Your scheduled purge has been cancelled.", ephemeral=True)
        else:
            await interaction.response.send_message("You don't have a scheduled purge to cancel.", ephemeral=True)

    @purge_group.command(name="modcancel", description="[Mod] Cancel a specific member's scheduled purge.")
    @app_commands.describe(member="The member whose scheduled purge you want to cancel.")
    @preconditions.has_permissions(manage_messages=True)
    async def modcancel(self, interaction: discord.Interaction, member: discord.Member):
        guild_id = interaction.guild_id
        user_id = member.id
        key = (guild_id, user_id)

        if key not in self.cache_purges:
            return await interaction.response.send_message(
                f"{member.display_name} does not have a scheduled purge in this server.",
                ephemeral=True
            )

        del self.cache_purges[key]
        await self.db.execute(
            "DELETE FROM scheduled_purges WHERE guild_id=? AND user_id=?",
            (guild_id, user_id)
        )

        await interaction.response.send_message(
            f"Successfully cancelled the scheduled purge for {member.mention}.",
            ephemeral=True
        )

        try:
            embed = discord.Embed(
                title="Scheduled Purge Cancelled",
                description=f"Your scheduled message purge in **{interaction.guild.name}** has been cancelled by a staff member.",
                color=discord.Color.red(),
                timestamp=discord.utils.utcnow()
            )
            await member.send(embed=embed)
        except discord.Forbidden:
            await interaction.followup.send(
                f"Note: Could not send a DM to {member.mention} (DMs closed).",
                ephemeral=True
            )

    @tasks.loop(minutes=1)
    async def purge_scheduler(self):
        now = time.time()
        to_execute = []

        for (guild_id, user_id), execute_at in list(self.cache_purges.items()):
            if now >= execute_at:
                to_execute.append((guild_id, user_id))

        for guild_id, user_id in to_execute:
            if (guild_id, user_id) in self.cache_purges:
                del self.cache_purges[(guild_id, user_id)]
            await self.db.execute("DELETE FROM scheduled_purges WHERE guild_id=? AND user_id=?", (guild_id, user_id))

            asyncio.create_task(self.execute_purge(guild_id, user_id))

    async def execute_purge(self, guild_id: int, user_id: int):
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return

        fourteen_days_ago = discord.utils.utcnow() - datetime.timedelta(days=14)

        for channel in guild.text_channels:
            try:
                perms = channel.permissions_for(guild.me)
                if not perms.read_message_history or not perms.manage_messages:
                    continue

                messages_to_delete = []
                async for msg in channel.history(limit=None, after=fourteen_days_ago):
                    if msg.author.id == user_id:
                        messages_to_delete.append(msg)

                        if len(messages_to_delete) == 100:
                            await channel.delete_messages(messages_to_delete)
                            messages_to_delete = []
                            await asyncio.sleep(2)

                if len(messages_to_delete) > 1:
                    await channel.delete_messages(messages_to_delete)

            except discord.Forbidden:
                continue
            except discord.HTTPException:
                continue

    def data_features(self) -> list[DataFeatureMeta]:
        return [DataFeatureMeta(
            feature_id="selfpurge",
            name="SelfPurge",
            user_export=True,
            user_delete=True,
            guild_export=True,
            guild_delete=True,
        )]

    async def _fetch_dicts(self, query: str, params: tuple = ()) -> list[dict]:
        conn = await self.db.queue.get()
        try:
            async with conn.execute(query, params) as cur:
                rows = await cur.fetchall()
                if not cur.description:
                    return []
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in rows]
        finally:
            self.db.queue.put_nowait(conn)

    async def data_export_user(self, user_id: int, *, guild_ids: list[int] | None) -> DataExportChunk:
        chunk = DataExportChunk(feature_id="selfpurge")
        if guild_ids is None:
            rows = await self._fetch_dicts(
                "SELECT guild_id, user_id, execute_at FROM scheduled_purges WHERE user_id = ?",
                (user_id,),
            )
        else:
            placeholders = ",".join("?" * len(guild_ids))
            rows = await self._fetch_dicts(
                f"SELECT guild_id, user_id, execute_at FROM scheduled_purges WHERE user_id = ? AND guild_id IN ({placeholders})",
                (user_id, *guild_ids),
            )
        for row in rows:
            gid = row.pop("guild_id")
            chunk.guild_data.setdefault(gid, {}).setdefault("scheduled_purges", []).append(row)
        return chunk

    async def data_export_guild(self, guild_id: int) -> DataExportChunk:
        settings = await self._fetch_dicts(
            "SELECT guild_id, enabled FROM guild_settings WHERE guild_id = ?", (guild_id,))
        purges = await self._fetch_dicts(
            "SELECT guild_id, user_id, execute_at FROM scheduled_purges WHERE guild_id = ?", (guild_id,))
        chunk = DataExportChunk(feature_id="selfpurge")
        chunk.guild_data[guild_id] = {"settings": settings, "scheduled_purges": purges}
        return chunk

    async def data_delete_user(self, user_id: int, *, guild_ids: list[int] | None, feature_id: str | None) -> DataDeleteResult:
        if feature_id and feature_id != "selfpurge":
            return DataDeleteResult(feature_id="selfpurge")
        rows_affected = 0
        if guild_ids is None:
            await self.db.execute("DELETE FROM scheduled_purges WHERE user_id = ?", (user_id,))
            keys = [(g, u) for (g, u) in self.cache_purges if u == user_id]
        else:
            for gid in guild_ids:
                await self.db.execute(
                    "DELETE FROM scheduled_purges WHERE guild_id = ? AND user_id = ?", (gid, user_id))
            keys = [(gid, user_id) for gid in guild_ids if (gid, user_id) in self.cache_purges]
        for key in keys:
            self.cache_purges.pop(key, None)
        rows_affected = len(keys)
        return DataDeleteResult(feature_id="selfpurge", deleted=True, rows_affected=rows_affected)

    async def data_delete_guild(self, guild_id: int, feature_id: str | None) -> DataDeleteResult:
        if feature_id and feature_id != "selfpurge":
            return DataDeleteResult(feature_id="selfpurge")
        purge_keys = [k for k in self.cache_purges if k[0] == guild_id]
        for key in purge_keys:
            self.cache_purges.pop(key, None)
        self.cache_settings.pop(guild_id, None)
        await self.db.execute("DELETE FROM scheduled_purges WHERE guild_id = ?", (guild_id,))
        await self.db.execute("DELETE FROM guild_settings WHERE guild_id = ?", (guild_id,))
        return DataDeleteResult(
            feature_id="selfpurge", deleted=True, rows_affected=len(purge_keys) + 1)

    async def data_monitor_guild(self, guild: discord.Guild) -> DataMonitorResult:
        return DataMonitorResult(feature_id="selfpurge")


async def setup(bot):
    await bot.add_cog(SelfPurge(bot))