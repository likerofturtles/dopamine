import discord
from discord.ext import commands, tasks
from discord import app_commands
from discord.ui import Modal, TextInput
import aiosqlite
import asyncio
import time
import re
from typing import Optional, List, Dict, Tuple, Any, AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime
from config import SMDB_PATH
from utils.checks import slash_mod_check


class ScheduledMessageSetupModal(Modal):
    def __init__(self, cog: "ScheduledMessages", interaction: discord.Interaction, channel: discord.TextChannel):
        super().__init__(title="Create Scheduled Message")
        self.cog = cog
        self.interaction = interaction
        self.channel = channel

        self.name_input = TextInput(
            label="Name",
            placeholder="Daily Reminder",
            max_length=50,
            required=True,
            style=discord.TextStyle.short,
        )
        self.frequency_input = TextInput(
            label="Frequency (Minimum 60 Sec.)",
            placeholder="2w 7d 8hr 11m",
            max_length=50,
            required=True,
            style=discord.TextStyle.short,
        )
        self.content_input = TextInput(
            label="Message Content",
            placeholder="Write the message that will be sent...",
            max_length=2000,
            required=True,
            style=discord.TextStyle.paragraph,
        )

        self.add_item(self.name_input)
        self.add_item(self.frequency_input)
        self.add_item(self.content_input)

    async def on_submit(self, modal_interaction: discord.Interaction):
        frequency_seconds = self.cog.parse_frequency(self.frequency_input.value)
        if frequency_seconds is None:
            embed = discord.Embed(
                title="Error: Invalid Frequency",
                description="Please use a valid frequency format.\nExamples: `2d`, `3m`, `4mon`, `2d 3hr`, `1w 2d 3hr 30m`",
                color=discord.Color.red(),
            )
            await modal_interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if frequency_seconds < 60:
            embed = discord.Embed(
                title="Error: Frequency Too Short",
                description="Minimum frequency is 60 seconds to ensure a smooth bot experience.",
                color=discord.Color.red(),
            )
            await modal_interaction.response.send_message(embed=embed, ephemeral=True)
            return

        guild_id = self.interaction.guild.id
        message_id = self.cog.get_next_message_id(guild_id)
        current_time = time.time()
        next_send_time = current_time

        data = {
            "guild_id": guild_id,
            "message_id": message_id,
            "name": self.name_input.value,
            "channel_id": self.channel.id,
            "message_content": self.content_input.value,
            "frequency_seconds": frequency_seconds,
            "next_send_time": next_send_time,
            "is_active": 1,
            "started_at": current_time
        }

        async with self.cog.acquire_db() as db:
            await db.execute(
                """
                INSERT INTO scheduled_messages
                (guild_id, message_id, name, channel_id, message_content, frequency_seconds,
                 next_send_time, is_active, started_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(data.values()),
            )
            await db.commit()

        if guild_id not in self.cog.message_cache:
            self.cog.message_cache[guild_id] = {}
        self.cog.message_cache[guild_id][message_id] = data

        embed = discord.Embed(
            title="Scheduled Message Created Successfully",
            description=(
                f"**Name:** {self.name_input.value}\n"
                f"**Channel:** {self.channel.mention}\n"
                f"**Frequency:** {self.cog.format_frequency(frequency_seconds)}\n"
                f"**Message:** {self.content_input.value[:100]}{'...' if len(self.content_input.value) > 100 else ''}\n"
                f"**Status:** 🟢 Active"
            ),
            color=discord.Color.green(),
        )
        embed.set_footer(text=f"Scheduled Message ID: {message_id}")

        await modal_interaction.response.send_message(embed=embed, ephemeral=True)


class ScheduledMessages(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.message_cache: Dict[int, Dict[int, dict]] = {}
        self.db_pool: Optional[asyncio.Queue[aiosqlite.Connection]] = None

    async def cog_load(self):
        await self.init_pools()
        await self.init_db()
        await self.populate_caches()
        if not self.send_scheduled_messages.is_running():
            self.send_scheduled_messages.start()

    async def cog_unload(self):
        if self.send_scheduled_messages.is_running():
            self.send_scheduled_messages.cancel()

        if self.db_pool:
            while not self.db_pool.empty():
                conn = await self.db_pool.get()
                await conn.close()

    async def init_pools(self, pool_size: int = 5):
        if self.db_pool is None:
            self.db_pool = asyncio.Queue(maxsize=pool_size)
            for _ in range(pool_size):
                conn = await aiosqlite.connect(SMDB_PATH, timeout=5.0)
                await conn.execute("PRAGMA busy_timeout=5000")
                await conn.execute("PRAGMA journal_mode=WAL")
                await conn.execute("PRAGMA synchronous=NORMAL")
                await conn.commit()
                await self.db_pool.put(conn)

    @asynccontextmanager
    async def acquire_db(self) -> AsyncGenerator[aiosqlite.Connection, None]:
        conn = await self.db_pool.get()
        try:
            yield conn
        finally:
            await self.db_pool.put(conn)

    async def init_db(self):
        async with self.acquire_db() as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduled_messages
                (
                    guild_id INTEGER,
                    message_id INTEGER,
                    name TEXT,
                    channel_id INTEGER,
                    message_content TEXT,
                    frequency_seconds INTEGER,
                    next_send_time REAL,
                    is_active INTEGER DEFAULT 1,
                    started_at REAL, PRIMARY KEY
                (
                    guild_id,
                    message_id
                )
                    )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_sm_active ON scheduled_messages(is_active, next_send_time)")
            await db.commit()

    async def populate_caches(self):
        self.message_cache.clear()
        async with self.acquire_db() as db:
            async with db.execute("SELECT * FROM scheduled_messages") as cursor:
                rows = await cursor.fetchall()
                columns = [column[0] for column in cursor.description]
                for row in rows:
                    data = dict(zip(columns, row))
                    g_id = data["guild_id"]
                    m_id = data["message_id"]

                    if g_id not in self.message_cache:
                        self.message_cache[g_id] = {}
                    self.message_cache[g_id][m_id] = data

    def get_next_message_id(self, guild_id: int) -> int:
        guild_msgs = self.message_cache.get(guild_id, {})
        if not guild_msgs:
            return 1
        return max(guild_msgs.keys()) + 1

    def parse_frequency(self, frequency_str: str) -> Optional[int]:
        frequency_str = frequency_str.lower().strip()
        units = {
            "s": 1, "sec": 1, "second": 1, "seconds": 1,
            "m": 60, "min": 60, "minute": 60, "minutes": 60,
            "h": 3600, "hour": 3600, "hours": 3600,
            "d": 86400, "day": 86400, "days": 86400,
            "w": 604800, "week": 604800, "weeks": 604800,
            "mon": 2629746, "month": 2629746, "months": 2629746,
            "y": 31556952, "year": 31556952, "years": 31556952,
        }
        total_seconds = 0
        pattern = r"(\d+(?:\.\d+)?)\s*([a-zA-Z]+)"
        matches = re.findall(pattern, frequency_str)
        if not matches: return None
        for number_str, unit in matches:
            try:
                number = float(number_str)
                if unit in units:
                    total_seconds += number * units[unit]
                else:
                    return None
            except ValueError:
                return None
        return int(total_seconds) if total_seconds > 0 else None

    def format_frequency(self, seconds: int) -> str:
        if seconds < 60: return f"{seconds} second{'s' if seconds != 1 else ''}"
        units = [
            (31556952, "year", "years"), (2629746, "month", "months"),
            (604800, "week", "weeks"), (86400, "day", "days"),
            (3600, "hour", "hours"), (60, "minute", "minutes"), (1, "second", "seconds"),
        ]
        parts, remaining = [], seconds
        for unit_seconds, singular, plural in units:
            if remaining >= unit_seconds:
                count = remaining // unit_seconds
                remaining %= unit_seconds
                parts.append(f"{count} {singular if count == 1 else plural}")
        if not parts: return f"{seconds} seconds"
        if len(parts) == 1: return parts[0]
        if len(parts) == 2: return f"{parts[0]} and {parts[1]}"
        return f"{', '.join(parts[:-1])}, and {parts[-1]}"

    scheduledmessage_group = app_commands.Group(name="scheduledmessage", description="Scheduled message commands")
    panel_subgroup = app_commands.Group(name="panel", description="Panel commands", parent=scheduledmessage_group)

    async def _scheduled_message_autocomplete(self, interaction: discord.Interaction, current: str,
                                              only_active: Optional[bool] = None):
        if not interaction.guild_id: return []

        guild_msgs = self.message_cache.get(interaction.guild_id, {})
        choices = []
        for m_id, data in guild_msgs.items():
            if only_active is True and not data["is_active"]: continue
            if only_active is False and data["is_active"]: continue

            label = f"{m_id}. {data['name']}"
            if current.lower() in label.lower():
                choices.append(app_commands.Choice(name=label[:100], value=m_id))

            if len(choices) >= 25: break
        return choices

    @panel_subgroup.command(name="setup", description="Create a new scheduled message")
    @app_commands.check(slash_mod_check)
    async def setup(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.send_modal(ScheduledMessageSetupModal(self, interaction, channel))

    @panel_subgroup.command(name="panels", description="View all scheduled messages for this server")
    @app_commands.check(slash_mod_check)
    async def panels(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        messages = self.message_cache.get(guild_id, {})

        if not messages:
            embed = discord.Embed(
                title="Your Scheduled Messages",
                description="No scheduled messages found, use `/scheduledmessage panel setup` to create one!",
                color=discord.Color(0x337FD5),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(title="Your Scheduled Messages", color=discord.Color(0x337FD5))
        description = ""
        for m_id in sorted(messages.keys()):
            data = messages[m_id]
            channel = self.bot.get_channel(data["channel_id"])
            c_mention = channel.mention if channel else f"Unknown ({data['channel_id']})"

            description += f"## {m_id}. {data['name']}\n"
            description += f"* **Message Content:**\n`{data['message_content']}`\n\n"
            description += f"* Repeats every **{self.format_frequency(data['frequency_seconds'])}**.\n"
            description += f"* **Status:** {'🟢 Active' if data['is_active'] else '🔴 Inactive'}\n"

            if data["is_active"]:
                description += f"* Next send: <t:{int(data['next_send_time'])}:R> in {c_mention}\n"
            else:
                description += f"* Channel: {c_mention}\n"
            description += f"* **Scheduled Message ID: {m_id}**\n\n"

        embed.description = description
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @panel_subgroup.command(name="delete", description="Delete a scheduled message")
    @app_commands.check(slash_mod_check)
    @app_commands.describe(message_id="Scheduled message to delete")
    @app_commands.rename(message_id="name")
    async def delete(self, interaction: discord.Interaction, message_id: int):
        guild_id = interaction.guild_id
        guild_msgs = self.message_cache.get(guild_id, {})

        if message_id not in guild_msgs:
            await interaction.response.send_message("No scheduled message exists with that ID.", ephemeral=True)
            return

        name = guild_msgs[message_id]["name"]

        async with self.acquire_db() as db:
            await db.execute("DELETE FROM scheduled_messages WHERE guild_id = ? AND message_id = ?",
                             (guild_id, message_id))
            await db.commit()

        del self.message_cache[guild_id][message_id]

        embed = discord.Embed(
            title="Scheduled Message Deleted Successfully",
            description=f"Successfully deleted scheduled message **{name}** (ID: {message_id}).",
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @delete.autocomplete("message_id")
    async def delete_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._scheduled_message_autocomplete(interaction, current)

    @scheduledmessage_group.command(name="start", description="Start a stopped scheduled message")
    @app_commands.check(slash_mod_check)
    async def start(self, interaction: discord.Interaction, message_id: int):
        guild_id = interaction.guild_id
        guild_msgs = self.message_cache.get(guild_id, {})

        if message_id not in guild_msgs or guild_msgs[message_id]["is_active"]:
            await interaction.response.send_message("Inactive scheduled message not found.", ephemeral=True)
            return

        data = guild_msgs[message_id]
        now = time.time()
        next_send = now + data["frequency_seconds"]

        async with self.acquire_db() as db:
            await db.execute(
                "UPDATE scheduled_messages SET is_active = 1, started_at = ?, next_send_time = ? WHERE guild_id = ? AND message_id = ?",
                (now, next_send, guild_id, message_id)
            )
            await db.commit()

        data["is_active"] = 1
        data["started_at"] = now
        data["next_send_time"] = next_send

        channel = self.bot.get_channel(data["channel_id"])
        await interaction.response.send_message(f"Scheduled message **{data['name']}** started!", ephemeral=True)

    @start.autocomplete("message_id")
    async def start_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._scheduled_message_autocomplete(interaction, current, only_active=False)

    @scheduledmessage_group.command(name="stop", description="Stop an active scheduled message")
    @app_commands.check(slash_mod_check)
    async def stop(self, interaction: discord.Interaction, message_id: int):
        guild_id = interaction.guild_id
        guild_msgs = self.message_cache.get(guild_id, {})

        if message_id not in guild_msgs or not guild_msgs[message_id]["is_active"]:
            await interaction.response.send_message("Active scheduled message not found.", ephemeral=True)
            return

        async with self.acquire_db() as db:
            await db.execute("UPDATE scheduled_messages SET is_active = 0 WHERE guild_id = ? AND message_id = ?",
                             (guild_id, message_id))
            await db.commit()

        guild_msgs[message_id]["is_active"] = 0

        await interaction.response.send_message(f"Scheduled message **{guild_msgs[message_id]['name']}** stopped.",
                                                ephemeral=True)

    @stop.autocomplete("message_id")
    async def stop_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._scheduled_message_autocomplete(interaction, current, only_active=True)

    @tasks.loop(seconds=60)
    async def send_scheduled_messages(self):
        now = time.time()
        updates = []

        for guild_id, messages in self.message_cache.items():
            for m_id, data in messages.items():
                if data["is_active"] and now >= data["next_send_time"]:
                    try:
                        channel = self.bot.get_channel(data["channel_id"])
                        if channel:
                            await channel.send(data["message_content"])

                        new_next_send = now + data["frequency_seconds"]

                        # Prepare for DB batch update
                        updates.append((new_next_send, guild_id, m_id))
                        # Update cache immediately
                        data["next_send_time"] = new_next_send
                    except Exception as e:
                        print(f"Error sending message {m_id}: {e}")

        if updates:
            async with self.acquire_db() as db:
                await db.executemany(
                    "UPDATE scheduled_messages SET next_send_time = ? WHERE guild_id = ? AND message_id = ?",
                    updates
                )
                await db.commit()


async def setup(bot):
    await bot.add_cog(ScheduledMessages(bot))