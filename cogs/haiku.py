import asyncio
import re
from collections import deque
from typing import Deque, Dict, List, Optional, Set, Tuple

import discord
import inflect
import pronouncing
import syllapy
from beacon import beacon_commands
from discord import app_commands
from discord.ext import commands

from utils.data_protocol import DataDeleteResult, DataExportChunk, DataFeatureMeta, DataMonitorResult
from utils.discord_health import is_access_error, report_access_failure


class HaikuDetector(commands.Cog):
    SLANG_SYLLABLES = {
        "lol": 3, "idk": 3, "lmao": 4, "rofl": 2, "omg": 3,
        "wtf": 3, "brb": 3, "tbh": 3, "smh": 3, "afk": 3,
        "btw": 3, "fr": 2, "ngl": 3, "rn": 2, "imo": 3
    }

    ABBREVIATIONS = {
        "dr": "doctor", "mr": "mister", "mrs": "misses",
        "st": "street", "rd": "road", "ave": "avenue",
        "vs": "versus", "etc": "et cetera"
    }

    KNOWN_ACRONYMS = {"NASA", "NATO", "SCUBA", "LASER", "AWOL", "UNICEF", "OPEC", "GIF", "JPEG"}

    def __init__(self, bot):
        self.bot = bot
        self.haiku_word_cache: Dict[str, int] = {}
        self.enabled_guilds: Set[int] = set()

        self.haiku_queue: "asyncio.Queue[discord.Message]" = asyncio.Queue()
        self._worker_tasks: List[asyncio.Task] = []
        self._recent_processed_messages: Deque[int] = deque(maxlen=500)

        self.inflect_engine = inflect.engine()

    async def cog_load(self):
        await self.bot.db.wait_ready()
        await self.populate_caches()
        await self.start_workers()

    async def cog_unload(self):
        for task in self._worker_tasks:
            task.cancel()

        if self._worker_tasks:
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        self._worker_tasks.clear()

    async def populate_caches(self):
        rows = await self.bot.db.execute("SELECT guild_id FROM haiku_settings WHERE is_enabled = 1")
        self.enabled_guilds = {row["guild_id"] for row in rows}

        rows = await self.bot.db.execute("SELECT word, syllables FROM haiku_words")
        self.haiku_word_cache = {row["word"]: int(row["syllables"]) for row in rows}

    async def start_workers(self, worker_count: int = 5):
        if self._worker_tasks:
            return
        loop = asyncio.get_running_loop()
        for _ in range(worker_count):
            task = loop.create_task(self._haiku_worker())
            self._worker_tasks.append(task)

    async def _haiku_worker(self):
        while True:
            try:
                message: discord.Message = await self.haiku_queue.get()
                try:
                    if message.id in self._recent_processed_messages:
                        continue

                    word_data = await self.process_content_to_syllables(message)

                    formatted_haiku = await self.format_haiku(word_data)

                    if formatted_haiku:
                        already_replied = False
                        async for reply in message.channel.history(limit=5, after=message.created_at):
                            if (reply.author == self.bot.user and
                                    reply.reference and
                                    reply.reference.message_id == message.id):
                                already_replied = True
                                break

                        if not already_replied:
                            embed = discord.Embed(
                                description=f"\n*{formatted_haiku}*\n\n— {message.author.display_name}"
                            )
                            embed.set_footer(text="I detect Haikus. And sometimes, successfully. To disable, use /haiku detection disable.")
                            await message.reply(embed=embed)
                            self._recent_processed_messages.append(message.id)

                except Exception as e:
                    if is_access_error(e) and message.guild:
                        await report_access_failure(self.bot, message.guild.id, "haiku")
            except asyncio.CancelledError:
                break
            except:
                continue
            finally:
                try:
                    self.haiku_queue.task_done()
                except:
                    pass

    async def get_word_syllables(self, word: str, original_word: str = "") -> int:
        if not word:
            return 0

        if not original_word:
            original_word = word

        word_lower = word.lower().strip().strip(".:;?!\"'()")

        if word_lower in self.SLANG_SYLLABLES:
            return self.SLANG_SYLLABLES[word_lower]

        cached = self.haiku_word_cache.get(word_lower)
        if cached is not None:
            return cached

        if original_word.isupper() and len(original_word) > 1 and original_word not in self.KNOWN_ACRONYMS:
            total_syllables = sum(3 if char.lower() == 'w' else 1 for char in original_word if char.isalpha())
            return max(1, total_syllables)

        phones = pronouncing.phones_for_word(word_lower)
        if phones:
            return pronouncing.syllable_count(phones[0])

        syll_count = syllapy.count(word_lower)
        if syll_count > 0 and len(word_lower) < 10:
            return syll_count

        count = 0
        vowels = "aeiouy"
        temp_word = word_lower

        if temp_word.endswith("ed"):
            if len(temp_word) > 3 and temp_word[-3] in "td":
                pass
            else:
                temp_word = temp_word[:-2] + "d"

        if temp_word.endswith("e"):
            if not (temp_word.endswith("le") and len(temp_word) > 2 and temp_word[-3] not in vowels):
                temp_word = temp_word[:-1]

        vowel_runs = re.findall(r'[aeiouy]+', temp_word)
        for run in vowel_runs:
            count += 1
            if len(run) > 1:
                if run in ["ia", "eo", "io", "uo", "oa", "ua", "au", "ou", "ai"]:
                    count += 1

        if temp_word.endswith(("ism", "ier", "ian", "uity", "ium", "ogy", "ally")):
            count += 1

        return max(1, count)

    async def remove_urls(self, text: str) -> str:
        return re.sub(r'https?://\S+|www\.\S+', '', text)

    async def count_message_syllables(self, message: str) -> int:
        clean_content = await self.remove_urls(message)

        clean_content = re.sub(r'[-_–—]', ' ', clean_content)

        clean_content = re.sub(r'[^\w\s\']', ' ', clean_content)

        words = clean_content.split()

        total = 0
        for word in words:
            word = word.strip("'")
            if word:
                total += await self.get_word_syllables(word)
        return total

    async def process_content_to_syllables(self, message: discord.Message) -> List[Tuple[str, int]]:
        content = message.content

        content = re.sub(r'```.*?```', '', content, flags=re.DOTALL)
        content = re.sub(r'`.*?`', '', content)

        symbol_map = {
            "$": " dollar ", "&": " and ", "@": " at ", "%": " percent ", "+": " plus "
        }
        for symbol, replacement in symbol_map.items():
            content = content.replace(symbol, replacement)

        mention_pattern = r'<@!?(\d+)>'
        for match in re.finditer(mention_pattern, content):
            user_id = int(match.group(1))
            member = message.guild.get_member(user_id)
            display_name = member.display_name if member else "user"
            content = content.replace(match.group(0), display_name)

        content = re.sub(r'<a?:\w+:\d+>', '', content)
        content = await self.remove_urls(content)

        def replace_number(match):
            try:
                words = self.inflect_engine.number_to_words(match.group())
                return f" {words.replace('-', ' ')} "
            except Exception:
                return " "

        content = re.sub(r'\b\d+(?:[.,]\d+)?\b', replace_number, content)

        clean_content = re.sub(r'[-_–—]', ' ', content)
        words = clean_content.split()

        word_data = []
        for word in words:
            clean_word = re.sub(r'[^\w\s\']', '', word)

            if clean_word:
                lower_word = clean_word.lower()
                if lower_word in self.ABBREVIATIONS:
                    expanded = self.ABBREVIATIONS[lower_word].split()
                    for exp_word in expanded:
                        count = await self.get_word_syllables(exp_word, original_word=exp_word)
                        word_data.append((exp_word, count))
                else:
                    count = await self.get_word_syllables(clean_word, original_word=word)
                    word_data.append((word, count))

        return word_data

    async def format_haiku(self, word_data: List[Tuple[str, int]]) -> Optional[str]:
        lines = [[], [], []]
        targets = [5, 7, 5]
        current_line = 0
        current_sum = 0

        for word, count in word_data:
            if current_line > 2:
                return None

            current_sum += count
            lines[current_line].append(word)

            if current_sum == targets[current_line]:
                current_line += 1
                current_sum = 0
            elif current_sum > targets[current_line]:
                return None

        if current_line == 3 and current_sum == 0:
            formatted_lines = []
            for line in lines:
                text = " ".join(line)
                formatted_lines.append(text[0].upper() + text[1:])
            return "\n".join(formatted_lines)

        return None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return

        if message.guild.id not in self.enabled_guilds:
            return

        if message.id in self._recent_processed_messages:
            return

        content = message.content.strip()

        if content.startswith(('/', '!', '?', '.', '-', '$', '&', '%', '~')):
            return

        if re.search(r'(.)\1{4,}', content):
            return

        if any(len(word) > 25 for word in content.split()):
            return

        await self.haiku_queue.put(message)

    def data_features(self) -> list[DataFeatureMeta]:
        return [DataFeatureMeta(
            feature_id="haiku",
            name="Haiku Detection",
            guild_export=True,
            guild_delete=True,
        )]

    async def data_export_user(self, user_id: int, *, guild_ids: list[int] | None) -> DataExportChunk:
        return DataExportChunk(feature_id="haiku")

    async def data_export_guild(self, guild_id: int) -> DataExportChunk:
        chunk = DataExportChunk(feature_id="haiku")
        settings = await self.bot.db.execute(
            "SELECT * FROM haiku_settings WHERE guild_id = ?", (guild_id,)
        )
        chunk.guild_data[guild_id] = {"haiku_settings": settings}
        return chunk

    async def data_delete_user(self, user_id: int, *, guild_ids: list[int] | None, feature_id: str | None) -> DataDeleteResult:
        return DataDeleteResult(feature_id="haiku")

    async def data_delete_guild(self, guild_id: int, feature_id: str | None) -> DataDeleteResult:
        if feature_id and feature_id != "haiku":
            return DataDeleteResult(feature_id="haiku")

        async with self.bot.db.acquire_db() as db:
            count_rows = await db.execute(
                "SELECT COUNT(*) AS cnt FROM haiku_settings WHERE guild_id = ?", (guild_id,)
            )
            rows_affected = count_rows[0]["cnt"] if count_rows else 0
            await db.execute("DELETE FROM haiku_settings WHERE guild_id = ?", (guild_id,))
            await db.commit()

        self.enabled_guilds.discard(guild_id)
        return DataDeleteResult(feature_id="haiku", deleted=True, rows_affected=rows_affected)

    async def data_monitor_guild(self, guild: discord.Guild) -> DataMonitorResult:
        return DataMonitorResult(feature_id="haiku")

    haiku_group = app_commands.Group(name="haiku", description="Haiku detection commands")
    detection_group = beacon_commands.Group(name="detection", description="Haiku detection settings", parent=haiku_group, permissions_preset="automation")

    @detection_group.command(name="enable", description="Enable haiku detection for the whole server")
    async def enable_haiku_detection(self, interaction: discord.Interaction):
        if not await self.bot.get_cog('TopGGVoter').check_vote_access(interaction.user.id):
            embed = discord.Embed(
                title="Vote to Use This Feature!",
                description=f"This command requires voting! To access this feature, please vote for Dopamine here: [top.gg](https://top.gg/bot/{self.bot.user.id})",
                color=0xffaa00
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if interaction.guild.id in self.enabled_guilds:
            embed = discord.Embed(
                title="Haiku Detection Already Active",
                description="Haiku detection is already currently enabled in this server.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        await self.bot.db.execute(
            "INSERT OR REPLACE INTO haiku_settings (guild_id, is_enabled) VALUES (?, 1)",
            (interaction.guild.id,)
        )
        self.enabled_guilds.add(interaction.guild.id)

        embed = discord.Embed(
            title="Haiku Detection Enabled",
            description="Haiku detection is now active across the server!\n\nI'll monitor messages and detect haikus automatically.",
            color=discord.Color.green()
        )
        embed.set_footer(text="Use /haiku detection disable to turn it off")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @detection_group.command(name="disable", description="Disable haiku detection for the server")
    async def disable_haiku_detection(self, interaction: discord.Interaction):

        if interaction.guild.id not in self.enabled_guilds:
            embed = discord.Embed(
                title="Haiku Detection Not Active",
                description="Haiku detection is not currently enabled in this server.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        await self.bot.db.execute(
            "UPDATE haiku_settings SET is_enabled = 0 WHERE guild_id = ?",
            (interaction.guild.id,)
        )
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
            records_to_insert = []
            added_words = []

            for entry in entries:
                if not entry:
                    continue
                parts = entry.strip().split()
                if len(parts) < 2:
                    continue

                try:
                    word = parts[0].lower().replace("'", "")
                    syllables = int(parts[1])
                    records_to_insert.append((word, syllables))
                    added_words.append(f"{word}: {syllables} syllables")
                except ValueError:
                    continue

            if records_to_insert:
                async with self.bot.db.acquire_db() as db:
                    await db.executemany(
                        'INSERT OR REPLACE INTO haiku_words (word, syllables) VALUES (?, ?)',
                        records_to_insert,
                    )
                    await db.commit()

                for word, syllables in records_to_insert:
                    self.haiku_word_cache[word] = syllables

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
        if ctx.author.id != 758576879715483719:
            return
        count = len(self.haiku_word_cache)
        await ctx.send(
            embed=discord.Embed(description=f"Total words in cache/db: **{count}**", color=discord.Color.blue()))

    @commands.command(name="view_haiku_words")
    @commands.has_permissions(manage_messages=True)
    async def view_haiku_words(self, ctx):
        if ctx.author.id != 758576879715483719:
            return

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