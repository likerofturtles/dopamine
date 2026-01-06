import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiosqlite
import asyncio
from collections import deque
from typing import Optional, Dict, Tuple, Any
import time
from contextlib import asynccontextmanager
from config import SDB_PATH

class StarboardCog(commands.Cog):
    """Starboard and LFG functionality with optimized database handling."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.SDB_PATH = SDB_PATH
        self.STAR_EMOJI = "⭐"
        self.starred_messages: deque[int] = deque(maxlen=10000)
        self.lfg_creators: dict[int, int] = {}
        self.guild_cooldowns: dict[str, float] = {}
        self.lfg_message_times: dict[int, float] = {}

        self._max_lfg_entries: int = 5000
        self._max_guild_cooldowns: int = 5000

        self._db_pool: Optional[asyncio.Queue[aiosqlite.Connection]] = None
        self._db_pool_size: int = 5

        self._write_queue: asyncio.Queue[Tuple[str, Tuple[Any, ...], Dict[str, Any]]] = asyncio.Queue()
        self._writer_task: Optional[asyncio.Task] = None

        self._settings_cache: Dict[int, Tuple[dict, float]] = {}
        self._settings_cache_ttl: int = 300

        self._starboard_tasks: Dict[int, asyncio.Task] = {}

    def cleanup_old_cooldowns(self, cooldown_dict: dict, max_age_seconds: int):
        """Remove cooldown entries older than max_age_seconds to prevent memory leaks."""
        current_time = time.time()
        to_remove = [key for key, timestamp in cooldown_dict.items() if current_time - timestamp > max_age_seconds]
        for key in to_remove:
            del cooldown_dict[key]

    def cleanup_old_lfg_entries(self):
        """Remove old LFG entries (messages older than 24 hours) to prevent memory leaks"""
        current_time = time.time()
        max_age = 24 * 60 * 60
        to_remove = [msg_id for msg_id, created_time in self.lfg_message_times.items() 
                     if current_time - created_time > max_age]
        for msg_id in to_remove:
            self.lfg_creators.pop(msg_id, None)
            self.lfg_message_times.pop(msg_id, None)

    def _enforce_lfg_limits(self):
        """Ensure LFG-related state does not grow unbounded in memory."""
        if len(self.lfg_message_times) <= self._max_lfg_entries:
            return

        sorted_items = sorted(self.lfg_message_times.items(), key=lambda item: item[1])
        to_drop = sorted_items[:-self._max_lfg_entries]
        for msg_id, _ in to_drop:
            self.lfg_message_times.pop(msg_id, None)
            self.lfg_creators.pop(msg_id, None)

    def _enforce_guild_cooldown_limits(self):
        """Ensure guild cooldown cache has a hard size limit."""
        if len(self.guild_cooldowns) <= self._max_guild_cooldowns:
            return

        sorted_items = sorted(self.guild_cooldowns.items(), key=lambda item: item[1])
        to_drop = sorted_items[:-self._max_guild_cooldowns]
        for guild_id, _ in to_drop:
            self.guild_cooldowns.pop(guild_id, None)

    async def cog_load(self):
        """Initialize database on cog load."""
        await self.init_star_db()
        if not self._db_keepalive.is_running():
            self._db_keepalive.start()
        self._writer_task = asyncio.create_task(self._writer_loop())
        if not self._cache_cleanup.is_running():
            self._cache_cleanup.start()

    async def cog_unload(self):
        """Close database connections on cog unload."""
        if self._db_keepalive.is_running():
            self._db_keepalive.cancel()
        if self._cache_cleanup.is_running():
            self._cache_cleanup.cancel()
        if self._writer_task:
            self._writer_task.cancel()
            try:
                await self._writer_task
            except asyncio.CancelledError:
                pass

        if self._db_pool is not None:
            while not self._db_pool.empty():
                conn = await self._db_pool.get()
                try:
                    await conn.close()
                except Exception:
                    pass

    async def _init_db_pool(self):
        """Create a pool of SQLite connections with optimized settings for I/O performance."""
        if self._db_pool is not None:
            return

        pool: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue(maxsize=self._db_pool_size)
        max_retries = 5

        for _ in range(self._db_pool_size):
            for attempt in range(max_retries):
                try:
                    conn = await aiosqlite.connect(self.SDB_PATH, timeout=5.0)
                    await conn.execute("PRAGMA busy_timeout=5000")
                    await conn.execute("PRAGMA journal_mode=WAL")
                    await conn.execute("PRAGMA wal_autocheckpoint=1000")
                    await conn.execute("PRAGMA synchronous=NORMAL")
                    await conn.execute("PRAGMA cache_size=-64000")
                    await conn.execute("PRAGMA foreign_keys=ON")
                    await conn.commit()
                    await pool.put(conn)
                    break
                except Exception:
                    if attempt < max_retries - 1:
                        await asyncio.sleep(0.1 * (2 ** attempt))
                        continue
                    else:
                        raise

        self._db_pool = pool

    @asynccontextmanager
    async def get_db_connection(self):
        """
        Acquire a connection from the pool.

        This replaces the previous single persistent connection with
        a small pool of reusable connections.
        """
        if self._db_pool is None:
            await self._init_db_pool()

        assert self._db_pool is not None
        conn = await self._db_pool.get()
        try:
            yield conn
        finally:
            try:
                await conn.execute("SELECT 1")
                await self._db_pool.put(conn)
            except Exception:
                try:
                    await conn.close()
                except Exception:
                    pass
                self._db_pool = None

    @asynccontextmanager
    async def read_db_connection(self):
        """
        Short-lived read-optimized connection for concurrent readers.

        Using separate connections for reads allows many concurrent read
        operations without contending with the single writer connection.
        """
        conn: Optional[aiosqlite.Connection] = None
        max_retries = 5
        for attempt in range(max_retries):
            try:
                conn = await aiosqlite.connect(self.SDB_PATH, timeout=5.0)
                await conn.execute("PRAGMA busy_timeout=5000")
                await conn.execute("PRAGMA journal_mode=WAL")
                await conn.execute("PRAGMA synchronous=OFF")
                await conn.execute("PRAGMA cache_size=-64000")
                await conn.execute("PRAGMA foreign_keys=ON")
                break
            except Exception:
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.1 * (2 ** attempt))
                    continue
                raise

        try:
            yield conn
        finally:
            if conn is not None:
                await conn.close()

    @tasks.loop(seconds=60)
    async def _db_keepalive(self):
        try:
            async with self.get_db_connection() as db:
                cur = await db.execute("SELECT 1")
                await cur.fetchone()
                await cur.close()
        except Exception:
            pass

    async def _writer_loop(self):
        """Background task that serializes all DB writes from the queue."""
        try:
            while True:
                op_name, args, kwargs = await self._write_queue.get()
                try:
                    if op_name == "upsert_star_post":
                        await self._execute_upsert_star_post(*args, **kwargs)
                    elif op_name == "delete_star_post":
                        await self._execute_delete_star_post(*args, **kwargs)
                    elif op_name == "update_guild_setting":
                        await self._execute_update_guild_setting(*args, **kwargs)
                except Exception:
                    pass
                finally:
                    self._write_queue.task_done()
        except asyncio.CancelledError:
            while not self._write_queue.empty():
                try:
                    self._write_queue.get_nowait()
                    self._write_queue.task_done()
                except asyncio.QueueEmpty:
                    break
            raise

    @tasks.loop(minutes=5)
    async def _cache_cleanup(self):
        """Periodically clean up expired guild settings and stale LFG entries/cooldowns."""
        now = time.time()
        expired = [gid for gid, (_, exp) in self._settings_cache.items() if exp <= now]
        for gid in expired:
            self._settings_cache.pop(gid, None)

        self.cleanup_old_lfg_entries()
        self.cleanup_old_cooldowns(self.guild_cooldowns, max_age_seconds=600)

    async def init_star_db(self):
        """Initialize database with optimized schema and indexes."""
        async with self.get_db_connection() as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id TEXT PRIMARY KEY,
                    star_threshold INTEGER DEFAULT 3,
                    starboard_channel_id TEXT,
                    lfg_threshold INTEGER DEFAULT 4
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS star_posts (
                    guild_id TEXT NOT NULL,
                    source_message_id TEXT NOT NULL,
                    starboard_message_id TEXT NOT NULL,
                    PRIMARY KEY (guild_id, source_message_id)
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_star_posts_guild 
                ON star_posts(guild_id)
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_star_posts_lookup 
                ON star_posts(guild_id, source_message_id)
            """)
            await db.commit()

    async def get_guild_settings(self, guild_id: int) -> dict:
        """Get guild settings with TTL cache and read-optimized DB access."""
        now = time.time()
        cached = self._settings_cache.get(guild_id)
        if cached:
            settings, expires_at = cached
            if expires_at > now:
                return settings

        gid_str = str(guild_id)
        async with self.read_db_connection() as db:
            cursor = await db.execute(
                "SELECT star_threshold, starboard_channel_id, lfg_threshold FROM guild_settings WHERE guild_id = ?",
                (gid_str,)
            )
            row = await cursor.fetchone()
            await cursor.close()

        if not row:
            async with self.get_db_connection() as writer_db:
                await writer_db.execute(
                    "INSERT INTO guild_settings (guild_id) VALUES (?)",
                    (gid_str,)
                )
                await writer_db.commit()
            settings = {"star_threshold": 3, "starboard_channel_id": None, "lfg_threshold": 4}
        else:
            settings = {
                "star_threshold": row[0],
                "starboard_channel_id": row[1],
                "lfg_threshold": row[2]
            }

        self._settings_cache[guild_id] = (settings, now + self._settings_cache_ttl)
        return settings

    def build_starboard_embed(self, message: discord.Message, star_count: int) -> discord.Embed:
        """Build starboard embed with optimized image detection."""
        text = message.content.strip() if message.content else ""
        embed = discord.Embed(description=text, color=discord.Color.gold())

        embed.set_author(
            name=message.author.display_name,
            icon_url=message.author.display_avatar.url
        )
        embed.add_field(
            name="Jump to Message",
            value=f"[Click Here]({message.jump_url})",
            inline=False
        )
        embed.set_footer(text=f"{star_count} ⭐ | #{message.channel.name}")

        image_url = None

        for att in message.attachments:
            if att.content_type and att.content_type.startswith("image/"):
                image_url = att.url
                break

        if not image_url:
            for e in message.embeds:
                if e.image and e.image.url:
                    image_url = e.image.url
                    break
                if e.thumbnail and e.thumbnail.url:
                    image_url = e.thumbnail.url
                    break
                if e.type == "image" and e.url:
                    image_url = e.url
                    break

        if image_url:
            embed.set_image(url=image_url)

        return embed

    async def get_star_post(self, guild_id: int, source_message_id: int) -> Optional[int]:
        """Get starboard post ID with optimized query using read-only connection."""
        async with self.read_db_connection() as db:
            cursor = await db.execute(
                "SELECT starboard_message_id FROM star_posts WHERE guild_id = ? AND source_message_id = ?",
                (str(guild_id), str(source_message_id))
            )
            row = await cursor.fetchone()
            await cursor.close()
        return int(row[0]) if row else None

    async def upsert_star_post(self, guild_id: int, source_message_id: int, starboard_message_id: int):
        """Queue an upsert of a starboard post."""
        await self._write_queue.put((
            "upsert_star_post",
            (guild_id, source_message_id, starboard_message_id),
            {}
        ))

    async def delete_star_post(self, guild_id: int, source_message_id: int):
        """Queue deletion of a starboard post."""
        await self._write_queue.put((
            "delete_star_post",
            (guild_id, source_message_id),
            {}
        ))

    async def update_guild_setting(self, guild_id: int, **kwargs):
        """Queue update of guild settings with dynamic fields (optimized batch update)."""
        if not kwargs:
            return

        cached = self._settings_cache.get(guild_id)
        if cached:
            settings, _ = cached
            settings.update(kwargs)
            self._settings_cache[guild_id] = (settings, time.time() + self._settings_cache_ttl)

        await self._write_queue.put((
            "update_guild_setting",
            (guild_id,),
            {"fields": kwargs}
        ))

    async def _execute_upsert_star_post(self, guild_id: int, source_message_id: int, starboard_message_id: int):
        """Actual DB write for upsert_star_post, executed in writer loop."""
        async with self.get_db_connection() as db:
            await db.execute(
                """INSERT INTO star_posts (guild_id, source_message_id, starboard_message_id) 
                   VALUES (?, ?, ?) 
                   ON CONFLICT(guild_id, source_message_id) 
                   DO UPDATE SET starboard_message_id = excluded.starboard_message_id""",
                (str(guild_id), str(source_message_id), str(starboard_message_id))
            )
            await db.commit()

    async def _execute_delete_star_post(self, guild_id: int, source_message_id: int):
        """Actual DB write for delete_star_post, executed in writer loop."""
        async with self.get_db_connection() as db:
            await db.execute(
                "DELETE FROM star_posts WHERE guild_id = ? AND source_message_id = ?",
                (str(guild_id), str(source_message_id))
            )
            await db.commit()

    async def _execute_update_guild_setting(self, guild_id: int, *, fields: dict):
        """Actual DB write for update_guild_setting, executed in writer loop."""
        if not fields:
            return
        set_clause = ", ".join(f"{key} = ?" for key in fields.keys())
        values = list(fields.values()) + [str(guild_id)]

        async with self.get_db_connection() as db:
            await db.execute(
                f"UPDATE guild_settings SET {set_clause} WHERE guild_id = ?",
                values
            )
            await db.commit()

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction: discord.Reaction, user: discord.User):
        """Handle LFG pings on reaction threshold."""
        if user.bot:
            return

        message = reaction.message
        guild = message.guild
        if not guild:
            return

        emoji = str(reaction.emoji)

        if emoji == self.STAR_EMOJI and message.id in self.lfg_creators:
            settings = await self.get_guild_settings(guild.id)
            creator_id = self.lfg_creators[message.id]
            creator = guild.get_member(creator_id)

            reaction_count = reaction.count
            if reaction.me:
                reaction_count -= 1

            if reaction_count >= settings["lfg_threshold"]:
                reactors = [u async for u in reaction.users() if not u.bot]
                mentions = " ".join(u.mention for u in reactors)
                ping_content = mentions or "LFG threshold reached, but I couldn't find anyone to ping."

                embed = discord.Embed(
                    title="LFG Group Ready!",
                    description=(
                        f"**Created by:** {creator.mention if creator else 'Unknown User'}"
                    ),
                    color=discord.Color.green()
                )
                await message.channel.send(
                    content=ping_content,
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions(users=True)
                )
                self.lfg_creators.pop(message.id, None)
                self.lfg_message_times.pop(message.id, None)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Handle starboard reaction additions with debounced processing."""
        if payload.user_id == self.bot.user.id:
            return
        if str(payload.emoji) != self.STAR_EMOJI:
            return
        self._schedule_starboard_update(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        """Handle starboard updates on reaction removal with debounced processing."""
        if str(payload.emoji) != self.STAR_EMOJI:
            return
        self._schedule_starboard_update(payload)

    def _schedule_starboard_update(self, payload: discord.RawReactionActionEvent):
        """Debounce starboard updates per message to coalesce rapid reactions."""
        message_id = payload.message_id
        existing = self._starboard_tasks.get(message_id)
        if existing and not existing.done():
            existing.cancel()

        task = self.bot.loop.create_task(self._process_starboard_payload(payload))
        self._starboard_tasks[message_id] = task

    async def _process_starboard_payload(self, payload: discord.RawReactionActionEvent):
        """Shared logic for processing starboard updates for both add and remove events."""
        await asyncio.sleep(0.5)

        try:
            guild = self.bot.get_guild(payload.guild_id)
            if not guild:
                return

            settings = await self.get_guild_settings(guild.id)
            starboard_channel_id = settings.get("starboard_channel_id")
            if not starboard_channel_id:
                return
            if str(payload.channel_id) == str(starboard_channel_id):
                return

            try:
                source_channel = guild.get_channel(payload.channel_id) or await guild.fetch_channel(payload.channel_id)
                message = await source_channel.fetch_message(payload.message_id)
            except Exception:
                return

            if message.author.bot:
                existing_id = await self.get_star_post(guild.id, message.id)
                if existing_id:
                    try:
                        sb_chan = guild.get_channel(int(starboard_channel_id)) or await guild.fetch_channel(
                            int(starboard_channel_id))
                        sb_msg = await sb_chan.fetch_message(existing_id)
                        await sb_msg.delete()
                    except Exception:
                        pass
                    await self.delete_star_post(guild.id, message.id)
                return

            star_reaction = next((r for r in message.reactions if str(r.emoji) == self.STAR_EMOJI), None)
            count = star_reaction.count if star_reaction else 0

            if count < settings["star_threshold"]:
                existing_id = await self.get_star_post(guild.id, message.id)
                if existing_id:
                    try:
                        sb_chan = guild.get_channel(int(starboard_channel_id)) or await guild.fetch_channel(
                            int(starboard_channel_id))
                        sb_msg = await sb_chan.fetch_message(existing_id)
                        await sb_msg.delete()
                    except Exception:
                        pass
                    await self.delete_star_post(guild.id, message.id)
                return

            try:
                sb_chan = guild.get_channel(int(starboard_channel_id)) or await guild.fetch_channel(
                    int(starboard_channel_id))
            except Exception:
                return

            embed = self.build_starboard_embed(message, count)
            existing_id = await self.get_star_post(guild.id, message.id)

            if existing_id:
                try:
                    sb_msg = await sb_chan.fetch_message(existing_id)
                    await sb_msg.edit(embed=embed)
                except discord.NotFound:
                    try:
                        sb_msg = await sb_chan.send(embed=embed)
                        await self.upsert_star_post(guild.id, message.id, sb_msg.id)
                    except Exception:
                        pass
                except Exception:
                    pass
            else:
                try:
                    sb_msg = await sb_chan.send(embed=embed)
                    await self.upsert_star_post(guild.id, message.id, sb_msg.id)
                    self.starred_messages.append(message.id)
                except Exception:
                    pass
        finally:
            self._starboard_tasks.pop(payload.message_id, None)

    @commands.Cog.listener()
    async def on_raw_reaction_clear(self, payload: discord.RawReactionClearEvent):
        """Handle all reactions being cleared."""
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        existing_id = await self.get_star_post(payload.guild_id, payload.message_id)
        if not existing_id:
            return

        try:
            settings = await self.get_guild_settings(guild.id)
            sb_chan = guild.get_channel(int(settings["starboard_channel_id"])) or await guild.fetch_channel(
                int(settings["starboard_channel_id"]))
            sb_msg = await sb_chan.fetch_message(existing_id)
            await sb_msg.delete()
        except Exception:
            pass

        await self.delete_star_post(payload.guild_id, payload.message_id)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        """Update starboard posts when source message is edited."""
        if not after.guild:
            return

        existing_id = await self.get_star_post(after.guild.id, after.id)

        if after.author.bot:
            if existing_id:
                try:
                    settings = await self.get_guild_settings(after.guild.id)
                    sb_chan = after.guild.get_channel(
                        int(settings["starboard_channel_id"])) or await after.guild.fetch_channel(
                        int(settings["starboard_channel_id"]))
                    sb_msg = await sb_chan.fetch_message(existing_id)
                    await sb_msg.delete()
                except Exception:
                    pass
                await self.delete_star_post(after.guild.id, after.id)
            return

        if not existing_id:
            return

        star_reaction = next((r for r in after.reactions if str(r.emoji) == self.STAR_EMOJI), None)
        count = star_reaction.count if star_reaction else 0

        settings = await self.get_guild_settings(after.guild.id)
        starboard_channel_id = settings.get("starboard_channel_id")
        if not starboard_channel_id:
            return

        try:
            sb_chan = after.guild.get_channel(int(starboard_channel_id)) or await after.guild.fetch_channel(
                int(starboard_channel_id))
            embed = self.build_starboard_embed(after, count)
            sb_msg = await sb_chan.fetch_message(existing_id)
            await sb_msg.edit(embed=embed)
        except Exception:
            pass

    starboard_group = app_commands.Group(name="starboard", description="Starboard configuration commands")

    @starboard_group.command(name="set_channel", description="Set the starboard channel")
    @app_commands.describe(channel="The channel where starred messages will be sent")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def starboard_set_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        """Set the starboard channel."""
        await self.get_guild_settings(interaction.guild.id)
        await self.update_guild_setting(interaction.guild.id, starboard_channel_id=str(channel.id))

        embed = discord.Embed(
            title="Starboard Channel Set",
            description=f"Starboard messages will now be sent to {channel.mention}.",
            color=discord.Color(0x337fd5)
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @starboard_group.command(name="threshold", description="Set the star threshold for starboard")
    @app_commands.describe(amount="Number of stars required for a message to appear on starboard")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def starboard_threshold(self, interaction: discord.Interaction, amount: int):
        """Set starboard threshold."""
        if amount < 1:
            await interaction.response.send_message("Threshold must be at least 1.", ephemeral=True)
            return

        await self.get_guild_settings(interaction.guild.id)
        await self.update_guild_setting(interaction.guild.id, star_threshold=amount)

        embed = discord.Embed(
            title="Starboard Threshold Updated",
            description=f"Messages now need **{amount}** ⭐ reactions to appear on starboard.",
            color=discord.Color(0x337fd5)
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    lfg_group = app_commands.Group(name="lfg", description="Looking For Group commands")

    @lfg_group.command(name="create", description="Create an LFG post that pings reactors when threshold is reached")
    async def lfg_create(self, interaction: discord.Interaction):
        """Create an LFG post."""
        guild_id = str(interaction.guild.id)
        now = time.time()
        cooldown_period = 60

        last_used = self.guild_cooldowns.get(guild_id, 0)
        remaining = cooldown_period - (now - last_used)
        if remaining > 0:
            embed = discord.Embed(
                description=f"This command is under cooldown. Please wait **{int(remaining)}** seconds.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        self.cleanup_old_cooldowns(self.guild_cooldowns, 120)
        
        self.guild_cooldowns[guild_id] = now

        self.cleanup_old_lfg_entries()

        settings = await self.get_guild_settings(interaction.guild.id)
        embed = discord.Embed(
            title="Looking For Group!",
            description=(
                f"React with {self.STAR_EMOJI} to join this group.\n"
                f"When **{settings['lfg_threshold']}** people react, everyone will be pinged!\n\n"
                f"**Created by:** {interaction.user.mention}"
            ),
            color=discord.Color(0x337fd5)
        )

        msg = await interaction.channel.send(embed=embed)
        await msg.add_reaction(self.STAR_EMOJI)

        self.lfg_creators[msg.id] = interaction.user.id
        self.lfg_message_times[msg.id] = time.time()

        confirm_embed = discord.Embed(
            title="LFG Post Created",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=confirm_embed, ephemeral=True)

    @lfg_group.command(name="threshold", description="Set the reaction threshold for LFG pings")
    @app_commands.describe(amount="Number of reactions needed to trigger LFG ping")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def lfg_threshold(self, interaction: discord.Interaction, amount: int):
        """Set LFG threshold."""
        if amount < 1:
            await interaction.response.send_message("Threshold must be at least 1.", ephemeral=True)
            return

        await self.get_guild_settings(interaction.guild.id)
        await self.update_guild_setting(interaction.guild.id, lfg_threshold=amount)

        embed = discord.Embed(
            title="LFG Threshold Updated",
            description=f"LFG posts now need **{amount}** ⭐ reactions to ping all members.",
            color=discord.Color(0x337fd5)
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @commands.command(name="teststarboard")
    async def teststarboard(self, ctx: commands.Context):
        """Test starboard functionality (restricted command)."""
        if ctx.author.id != 758576879715483719:
            return

        if not ctx.message.reference or not ctx.message.reference.message_id:
            return

        try:
            ref = ctx.message.reference
            channel = ctx.channel if ref.channel_id == ctx.channel.id else (
                    ctx.guild.get_channel(ref.channel_id) or await ctx.guild.fetch_channel(ref.channel_id)
            )
            target_message = await channel.fetch_message(ref.message_id)
        except Exception:
            return

        settings = await self.get_guild_settings(ctx.guild.id)
        starboard_channel_id = settings.get("starboard_channel_id")
        if not starboard_channel_id:
            return

        if str(starboard_channel_id) == str(channel.id):
            return

        try:
            starboard_channel = ctx.guild.get_channel(int(starboard_channel_id)) or await ctx.guild.fetch_channel(
                int(starboard_channel_id))
        except Exception:
            return

        star_reaction = next((r for r in target_message.reactions if str(r.emoji) == self.STAR_EMOJI), None)
        current_count = star_reaction.count if star_reaction else 0

        embed = self.build_starboard_embed(target_message, current_count)

        try:
            await starboard_channel.send(embed=embed)
        except Exception:
            pass

async def setup(bot):
    await bot.add_cog(StarboardCog(bot))
