import discord
from discord.ext import commands, tasks
from discord import app_commands
from discord.ui import Modal, TextInput
import aiosqlite
import asyncio
import collections
import time
import re
from typing import Optional, List, Dict, Tuple, Any
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

        message_id = await self.cog.get_next_message_id(self.interaction.guild.id)
        current_time = time.time()
        next_send_time = current_time + frequency_seconds

        async with await self.cog.get_db_connection() as db:
            await db.execute(
                """
                INSERT INTO scheduled_messages
                    (guild_id, message_id, name, channel_id, message_content, frequency_seconds,
                     next_send_time, is_active, started_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    self.interaction.guild.id,
                    message_id,
                    self.name_input.value,
                    self.channel.id,
                    self.content_input.value,
                    frequency_seconds,
                    next_send_time,
                    current_time,
                ),
            )
            await db.commit()

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
        self._db_pool: List[aiosqlite.Connection] = []
        self._db_pool_lock = asyncio.Lock()
        self._db_pool_initialized = False
        self._db_pool_size = 8

        self._max_message_id_cache: Dict[int, Tuple[int, float]] = {}
        self._autocomplete_cache: "collections.OrderedDict[Tuple[int, Optional[bool]], List[Tuple[int, str]]]" = (
            collections.OrderedDict()
        )
        self._autocomplete_cache_max_guilds = 100

    async def cog_load(self):
        """Initialize the scheduled messages database when cog is loaded"""
        await self.init_sm_db()
        if not self.send_scheduled_messages.is_running():
            self.send_scheduled_messages.start()
        if not self.cleanup_cache.is_running():
            self.cleanup_cache.start()

    async def cog_unload(self):
        """Close database connection when cog is unloaded"""
        if self.send_scheduled_messages.is_running():
            self.send_scheduled_messages.cancel()
        if self.cleanup_cache.is_running():
            self.cleanup_cache.cancel()

        async with self._db_pool_lock:
            for conn in self._db_pool:
                await conn.close()
            self._db_pool.clear()

    async def _init_db_pool(self):
        """Initialize the async SQLite connection pool lazily."""
        if self._db_pool_initialized:
            return

        async with self._db_pool_lock:
            if self._db_pool_initialized:
                return

            max_retries = 5

            for _ in range(self._db_pool_size):
                for attempt in range(max_retries):
                    try:
                        conn = await aiosqlite.connect(SMDB_PATH, timeout=5.0)
                        await conn.execute("PRAGMA busy_timeout=5000")
                        self._db_pool.append(conn)
                        break
                    except Exception:
                        if attempt < max_retries - 1:
                            await asyncio.sleep(0.1 * (2 ** attempt))
                            continue
                        else:
                            raise

            self._db_pool_initialized = True

    async def get_db_connection(self):
        """
        Async context manager yielding a pooled database connection.

        Usage:
            async with await self.get_db_connection() as db:
                ...
        """

        await self._init_db_pool()

        class _DBConnContext:
            def __init__(self, outer: "ScheduledMessages"):
                self._outer = outer
                self._conn: Optional[aiosqlite.Connection] = None

            async def __aenter__(self) -> aiosqlite.Connection:
                async with self._outer._db_pool_lock:
                    while not self._outer._db_pool:
                        await asyncio.sleep(0)
                    self._conn = self._outer._db_pool.pop()
                return self._conn

            async def __aexit__(
                self,
                exc_type: Optional[type],
                exc: Optional[BaseException],
                tb: Optional[Any],
            ) -> None:
                if self._conn is not None:
                    async with self._outer._db_pool_lock:
                        self._outer._db_pool.append(self._conn)
                    self._conn = None

        return _DBConnContext(self)

    async def init_sm_db(self):
        """Initialize the scheduled messages database"""
        db = await aiosqlite.connect(SMDB_PATH, timeout=5.0)

        try:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=NORMAL")
            await db.execute("PRAGMA cache_size=-64000")
            await db.execute("PRAGMA foreign_keys=ON")

            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduled_messages (
                    guild_id INTEGER,
                    message_id INTEGER,
                    name TEXT,
                    channel_id INTEGER,
                    message_content TEXT,
                    frequency_seconds INTEGER,
                    next_send_time REAL,
                    is_active INTEGER DEFAULT 1,
                    started_at REAL,
                    PRIMARY KEY (guild_id, message_id)
                )
                """
            )

            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_scheduled_messages_active 
                ON scheduled_messages(is_active, next_send_time)
                """
            )

            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_scheduled_messages_guild 
                ON scheduled_messages(guild_id, message_id)
                """
            )

            await db.commit()
        finally:
            await db.close()

    def parse_frequency(self, frequency_str: str) -> Optional[int]:
        """
        Parse frequency string and return seconds
        Examples: '2d', '3m', '4mon', '2d 3hr', '1w 2d 3hr 30m'
        """
        frequency_str = frequency_str.lower().strip()

        units = {
            "s": 1,
            "sec": 1,
            "second": 1,
            "seconds": 1,
            "m": 60,
            "min": 60,
            "minute": 60,
            "minutes": 60,
            "hr": 3600,
            "hour": 3600,
            "hours": 3600,
            "d": 86400,
            "day": 86400,
            "days": 86400,
            "w": 604800,
            "week": 604800,
            "weeks": 604800,
            "mon": 2629746,
            "month": 2629746,
            "months": 2629746,
            "y": 31556952,
            "year": 31556952,
            "years": 31556952,
        }

        total_seconds = 0
        pattern = r"(\d+(?:\.\d+)?)\s*([a-zA-Z]+)"
        matches = re.findall(pattern, frequency_str)

        if not matches:
            return None

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
        """Convert seconds to human readable format showing all components"""
        if seconds < 60:
            return f"{seconds} second{'s' if seconds != 1 else ''}"

        units = [
            (31556952, "year", "years"),
            (2629746, "month", "months"),
            (604800, "week", "weeks"),
            (86400, "day", "days"),
            (3600, "hour", "hours"),
            (60, "minute", "minutes"),
            (1, "second", "seconds"),
        ]

        parts: List[str] = []
        remaining = seconds

        for unit_seconds, singular, plural in units:
            if remaining >= unit_seconds:
                count = remaining // unit_seconds
                remaining = remaining % unit_seconds
                unit_name = singular if count == 1 else plural
                parts.append(f"{count} {unit_name}")

        if not parts:
            return f"{seconds} second{'s' if seconds != 1 else ''}"

        if len(parts) == 1:
            return parts[0]
        if len(parts) == 2:
            return f"{parts[0]} and {parts[1]}"
        return f"{', '.join(parts[:-1])}, and {parts[-1]}"

    async def get_next_message_id(self, guild_id: int) -> int:
        """
        Get the next message ID for a guild using a cached max ID with expiration.
        This prefers performance over reusing gaps in the ID sequence.
        """
        now = time.time()
        cache_entry = self._max_message_id_cache.get(guild_id)
        max_id: Optional[int] = None
        cache_ttl = 30 * 60

        if cache_entry is not None:
            cached_max_id, ts = cache_entry
            if now - ts < cache_ttl:
                max_id = cached_max_id

        if max_id is None:
            async with await self.get_db_connection() as db:
                cursor = await db.execute(
                    "SELECT MAX(message_id) FROM scheduled_messages WHERE guild_id = ?",
                    (guild_id,),
                )
                row = await cursor.fetchone()
                await cursor.close()

            max_id = row[0] if row and row[0] is not None else 0

        next_id = max_id + 1
        self._max_message_id_cache[guild_id] = (next_id, now)
        return next_id

    scheduledmessage_group = app_commands.Group(name="scheduledmessage", description="Scheduled message commands")
    panel_subgroup = app_commands.Group(
        name="panel",
        description="Scheduled message panel commands",
        parent=scheduledmessage_group,
    )

    async def _scheduled_message_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
        only_active: Optional[bool] = None,
    ):
        if not interaction.guild:
            return []

        base_query = """
            SELECT message_id, name
            FROM scheduled_messages
            WHERE guild_id = ?
        """
        params = [interaction.guild.id]

        if only_active is True:
            base_query += " AND is_active = 1"
        elif only_active is False:
            base_query += " AND is_active = 0"

        base_query += " ORDER BY message_id"

        cache_key = (interaction.guild.id, only_active)
        rows: List[Tuple[int, str]]

        cached = self._autocomplete_cache.get(cache_key)
        if cached is not None:
            self._autocomplete_cache.move_to_end(cache_key)
            rows = cached
        else:
            async with await self.get_db_connection() as db:
                cursor = await db.execute(base_query, params)
                rows = await cursor.fetchall()
                await cursor.close()

            self._autocomplete_cache[cache_key] = rows
            if len(self._autocomplete_cache) > self._autocomplete_cache_max_guilds:
                self._autocomplete_cache.popitem(last=False)

        results = []
        current_lower = current.lower()
        for message_id, name in rows:
            label = f"{message_id}. {name}"
            if current_lower and current_lower not in label.lower():
                continue
            results.append(app_commands.Choice(name=label[:100], value=message_id))
            if len(results) >= 25:
                break

        return results

    @panel_subgroup.command(name="setup", description="Create a new scheduled message")
    @app_commands.check(slash_mod_check)
    @app_commands.describe(channel="Channel that will receive the scheduled message")
    async def setup(self, interaction: discord.Interaction, channel: discord.TextChannel):
        """Open a modal to create a new scheduled message"""
        await interaction.response.send_modal(ScheduledMessageSetupModal(self, interaction, channel))

    @panel_subgroup.command(name="panels", description="View all scheduled messages for this server")
    @app_commands.check(slash_mod_check)
    async def panels(self, interaction: discord.Interaction):
        """Display all scheduled messages for the guild"""
        async with await self.get_db_connection() as db:
            cursor = await db.execute(
                """
                SELECT message_id, name, channel_id, message_content, frequency_seconds, next_send_time, is_active
                FROM scheduled_messages
                WHERE guild_id = ?
                ORDER BY message_id
                """,
                (interaction.guild.id,),
            )

            messages = await cursor.fetchall()
            await cursor.close()

        if not messages:
            embed = discord.Embed(
                title="Your Scheduled Messages",
                description="No scheduled messages found, use `/scheduledmessage panel setup` to create one!",
                color=discord.Color(0x337FD5),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(
            title="Your Scheduled Messages",
            color=discord.Color(0x337FD5),
        )

        description = ""
        for message_id, name, channel_id, message_content, frequency_seconds, next_send_time, is_active in messages:
            channel = self.bot.get_channel(channel_id)
            channel_name = channel.mention if channel else f"Unknown Channel ({channel_id})"

            next_send = datetime.fromtimestamp(next_send_time)
            description += f"## {message_id}. {name}\n"
            description += f"* **Message Content:**\n`{message_content}`\n\n"
            description += f"* Repeats every **{self.format_frequency(frequency_seconds)}**.\n"
            description += f"* **Status:** {'🟢 Active' if is_active else '🔴 Inactive'}\n"

            if is_active:
                description += f"* Next send: <t:{int(next_send_time)}:R> in {channel_name}\n"
            else:
                description += f"* Channel: {channel_name}\n"

            description += f"* **Scheduled Message ID: {message_id}**\n\n"

        embed.description = description
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @panel_subgroup.command(name="delete", description="Delete a scheduled message")
    @app_commands.check(slash_mod_check)
    @app_commands.describe(message_id="Scheduled message to delete")
    @app_commands.rename(message_id="name")
    async def delete(self, interaction: discord.Interaction, message_id: int):
        """Delete a scheduled message"""
        async with await self.get_db_connection() as db:
            cursor = await db.execute(
                """
                SELECT name FROM scheduled_messages
                WHERE guild_id = ? AND message_id = ?
                """,
                (interaction.guild.id, message_id),
            )

            row = await cursor.fetchone()
            await cursor.close()
        if not row:
            embed = discord.Embed(
                title="Error: Message Not Found",
                description="No scheduled message exists with that ID.",
                color=discord.Color.red(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        name = row[0]

        async with await self.get_db_connection() as db:
            await db.execute(
                """
                DELETE FROM scheduled_messages
                WHERE guild_id = ? AND message_id = ?
                """,
                (interaction.guild.id, message_id),
            )

            await db.commit()

        embed = discord.Embed(
            title="Scheduled Message Deleted Successfully",
            description=f"Successfully deleted scheduled message **{name}** (ID: {message_id}).",
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @delete.autocomplete("message_id")
    async def delete_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._scheduled_message_autocomplete(interaction, current, only_active=None)

    @scheduledmessage_group.command(name="start", description="Start a stopped scheduled message")
    @app_commands.check(slash_mod_check)
    @app_commands.describe(message_id="Inactive scheduled message to start")
    async def start(self, interaction: discord.Interaction, message_id: int):
        """Start an inactive scheduled message"""
        async with await self.get_db_connection() as db:
            cursor = await db.execute(
                """
                SELECT name, channel_id, frequency_seconds
                FROM scheduled_messages
                WHERE guild_id = ? AND message_id = ? AND is_active = 0
                """,
                (interaction.guild.id, message_id),
            )

            row = await cursor.fetchone()
            await cursor.close()
        if not row:
            embed = discord.Embed(
                title="Panel is already running",
                description="No inactive scheduled message found with that ID.",
                color=discord.Color.red(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        name, channel_id, frequency_seconds = row
        current_time = time.time()
        next_send_time = current_time + frequency_seconds

        async with await self.get_db_connection() as db:
            await db.execute(
                """
                UPDATE scheduled_messages
                SET is_active = 1, started_at = ?, next_send_time = ?
                WHERE guild_id = ? AND message_id = ?
                """,
                (current_time, next_send_time, interaction.guild.id, message_id),
            )

            await db.commit()

        channel = self.bot.get_channel(channel_id)
        embed = discord.Embed(
            title="Scheduled Message Started Successfully",
            description=(
                f"Scheduled message **{name}** is now active and will send in "
                f"{channel.mention if channel else f'Unknown Channel ({channel_id})'}!"
            ),
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @start.autocomplete("message_id")
    async def start_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._scheduled_message_autocomplete(interaction, current, only_active=False)

    @scheduledmessage_group.command(name="stop", description="Stop an active scheduled message")
    @app_commands.check(slash_mod_check)
    @app_commands.describe(message_id="Active scheduled message to stop")
    async def stop(self, interaction: discord.Interaction, message_id: int):
        """Stop an active scheduled message"""
        async with await self.get_db_connection() as db:
            cursor = await db.execute(
                """
                SELECT name, channel_id
                FROM scheduled_messages
                WHERE guild_id = ? AND message_id = ? AND is_active = 1
                """,
                (interaction.guild.id, message_id),
            )

            row = await cursor.fetchone()
            await cursor.close()
        if not row:
            embed = discord.Embed(
                title="Panel already stopped",
                description="No active scheduled message found with that ID.",
                color=discord.Color.red(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        name, channel_id = row

        async with await self.get_db_connection() as db:
            await db.execute(
                """
                UPDATE scheduled_messages
                SET is_active = 0
                WHERE guild_id = ? AND message_id = ?
                """,
                (interaction.guild.id, message_id),
            )

            await db.commit()

        channel = self.bot.get_channel(channel_id)
        embed = discord.Embed(
            title="Scheduled Message Stopped Successfully",
            description=(
                f"Scheduled message **{name}** has been stopped in "
                f"{channel.mention if channel else f'Unknown Channel ({channel_id})'}.\n\n"
                "You can start it again using `/scheduledmessage start`."
            ),
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @stop.autocomplete("message_id")
    async def stop_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._scheduled_message_autocomplete(interaction, current, only_active=True)

    @tasks.loop(seconds=60)
    async def send_scheduled_messages(self):
        """Background task to send scheduled messages"""
        try:
            current_time = datetime.now().timestamp()

            async with await self.get_db_connection() as db:
                cursor = await db.execute(
                    """
                    SELECT guild_id, message_id, name, channel_id, message_content, frequency_seconds
                    FROM scheduled_messages
                    WHERE next_send_time <= ? AND is_active = 1
                    """,
                    (current_time,),
                )

                messages_to_send = await cursor.fetchall()
                await cursor.close()

                updates: List[Tuple[float, int, int]] = []

                for guild_id, message_id, name, channel_id, message_content, frequency_seconds in messages_to_send:
                    try:
                        guild = self.bot.get_guild(guild_id)
                        if not guild:
                            continue

                        channel = guild.get_channel(channel_id)
                        if not channel:
                            continue

                        await channel.send(message_content)

                        next_send_time = current_time + frequency_seconds
                        updates.append((next_send_time, guild_id, message_id))

                    except Exception as e:
                        print(f"Error sending scheduled message {message_id} in guild {guild_id}: {e}")
                        continue

                if updates:
                    await db.executemany(
                        """
                        UPDATE scheduled_messages
                        SET next_send_time = ?
                        WHERE guild_id = ? AND message_id = ?
                        """,
                        updates,
                    )
                    await db.commit()

        except Exception as e:
            print(f"Error in send_scheduled_messages task: {e}")

    @tasks.loop(hours=2)
    async def cleanup_cache(self):
        """Periodic background task to prune stale cache entries."""
        now = time.time()
        cache_ttl = 30 * 60

        stale_guilds = [
            guild_id
            for guild_id, (_max_id, ts) in self._max_message_id_cache.items()
            if now - ts > cache_ttl
        ]
        for guild_id in stale_guilds:
            self._max_message_id_cache.pop(guild_id, None)

        while len(self._autocomplete_cache) > self._autocomplete_cache_max_guilds:
            self._autocomplete_cache.popitem(last=False)


async def setup(bot):
    await bot.add_cog(ScheduledMessages(bot))
