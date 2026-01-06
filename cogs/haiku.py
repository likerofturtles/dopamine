import asyncio
import re
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Deque, Dict, List, Optional, Set, Tuple

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import HDDB_PATH, HWDDB_PATH
from utils.checks import slash_mod_check

class HaikuDetector(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._haiku_word_cache: Dict[str, int] = {}
        self._active_channels: Set[Tuple[int, int]] = set()
        self._last_active_channels_refresh: Optional[datetime] = None
        self._hd_pool: Optional[asyncio.Queue[aiosqlite.Connection]] = None
        self._hwd_pool: Optional[asyncio.Queue[aiosqlite.Connection]] = None
        self.haiku_queue: "asyncio.Queue[discord.Message]" = asyncio.Queue()
        self._worker_tasks: List[asyncio.Task] = []
        self._recent_processed_messages: Deque[int] = deque(maxlen=500)

    async def cog_load(self):
        """Initialize the haiku databases when cog is loaded"""
        await self._init_pools()
        await self.init_haiku_db()
        await self.init_haiku_words_db()
        await self.refresh_haiku_word_cache()
        await self.refresh_active_channels()
        await self.start_workers()
        if not self.haiku_monitor.is_running():
            self.haiku_monitor.start()

    async def cog_unload(self):
        """Clean up when cog is unloaded"""
        if self.haiku_monitor.is_running():
            self.haiku_monitor.cancel()
        for task in self._worker_tasks:
            task.cancel()
        while not self.haiku_queue.empty():
            try:
                self.haiku_queue.get_nowait()
                self.haiku_queue.task_done()
            except asyncio.QueueEmpty:
                break
        for pool in (self._hd_pool, self._hwd_pool):
            if pool is None:
                continue
            while not pool.empty():
                conn = await pool.get()
                try:
                    await conn.close()
                except Exception:
                    pass

    async def _create_pooled_connection(self, path: str) -> aiosqlite.Connection:
        """
        Create a single SQLite connection with tuned PRAGMAs for inclusion in a pool.
        """
        max_retries = 5
        for attempt in range(max_retries):
            try:
                conn = await aiosqlite.connect(
                    path,
                    timeout=5.0,
                    isolation_level=None,
                )
                await conn.execute("PRAGMA busy_timeout=5000")
                await conn.execute("PRAGMA journal_mode=WAL")
                await conn.execute("PRAGMA wal_autocheckpoint=1000")
                await conn.execute("PRAGMA synchronous=NORMAL")
                await conn.execute("PRAGMA cache_size=-64000")
                await conn.execute("PRAGMA optimize")
                await conn.commit()
                return conn
            except Exception:
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.1 * (2 ** attempt))
                    continue
                raise

    async def _init_pools(self, pool_size: int = 5):
        """
        Initialize small connection pools for the haiku settings and words databases.

        Using a bounded pool keeps the number of concurrent SQLite connections sane
        while still allowing a high degree of concurrency across tasks.
        """
        if self._hd_pool is None:
            self._hd_pool = asyncio.Queue(maxsize=pool_size)
            for _ in range(pool_size):
                conn = await self._create_pooled_connection(HDDB_PATH)
                await self._hd_pool.put(conn)

        if self._hwd_pool is None:
            self._hwd_pool = asyncio.Queue(maxsize=pool_size)
            for _ in range(pool_size):
                conn = await self._create_pooled_connection(HWDDB_PATH)
                await self._hwd_pool.put(conn)

    @asynccontextmanager
    async def _acquire_hd_db(self) -> aiosqlite.Connection:
        """
        Acquire a connection from the haiku settings pool.

        This yields a pooled connection and returns it to the pool afterwards.
        """
        assert self._hd_pool is not None, "Haiku settings DB pool not initialized"
        conn = await self._hd_pool.get()
        try:
            yield conn
        finally:
            await self._hd_pool.put(conn)

    @asynccontextmanager
    async def _acquire_hwd_db(self) -> aiosqlite.Connection:
        """
        Acquire a connection from the haiku words pool.

        This yields a pooled connection and returns it to the pool afterwards.
        """
        assert self._hwd_pool is not None, "Haiku words DB pool not initialized"
        conn = await self._hwd_pool.get()
        try:
            yield conn
        finally:
            await self._hwd_pool.put(conn)

    async def init_haiku_db(self):
        """Initialize the haiku detection database"""
        async with self._acquire_hd_db() as db:
            await db.execute(
                '''
                CREATE TABLE IF NOT EXISTS haiku_settings (
                    guild_id INTEGER PRIMARY KEY,
                    channel_id INTEGER,
                    is_enabled INTEGER DEFAULT 0
                )
                '''
            )

            await db.execute(
                '''
                CREATE INDEX IF NOT EXISTS idx_haiku_settings_enabled 
                ON haiku_settings(is_enabled)
                '''
            )

            await db.commit()

    async def init_haiku_words_db(self):
        """Initialize the haiku words database"""
        async with self._acquire_hwd_db() as db:
            await db.execute(
                '''
                CREATE TABLE IF NOT EXISTS haiku_words (
                    word TEXT PRIMARY KEY,
                    syllables INTEGER
                )
                '''
            )
            await db.commit()

    async def refresh_haiku_word_cache(self):
        """Load the entire haiku_words table into an in-memory cache."""
        cache: Dict[str, int] = {}
        async with self._acquire_hwd_db() as db:
            async with db.execute('SELECT word, syllables FROM haiku_words') as cursor:
                async for word, syllables in cursor:
                    cache[word] = int(syllables)
        self._haiku_word_cache = cache

    async def start_workers(self, worker_count: int = 5):
        """Start background worker tasks for processing queued haiku messages."""
        if self._worker_tasks:
            return

        loop = asyncio.get_running_loop()
        for _ in range(worker_count):
            task = loop.create_task(self._haiku_worker())
            self._worker_tasks.append(task)

    async def _haiku_worker(self):
        """Consume messages from the queue and process haiku detection."""
        while True:
            message: discord.Message = await self.haiku_queue.get()
            try:
                if message.id in self._recent_processed_messages:
                    continue

                message_content = await self.remove_urls(message.content)
                if not message_content.strip():
                    continue

                syllable_count = await self.count_message_syllables(message_content)

                if syllable_count == 17:
                    already_replied = False
                    async for reply in message.channel.history(limit=50, after=message.created_at):
                        if (reply.author == self.bot.user and
                                reply.reference and
                                reply.reference.message_id == message.id):
                            already_replied = True
                            break

                    if not already_replied:
                        formatted_haiku = await self.format_haiku(message_content)

                        embed = discord.Embed(
                            description=f"\n_{formatted_haiku}_\n\n— {message.author.display_name}\n\n"
                        )
                        embed.set_footer(
                            text=f"I detect Haikus. And sometimes, successfully. use /haiku detection disable to opt out.")

                        await message.reply(embed=embed)

                        self._recent_processed_messages.append(message.id)
            finally:
                self.haiku_queue.task_done()

    async def get_word_syllables(self, word: str) -> int:
        """Get syllable count for a word from the in-memory cache, with fallback estimation.

        All database reads are front-loaded into `_haiku_word_cache`, so at runtime we
        never need to touch SQLite here. This keeps the hot path purely in-memory.
        """
        word_for_lookup = word.lower().replace("'", "")

        cached = self._haiku_word_cache.get(word_for_lookup)
        if cached is not None:
            return cached

        word_clean = word_for_lookup
        vowels = 'aeiouy'
        syllable_count = 0
        prev_was_vowel = False

        for char in word_clean:
            is_vowel = char in vowels
            if is_vowel and not prev_was_vowel:
                syllable_count += 1
            prev_was_vowel = is_vowel

        if word_clean.endswith('e') and syllable_count > 1:
            syllable_count -= 1

        return max(1, syllable_count)

    async def remove_urls(self, text: str) -> str:
        """Remove URLs from a string"""
        url_pattern = re.compile(r'https?://\S+|www\.\S+')
        return url_pattern.sub('', text)

    async def count_message_syllables(self, message: str) -> int:
        """Count total syllables in a message, handling both single-line and multi-line haikus"""
        message_without_urls = await self.remove_urls(message)
        message_split = re.sub(r'[-_–—]', ' ', message_without_urls)

        clean_message = re.sub(r'[*,"&@!()$#.:;{}[\]|\\/=+~`]', ' ', message_split)

        lines = [line.strip() for line in clean_message.split('\n') if line.strip()]

        if len(lines) == 1:
            words = re.sub(r'\s+', ' ', clean_message).split()
            total_syllables = 0
            for word in words:
                if word:
                    total_syllables += await self.get_word_syllables(word)
            return total_syllables

        total_syllables = 0
        for line in lines:
            words = re.sub(r'\s+', ' ', line).split()
            for word in words:
                if word:
                    total_syllables += await self.get_word_syllables(word)

        return total_syllables

    async def format_haiku(self, message: str) -> str:
        """Format a message into 5-7-5 haiku format, preserving original symbols and structure"""

        message_without_urls = await self.remove_urls(message)

        original_tokens = []

        temp_message = message_without_urls
        for separator in ['-', '_', '–', '—']:
            temp_message = temp_message.replace(separator, ' ')

        clean_for_words = re.sub(r'[*,"&@!()$#.:;{}[\]|\\/=+~`]', ' ', temp_message)

        words_with_apostrophes = re.sub(r'\s+', ' ', clean_for_words).strip().split()

        if len(words_with_apostrophes) < 3:
            return message

        line1_words, line2_words, line3_words = [], [], []
        line1_syllables, line2_syllables, line3_syllables = 0, 0, 0

        for word in words_with_apostrophes:
            if not word:
                continue

            syllables = await self.get_word_syllables(word)

            if line1_syllables < 5:
                line1_words.append(word)
                line1_syllables += syllables
            elif line2_syllables < 7:
                line2_words.append(word)
                line2_syllables += syllables
            else:
                line3_words.append(word)
                line3_syllables += syllables

        def capitalize_first(words_list):
            if not words_list:
                return ""
            text = ' '.join(words_list)
            if text:
                return text[0].upper() + text[1:]
            return text

        haiku_lines = [
            capitalize_first(line1_words),
            capitalize_first(line2_words),
            capitalize_first(line3_words)
        ]

        return '\n'.join(haiku_lines)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """
        Lightweight listener that checks whether a new message is in a haiku-enabled
        channel and, if so, enqueues it for background processing.

        This replaces the previous polling-based design that walked channel history,
        which does not scale well to thousands of servers.
        """
        if message.guild is None or message.author.bot:
            return

        if (message.guild.id, message.channel.id) not in self._active_channels:
            return

        if message.id in self._recent_processed_messages:
            return

        await self.haiku_queue.put(message)

    haiku_group = app_commands.Group(name="haiku", description="Haiku detection commands")
    detection_group = app_commands.Group(name="detection", description="Haiku detection settings", parent=haiku_group)

    @detection_group.command(name="enable", description="Enable haiku detection in a channel")
    @app_commands.check(slash_mod_check)
    async def enable_haiku_detection(
            self,
            interaction: discord.Interaction,
            channel: discord.TextChannel
    ):
        """Enable haiku detection in a channel"""

        if not await self.bot.get_cog('TopGGVoter').check_vote_access(interaction.user.id):
            embed = discord.Embed(
                title="Vote to Use This Feature!",
                description="This command requires voting! To access this feature, please vote for Dopamine here: [top.gg](https://top.gg/bot/{bot_id})".format(
                    bot_id=self.bot.user.id
                ),
                color=0xffaa00
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        async with self._acquire_hd_db() as db:
            async with db.execute(
                '''
                SELECT channel_id FROM haiku_settings 
                WHERE guild_id = ? AND is_enabled = 1
                ''',
                (interaction.guild.id,),
            ) as cursor:
                existing = await cursor.fetchone()

            if existing and existing[0] != channel.id:
                await db.execute(
                    '''
                    UPDATE haiku_settings 
                    SET is_enabled = 0 
                    WHERE guild_id = ? AND channel_id = ?
                    ''',
                    (interaction.guild.id, existing[0]),
                )

            await db.execute(
                '''
                INSERT OR REPLACE INTO haiku_settings 
                (guild_id, channel_id, is_enabled) 
                VALUES (?, ?, 1)
                ''',
                (interaction.guild.id, channel.id),
            )

            await db.commit()

        await self.refresh_active_channels()

        embed = discord.Embed(
            title="Haiku Detection Enabled",
            description=f"Haiku detection is now active in {channel.mention}!\n\nI'll monitor messages and detect haikus automatically.",
            color=discord.Color.green()
        )
        embed.set_footer(text="Use /haiku detection disable to turn it off")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @detection_group.command(name="disable", description="Disable haiku detection")
    @app_commands.check(slash_mod_check)
    async def disable_haiku_detection(self, interaction: discord.Interaction):
        """Disable haiku detection"""

        if not await self.bot.get_cog('TopGGVoter').check_vote_access(interaction.user.id):
            embed = discord.Embed(
                title="Vote to Use This Feature!",
                description="This command requires voting! To access this feature, please vote for Dopamine here: [top.gg](https://top.gg/bot/{bot_id})".format(
                    bot_id=self.bot.user.id
                ),
                color=0xffaa00
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        async with self._acquire_hd_db() as db:
            async with db.execute(
                '''
                SELECT channel_id FROM haiku_settings 
                WHERE guild_id = ? AND is_enabled = 1
                ''',
                (interaction.guild.id,),
            ) as cursor:
                result = await cursor.fetchone()

            if not result:
                embed = discord.Embed(
                    title="Haiku Detection Not Active",
                    description="Haiku detection is not currently enabled in this server.",
                    color=discord.Color.red()
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            await db.execute(
                '''
                UPDATE haiku_settings 
                SET is_enabled = 0 
                WHERE guild_id = ?
                ''',
                (interaction.guild.id,),
            )

            await db.commit()

        channel = self.bot.get_channel(result[0])
        channel_name = channel.mention if channel else "Unknown Channel"

        embed = discord.Embed(
            title="Haiku Detection Disabled",
            description=f"Haiku detection has been disabled in {channel_name}.",
            color=discord.Color.orange()
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @commands.command(name="update_haiku_database")
    async def update_haiku_database(self, ctx, *, data: str):
        """Update the haiku database with new words and syllable counts"""

        if ctx.author.id != 758576879715483719:
            embed = discord.Embed(
                title="You don't have permission to use this command!",
                description="This is a developer-only command, used to directly manage what words the haiku feature detects. It's not available to the public due to security reasons.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return

        try:
            entries = [entry.strip() for entry in data.split(',')]
            added_words = []

            to_insert = []
            for entry in entries:
                if not entry:
                    continue

                parts = entry.strip().split()
                if len(parts) < 2:
                    continue

                try:
                    word = parts[0].lower().replace("'", "")
                    syllables = int(parts[1])

                    to_insert.append((word, syllables))
                    added_words.append(f"{word}: {syllables} syllables")

                except ValueError:
                    continue

            if to_insert:
                async with self._acquire_hwd_db() as db:
                    await db.executemany(
                        '''
                        INSERT OR REPLACE INTO haiku_words 
                        (word, syllables) VALUES (?, ?)
                        ''',
                        to_insert,
                    )
                    await db.commit()
                await self.refresh_haiku_word_cache()

            if added_words:
                embed = discord.Embed(
                    title="Haiku Database Updated",
                    description=f"Successfully added/updated {len(added_words)} words:\n\n" +
                                "\n".join(added_words[:10]) +
                                (f"\n... and {len(added_words) - 10} more" if len(added_words) > 10 else ""),
                    color=discord.Color.green()
                )
            else:
                embed = discord.Embed(
                    title="No Valid Entries",
                    description="No valid word-syllable pairs were found in your input.",
                    color=discord.Color.red()
                )

            await ctx.send(embed=embed)

        except Exception as e:
            embed = discord.Embed(
                title="Error",
                description=f"An error occurred while updating the database: {str(e)}",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)

    @commands.command(name="view_haiku_dbcount")
    @commands.has_permissions(manage_messages=True)
    async def view_haiku_dbcount(self, ctx):
        """View the total number of words in the haiku database"""

        if ctx.author.id != 758576879715483719:
            embed = discord.Embed(
                title="Access Denied",
                description="You don't have permission to use this command.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return

        async with self._acquire_hwd_db() as db:
            async with db.execute('SELECT COUNT(*) FROM haiku_words') as cursor:
                count = await cursor.fetchone()

        embed = discord.Embed(
            title="Haiku Database Count",
            description=f"Total words in database: **{count[0]}**",
            color=discord.Color.blue()
        )

        await ctx.send(embed=embed)

    @commands.command(name="view_haiku_words")
    @commands.has_permissions(manage_messages=True)
    async def view_haiku_words(self, ctx):
        """View all words and their syllable counts in the haiku database"""

        if ctx.author.id != 758576879715483719:
            embed = discord.Embed(
                title="Access Denied",
                description="You don't have permission to use this command.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return

        async with self._acquire_hwd_db() as db:
            async with db.execute('SELECT word, syllables FROM haiku_words ORDER BY word') as cursor:
                words = await cursor.fetchall()

        if not words:
            embed = discord.Embed(
                title="Haiku Database Empty",
                description="No words found in the database.",
                color=discord.Color.orange()
            )
            await ctx.send(embed=embed)
            return

        current_message = ""
        message_count = 1

        embed = discord.Embed(
            title=f"Haiku Database Words (Part {message_count})",
            color=discord.Color.green()
        )

        for word, syllables in words:
            word_entry = f"**{word}**: {syllables} syllable{'s' if syllables != 1 else ''}\n"

            if len(current_message) + len(word_entry) > 2000:
                embed.description = current_message
                await ctx.send(embed=embed)

                message_count += 1
                embed = discord.Embed(
                    title=f"Haiku Database Words (Part {message_count})",
                    color=discord.Color.green()
                )
                current_message = word_entry
            else:
                current_message += word_entry

        if current_message:
            embed.description = current_message
            embed.set_footer(text=f"Total: {len(words)} words")
            await ctx.send(embed=embed)

    async def refresh_active_channels(self):
        """Refresh the in-memory set of channels that have haiku detection enabled."""
        async with self._acquire_hd_db() as db:
            async with db.execute(
                '''
                SELECT guild_id, channel_id FROM haiku_settings 
                WHERE is_enabled = 1
                '''
            ) as cursor:
                rows = await cursor.fetchall()

        self._active_channels = {(guild_id, channel_id) for guild_id, channel_id in rows}
        self._last_active_channels_refresh = datetime.now(timezone.utc)

    @tasks.loop(seconds=10)
    async def haiku_monitor(self):
        """
        Lightweight background task that periodically refreshes the in-memory
        cache of haiku-enabled channels from the database.

        The actual message monitoring is now done via the `on_message` listener
        for scalability; this loop does not walk channel history anymore.
        """
        try:
            now = datetime.now(timezone.utc)
            if (
                self._last_active_channels_refresh is None
                or (now - self._last_active_channels_refresh).total_seconds() >= 180
            ):
                try:
                    await self.refresh_active_channels()
                except Exception as e:
                    print(f"Error refreshing haiku active channels: {e}")
        except Exception as e:
            print(f"Error in haiku_monitor task: {e}")

async def setup(bot):
    await bot.add_cog(HaikuDetector(bot))
