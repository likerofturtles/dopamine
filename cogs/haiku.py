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
        self.haiku_word_cache: Dict[str, int] = {}
        self.enabled_guilds: Set[int] = set()

        self.hd_pool: Optional[asyncio.Queue[aiosqlite.Connection]] = None
        self.hwd_pool: Optional[asyncio.Queue[aiosqlite.Connection]] = None

        self.haiku_queue: "asyncio.Queue[discord.Message]" = asyncio.Queue()
        self._worker_tasks: List[asyncio.Task] = []
        self._recent_processed_messages: Deque[int] = deque(maxlen=500)

    async def cog_load(self):
        await self.init_pools()
        await self.init_db()
        await self.populate_caches()
        await self.start_workers()

    async def cog_unload(self):
        for task in self._worker_tasks:
            task.cancel()

        while not self.haiku_queue.empty():
            try:
                self.haiku_queue.get_nowait()
                self.haiku_queue.task_done()
            except asyncio.QueueEmpty:
                break

        for pool in (self.hd_pool, self.hwd_pool):
            if pool:
                while not pool.empty():
                    conn = await pool.get()
                    await conn.close()

    async def init_pools(self, pool_size: int = 5):
        if self.hd_pool is None:
            self.hd_pool = asyncio.Queue(maxsize=pool_size)
            for _ in range(pool_size):
                conn = await aiosqlite.connect(HDDB_PATH, timeout=5, isolation_level=None)
                await self._apply_pragmas(conn)
                await self.hd_pool.put(conn)

        if self.hwd_pool is None:
            self.hwd_pool = asyncio.Queue(maxsize=pool_size)
            for _ in range(pool_size):
                conn = await aiosqlite.connect(HWDDB_PATH, timeout=5, isolation_level=None)
                await self._apply_pragmas(conn)
                await self.hwd_pool.put(conn)

    async def _apply_pragmas(self, conn: aiosqlite.Connection):
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.commit()

    @asynccontextmanager
    async def acquire_hd_db(self):
        conn = await self.hd_pool.get()
        try:
            yield conn
        finally:
            await self.hd_pool.put(conn)

    @asynccontextmanager
    async def acquire_hwd_db(self):
        conn = await self.hwd_pool.get()
        try:
            yield conn
        finally:
            await self.hwd_pool.put(conn)

    async def init_db(self):
        async with self.acquire_hd_db() as db:
            await db.execute('''
                             CREATE TABLE IF NOT EXISTS haiku_settings
                             (
                                 guild_id INTEGER PRIMARY KEY,
                                 channel_id TEXT DEFAULT '',
                                 is_enabled INTEGER DEFAULT 0
                             )
                             ''')
            await db.commit()

        async with self.acquire_hwd_db() as db:
            await db.execute('''
                             CREATE TABLE IF NOT EXISTS haiku_words
                             (
                                 word TEXT PRIMARY KEY, 
                                 syllables INTEGER
                             )
                             ''')
            await db.commit()

    async def populate_caches(self):
        # Load active guilds
        async with self.acquire_hd_db() as db:
            async with db.execute("SELECT guild_id FROM haiku_settings WHERE is_enabled = 1") as cursor:
                rows = await cursor.fetchall()
                self.enabled_guilds = {row[0] for row in rows}

        async with self.acquire_hwd_db() as db:
            async with db.execute("SELECT word, syllables FROM haiku_words") as cursor:
                rows = await cursor.fetchall()
                self.haiku_word_cache = {row[0]: int(row[1]) for row in rows}


    async def start_workers(self, worker_count: int = 5):
        if self._worker_tasks:
            return
        loop = asyncio.get_running_loop()
        for _ in range(worker_count):
            task = loop.create_task(self._haiku_worker())
            self._worker_tasks.append(task)

    async def _haiku_worker(self):
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
                    # Check for duplicate replies
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
                            text="I detect Haikus. And sometimes, successfully. To disable, use /haiku detection disable."
                        )
                        await message.reply(embed=embed)
                        self._recent_processed_messages.append(message.id)
            except Exception as e:
                print(f"Error in haiku worker: {e}")
            finally:
                self.haiku_queue.task_done()


    async def get_word_syllables(self, word: str) -> int:
        word_for_lookup = word.lower().replace("'", "")

        cached = self.haiku_word_cache.get(word_for_lookup)
        if cached is not None:
            return cached

        vowels = 'aeiouy'
        syllable_count = 0
        prev_was_vowel = False
        for char in word_for_lookup:
            is_vowel = char in vowels
            if is_vowel and not prev_was_vowel:
                syllable_count += 1
            prev_was_vowel = is_vowel

        if word_for_lookup.endswith('e') and syllable_count > 1:
            syllable_count -= 1
        return max(1, syllable_count)

    async def remove_urls(self, text: str) -> str:
        return re.sub(r'https?://\S+|www\.\S+', '', text)

    async def count_message_syllables(self, message: str) -> int:
        message_without_urls = await self.remove_urls(message)
        message_split = re.sub(r'[-_–—]', ' ', message_without_urls)
        clean_message = re.sub(r'[*,"&@!()$#.:;{}[\]|\\/=+~`]', ' ', message_split)

        words = re.sub(r'\s+', ' ', clean_message).strip().split()
        total = 0
        for word in words:
            if word:
                total += await self.get_word_syllables(word)
        return total

    async def format_haiku(self, message: str) -> str:
        message_without_urls = await self.remove_urls(message)

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
        if message.guild is None or message.author.bot:
            return

        if message.guild.id not in self.enabled_guilds:
            return

        if message.id in self._recent_processed_messages:
            return

        await self.haiku_queue.put(message)

    haiku_group = app_commands.Group(name="haiku", description="Haiku detection commands")
    detection_group = app_commands.Group(name="detection", description="Haiku detection settings", parent=haiku_group)

    @detection_group.command(name="enable", description="Enable haiku detection for the whole server")
    @app_commands.check(slash_mod_check)
    async def enable_haiku_detection(self, interaction: discord.Interaction):
        if not await self.bot.get_cog('TopGGVoter').check_vote_access(interaction.user.id):
            embed = discord.Embed(
                title="Vote to Use This Feature!",
                description=f"This command requires voting! To access this feature, please vote for Dopamine here: [top.gg](https://top.gg/bot/{self.bot.user.id})",
                color=0xffaa00
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        async with self.acquire_hd_db() as db:
            await db.execute(
                "INSERT OR REPLACE INTO haiku_settings (guild_id, is_enabled) VALUES (?, 1)",
                (interaction.guild.id,)
            )
            await db.commit()

        self.enabled_guilds.add(interaction.guild.id)

        embed = discord.Embed(
            title="Haiku Detection Enabled",
            description="Haiku detection is now active across the server!\n\nI'll monitor messages and detect haikus automatically.",
            color=discord.Color.green()
        )
        embed.set_footer(text="Use /haiku detection disable to turn it off")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @detection_group.command(name="disable", description="Disable haiku detection for the server")
    @app_commands.check(slash_mod_check)
    async def disable_haiku_detection(self, interaction: discord.Interaction):
        if not await self.bot.get_cog('TopGGVoter').check_vote_access(interaction.user.id):
            embed = discord.Embed(
                title="Vote to Use This Feature!",
                description=f"This command requires voting! To access this feature, please vote for Dopamine here: [top.gg](https://top.gg/bot/{self.bot.user.id})",
                color=0xffaa00
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if interaction.guild.id not in self.enabled_guilds:
            embed = discord.Embed(
                title="Haiku Detection Not Active",
                description="Haiku detection is not currently enabled in this server.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        async with self.acquire_hd_db() as db:
            await db.execute("UPDATE haiku_settings SET is_enabled = 0 WHERE guild_id = ?", (interaction.guild.id,))
            await db.commit()

        self.enabled_guilds.discard(interaction.guild.id)

        embed = discord.Embed(
            title="Haiku Detection Disabled",
            description="Haiku detection has been disabled for this server.",
            color=discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @commands.command(name="update_haiku_database")
    async def update_haiku_database(self, ctx, *, data: str):
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
                if not entry: continue
                parts = entry.strip().split()
                if len(parts) < 2: continue

                try:
                    word = parts[0].lower().replace("'", "")
                    syllables = int(parts[1])
                    to_insert.append((word, syllables))
                    added_words.append(f"{word}: {syllables} syllables")
                except ValueError:
                    continue

            if to_insert:
                async with self.acquire_hwd_db() as db:
                    await db.executemany(
                        'INSERT OR REPLACE INTO haiku_words (word, syllables) VALUES (?, ?)',
                        to_insert,
                    )
                    await db.commit()
                for word, syllables in to_insert:
                    self.haiku_word_cache[word] = syllables

            if added_words:
                embed = discord.Embed(
                    title="Haiku Database Updated",
                    description=f"Successfully added/updated {len(added_words)} words:\n\n" +
                                "\n".join(added_words[:10]) +
                                (f"\n... and {len(added_words) - 10} more" if len(added_words) > 10 else ""),
                    color=discord.Color.green()
                )
            else:
                embed = discord.Embed(title="No Valid Entries", description="No valid word-syllable pairs were found.",
                                      color=discord.Color.red())

            await ctx.send(embed=embed)
        except Exception as e:
            await ctx.send(embed=discord.Embed(title="Error", description=f"An error occurred: {str(e)}",
                                               color=discord.Color.red()))

    @commands.command(name="view_haiku_dbcount")
    async def view_haiku_dbcount(self, ctx):
        if ctx.author.id != 758576879715483719: return
        count = len(self.haiku_word_cache)
        await ctx.send(
            embed=discord.Embed(description=f"Total words in cache/db: **{count}**", color=discord.Color.blue()))

    @commands.command(name="view_haiku_words")
    @commands.has_permissions(manage_messages=True)
    async def view_haiku_words(self, ctx):
        if ctx.author.id != 758576879715483719:
            return  # Silent fail or add original Access Denied embed

        words = sorted(self.haiku_word_cache.items())
        if not words:
            await ctx.send(embed=discord.Embed(title="Haiku Database Empty", color=discord.Color.orange()))
            return

        current_message = ""
        message_count = 1
        embed = discord.Embed(title=f"Haiku Database Words (Part {message_count})", color=discord.Color.green())

        for word, syllables in words:
            word_entry = f"**{word}**: {syllables} syllable{'s' if syllables != 1 else ''}\n"

            if len(current_message) + len(word_entry) > 2000:
                embed.description = current_message
                await ctx.send(embed=embed)
                message_count += 1
                embed = discord.Embed(title=f"Haiku Database Words (Part {message_count})", color=discord.Color.green())
                current_message = word_entry
            else:
                current_message += word_entry

        if current_message:
            embed.description = current_message
            embed.set_footer(text=f"Total: {len(words)} words")
            await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(HaikuDetector(bot))