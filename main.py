# very sexy moderation bot developed by an individual with a sexy girlfriend.py
import os
import aiohttp
import logging
import asyncio
import time
import sys
import signal
import aiosqlite
from contextlib import asynccontextmanager
import discord
import psutil
from discord.ext import commands, tasks
from discord import app_commands
from discord.ui import View, Button
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
from typing import Optional

from config import DB_PATH, VDB_PATH, SMDB_PATH, SDB_PATH, ARDB_PATH, MCTDB_PATH, STICKYDB_PATH, TOPDB_PATH, HDDB_PATH, HWDDB_PATH, NOTEDB_PATH
from utils.checks import mod_check, slash_mod_check, guild_check

from VERSION import bot_version

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
TOPGGTOKEN = os.getenv("TOPGG_TOKEN")
if not TOKEN:
    raise SystemExit("Set DISCORD_TOKEN in .env")

def signal_handler(sig, frame):
    print("\nBot shutdown requested...")
    print("👋 Goodbye!")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

logger = logging.getLogger("discord")
log_path = os.path.join(os.path.dirname(__file__), "discord.log")
handler = logging.FileHandler(filename=log_path, encoding="utf-8", mode="a")
logger.setLevel(logging.DEBUG)
logger.addHandler(handler)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.reactions = True

bot = commands.Bot(command_prefix="!!", intents=intents, help_command=None)

bot.guild_cooldowns = {}
bot.lfg_creators = {}

POINTVALUES_CACHE_TTL = 300
LOG_CHANNEL_CACHE_TTL = 300
WELCOME_CHANNEL_CACHE_TTL = 300

pointvalues_cache: dict[str, tuple[list[int], float]] = {}
log_channel_cache: dict[int, tuple[int | None, float]] = {}
welcome_channel_cache: dict[int, tuple[int | None, float]] = {}


def _cleanup_ttl_cache(cache: dict):
    if not cache:
        return
    now = time.time()
    to_remove = [key for key, (_, expires_at) in cache.items() if expires_at <= now]
    for key in to_remove:
        cache.pop(key, None)


class AioSqliteConnectionPool:

    def __init__(self, db_path: str, max_size: int = 5):
        self._db_path = db_path
        self._max_size = max_size
        self._queue: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue()
        self._initialized = False
        self._lock = asyncio.Lock()

    async def _create_connection(self) -> aiosqlite.Connection:
        conn = await aiosqlite.connect(self._db_path, timeout=30.0)
        await conn.execute("PRAGMA busy_timeout=30000")
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA cache_size=-64000")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.commit()
        return conn

    async def init(self):
        if self._initialized:
            return
        async with self._lock:
            if self._initialized:
                return
            for _ in range(self._max_size):
                conn = await self._create_connection()
                await self._queue.put(conn)
            self._initialized = True

    @asynccontextmanager
    async def acquire(self):
        if not self._initialized:
            await self.init()
        conn = await self._queue.get()
        try:
            yield conn
        finally:
            await self._queue.put(conn)

    async def close(self):
        while not self._queue.empty():
            conn = await self._queue.get()
            try:
                await conn.close()
            except Exception:
                pass
        self._initialized = False

def cleanup_old_cooldowns(cooldown_dict: dict, max_age_seconds: int):
    current_time = time.time()
    to_remove = [key for key, timestamp in cooldown_dict.items() if current_time - timestamp > max_age_seconds]
    for key in to_remove:
        del cooldown_dict[key]


core_db_pool: Optional[AioSqliteConnectionPool] = None
values_db_pool: Optional[AioSqliteConnectionPool] = None


def get_core_db_pool() -> AioSqliteConnectionPool:
    global core_db_pool
    if core_db_pool is None:
        core_db_pool = AioSqliteConnectionPool(DB_PATH, max_size=5)
    return core_db_pool


def get_values_db_pool() -> AioSqliteConnectionPool:
    global values_db_pool
    if values_db_pool is None:
        values_db_pool = AioSqliteConnectionPool(VDB_PATH, max_size=3)
    return values_db_pool

async def close_db_connections():
    global core_db_pool, values_db_pool
    if core_db_pool:
        await core_db_pool.close()
        core_db_pool = None
    if values_db_pool:
        await values_db_pool.close()
        values_db_pool = None


async def init_core_db():
    async with get_core_db_pool().acquire() as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            guild_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            points INTEGER NOT NULL DEFAULT 0,
            last_punishment INTEGER,
            last_decay INTEGER,
            PRIMARY KEY (guild_id, user_id)
        );
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS ban_schedule (
            guild_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            unban_at INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        );
        """)

        await db.execute("CREATE INDEX IF NOT EXISTS idx_ban_schedule_unban_at ON ban_schedule(unban_at);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_users_guild_user ON users(guild_id, user_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_users_points ON users(points);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_users_last_punishment ON users(last_punishment);")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS perma_bans (
            guild_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            unban_pending INTEGER DEFAULT 0,
            applied INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        );
        """)

        await db.execute("CREATE INDEX IF NOT EXISTS idx_perma_bans_lookup ON perma_bans(guild_id, user_id);")

        await db.commit()


def now_ts():
    return int(datetime.now(timezone.utc).timestamp())

def ts_to_dt(ts: int):
    return datetime.fromtimestamp(ts, tz=timezone.utc)

def dt_to_ts(dt: datetime):
    return int(dt.replace(tzinfo=timezone.utc).timestamp())



_db_initialized = False


async def init_values_db():
    global _db_initialized

    if _db_initialized:
        return

    try:
        async with get_values_db_pool().acquire() as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS guild_pointvalues (
                    guild_id TEXT PRIMARY KEY,
                    p1 INTEGER DEFAULT 1,
                    p2 INTEGER DEFAULT 2,
                    p3 INTEGER DEFAULT 3,
                    p4 INTEGER DEFAULT 4,
                    p5 INTEGER DEFAULT 5,
                    p6 INTEGER DEFAULT 6,
                    p7 INTEGER DEFAULT 7,
                    p8 INTEGER DEFAULT 8,
                    p9 INTEGER DEFAULT 9,
                    p10 INTEGER DEFAULT 10,
                    p11 INTEGER DEFAULT 11,
                    p12 INTEGER DEFAULT 12
                )
            """)
            await db.commit()
        _db_initialized = True
    except Exception as e:
        print(f"Error initializing database: {e}")
        raise


async def get_guild_pointvalues(guild_id: str):
    await init_values_db()

    now = time.time()
    _cleanup_ttl_cache(pointvalues_cache)
    cached = pointvalues_cache.get(guild_id)
    if cached:
        values, expires_at = cached
        if now < expires_at:
            return values
        else:
            pointvalues_cache.pop(guild_id, None)

    try:
        async with get_values_db_pool().acquire() as db:
            cursor = await db.execute(
                "SELECT * FROM guild_pointvalues WHERE guild_id = ?",
                (guild_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if not row:
            async with get_values_db_pool().acquire() as db:
                await db.execute(
                    "INSERT INTO guild_pointvalues (guild_id) VALUES (?)",
                    (guild_id,),
                )
                await db.commit()
            values = [1,2,3,4,5,6,7,8,9,10,11,12]
        else:
            values = list(row[1:])

        pointvalues_cache[guild_id] = (values, now + POINTVALUES_CACHE_TTL)
        return values
    except Exception as e:
        print(f"Error getting guild point values: {e}")
        return [1,2,3,4,5,6,7,8,9,10,11,12]

async def update_guild_pointvalue(guild_id: str, index: int, new_value: int):
    await init_values_db()

    if not (1 <= index <= 12):
        raise ValueError("Index must be between 1 and 12")
    if not (0 <= new_value <= 1000):
        raise ValueError("Value must be between 0 and 1000 (0 = disabled)")

    try:
        async with get_values_db_pool().acquire() as db:
            await db.execute(
                "INSERT OR IGNORE INTO guild_pointvalues (guild_id) VALUES (?)",
                (guild_id,),
            )
            await db.commit()

            col = f"p{index}"
            await db.execute(
                f"UPDATE guild_pointvalues SET {col} = ? WHERE guild_id = ?",
                (new_value, guild_id),
            )
            await db.commit()
        pointvalues_cache.pop(guild_id, None)
    except Exception as e:
        print(f"Error updating guild point value: {e}")
        raise


async def get_punishment(points: int, guild_id: str):
    thresholds = await get_guild_pointvalues(guild_id)
    punishments = {
        1: ("warning", None),
        2: ("timeout", timedelta(minutes=15)),
        3: ("timeout", timedelta(minutes=30)),
        4: ("timeout", timedelta(minutes=45)),
        5: ("timeout", timedelta(minutes=60)),
        6: ("ban", timedelta(hours=12)),
        7: ("ban", timedelta(hours=12)),
        8: ("ban", timedelta(days=1)),
        9: ("ban", timedelta(days=3)),
        10: ("ban", timedelta(days=7)),
        11: ("ban", timedelta(days=7)),
        12: ("ban", None),
    }

    prev_punishment_idx = 1
    for i, threshold in enumerate(thresholds, start=1):
        if threshold == 0:
            continue
        if points < threshold:
            return punishments[prev_punishment_idx]
        prev_punishment_idx = i
    
    return punishments[prev_punishment_idx]


def format_punishment_text(action: str | None, duration: timedelta | None) -> tuple[str | None, str | None, str]:
    if action and action.lower() == "ban":
        if duration is None:
            action_text = "banned permanently"
        else:
            action_text = "banned"
    else:
        action_text = action

    duration_text = None
    if action in ["timeout", "ban"] and duration is not None:
        total_seconds = int(duration.total_seconds()) if isinstance(duration, timedelta) else int(duration)
        if total_seconds < 60:
            duration_text = f"{total_seconds} second{'s' if total_seconds != 1 else ''}"
        elif total_seconds < 3600:
            minutes = total_seconds // 60
            duration_text = f"{minutes} minute{'s' if minutes != 1 else ''}"
        elif total_seconds < 86400:
            hours = total_seconds // 3600
            duration_text = f"{hours} hour{'s' if hours != 1 else ''}"
        else:
            days = total_seconds // 86400
            duration_text = f"{days} day{'s' if days != 1 else ''}"

    if duration_text:
        punishment_text = f"{action_text} for {duration_text}"
    else:
        punishment_text = f"{action_text}" if action_text else "No punishment"

    return action_text, duration_text, punishment_text

async def db_get_user(guild_id: str, user_id: str):
    async with get_core_db_pool().acquire() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        result = await cursor.fetchone()
        await cursor.close()
        return result


async def db_create_or_update_user(guild_id: str, user_id: str, points: int, last_punishment_ts: int = None, last_decay_ts: int = None):
    async with get_core_db_pool().acquire() as db:
        await db.execute("""
            INSERT INTO users(guild_id, user_id, points, last_punishment, last_decay)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
              points=excluded.points,
              last_punishment=excluded.last_punishment,
              last_decay=excluded.last_decay
        """, (guild_id, user_id, points, last_punishment_ts, last_decay_ts))
        await db.commit()


async def db_update_points(guild_id: str, user_id: str, new_points: int):
    async with get_core_db_pool().acquire() as db:
        await db.execute("UPDATE users SET points = ? WHERE guild_id = ? AND user_id = ?", (new_points, guild_id, user_id))
        await db.commit()


async def db_update_last_punishment(guild_id: str, user_id: str, ts: int):
    async with get_core_db_pool().acquire() as db:
        await db.execute("UPDATE users SET last_punishment = ?, last_decay = NULL WHERE guild_id = ? AND user_id = ?", (ts, guild_id, user_id))
        await db.commit()


async def db_set_last_decay(guild_id: str, user_id: str, ts: int):
    async with get_core_db_pool().acquire() as db:
        await db.execute("UPDATE users SET last_decay = ? WHERE guild_id = ? AND user_id = ?", (ts, guild_id, user_id))
        await db.commit()


async def schedule_unban_in_db(guild_id: str, user_id: str, unban_at_ts: int):
    async with get_core_db_pool().acquire() as db:
        await db.execute("INSERT OR REPLACE INTO ban_schedule(guild_id, user_id, unban_at) VALUES (?, ?, ?)",
                         (guild_id, user_id, unban_at_ts))
        await db.commit()


async def unschedule_unban_in_db(guild_id: str, user_id: str):
    async with get_core_db_pool().acquire() as db:
        await db.execute("DELETE FROM ban_schedule WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        await db.commit()


async def insert_perma_ban_record(guild_id: str, user_id: str):
    async with get_core_db_pool().acquire() as db:
        await db.execute("INSERT OR REPLACE INTO perma_bans(guild_id, user_id, unban_pending, applied) VALUES (?, ?, 0, 0)",
                         (guild_id, user_id))
        await db.commit()


async def mark_perma_unban_pending(guild_id: str, user_id: str):
    async with get_core_db_pool().acquire() as db:
        await db.execute("UPDATE perma_bans SET unban_pending = 1 WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        await db.commit()


async def mark_perma_applied(guild_id: str, user_id: str):
    async with get_core_db_pool().acquire() as db:
        await db.execute("UPDATE perma_bans SET applied = 1 WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        await db.commit()


async def get_pending_perma_for_user_guild(guild_id: str, user_id: str):
    async with get_core_db_pool().acquire() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM perma_bans WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        result = await cursor.fetchone()
        await cursor.close()
        return result

async def init_log_channels_table():
    async with get_core_db_pool().acquire() as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS log_channels (
            guild_id INTEGER PRIMARY KEY,
            channel_id INTEGER
        )
        """)
        await db.commit()

async def db_set_log_channel(guild_id: int, channel_id: int):
    async with get_core_db_pool().acquire() as db:
        await db.execute(
            "INSERT OR REPLACE INTO log_channels(guild_id, channel_id) VALUES (?, ?)",
            (guild_id, channel_id)
        )
        await db.commit()
    log_channel_cache[guild_id] = (channel_id, time.time() + LOG_CHANNEL_CACHE_TTL)

async def db_get_log_channel(guild_id: int):
    now = time.time()
    _cleanup_ttl_cache(log_channel_cache)
    cached = log_channel_cache.get(guild_id)
    if cached:
        channel_id, expires_at = cached
        if now < expires_at:
            return channel_id
        else:
            log_channel_cache.pop(guild_id, None)

    async with get_core_db_pool().acquire() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT channel_id FROM log_channels WHERE guild_id = ?", (guild_id,))
        row = await cursor.fetchone()
        await cursor.close()
        channel_id = row["channel_id"] if row else None

    log_channel_cache[guild_id] = (channel_id, now + LOG_CHANNEL_CACHE_TTL)
    return channel_id


@bot.command(name="setlog")
@mod_check()
async def setlog(ctx, channel: discord.TextChannel):

    await db_set_log_channel(ctx.guild.id, channel.id)

    channel_id = await db_get_log_channel(ctx.guild.id)

    embed = discord.Embed(
        title="Log Channel Updated Successfully",
        description=f"Log channel set to {channel.mention}",
        color=discord.Color.green()
    )
    embed.set_footer(text=f"Set by {ctx.author}", icon_url=ctx.author.display_avatar.url)

    await ctx.send(embed=embed)

@bot.tree.command(name="setlog", description="Set the logging channel for logs.")
@app_commands.check(slash_mod_check)
@app_commands.describe(channel="Channel to use for logs")
async def setlog_slash(interaction: discord.Interaction, channel: discord.TextChannel):
    await db_set_log_channel(interaction.guild.id, channel.id)
    channel_id = await db_get_log_channel(interaction.guild.id)
    embed = discord.Embed(
        title="Log Channel Updated Successfully",
        description=f"Log channel set to {channel.mention}",
        color=discord.Color.green()
    )
    embed.set_footer(text=f"Set by {interaction.user}", icon_url=interaction.user.display_avatar.url)
    await interaction.response.send_message(embed=embed)

async def get_log_channel(guild: discord.Guild) -> discord.TextChannel | None:
    channel_id = await db_get_log_channel(guild.id)
    if not channel_id:
        return None
    channel = guild.get_channel(channel_id)
    if channel:
        return channel
    try:
        return await guild.fetch_channel(channel_id)
    except (discord.NotFound, discord.Forbidden):
        return None

def human_readable_duration(duration: timedelta) -> str:
    total_seconds = int(duration.total_seconds())
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    parts = []
    if days > 0:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours > 0:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes > 0 and days == 0:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    return ", ".join(parts) if parts else "less than a minute"


async def apply_punishment_for_points(
        ctx,
        guild: discord.Guild,
        member: discord.User,
        points: int,
        reason: str | None = None,
        author: discord.Member | None = None
):
    guild = ctx.guild
    action, duration = await get_punishment(points, str(guild.id))
    reason_text = f"Points: {points} point(s)" + (f" | {reason}" if reason else "")

    log_ch = await get_log_channel(ctx.guild)

    def build_embed(action_text: str, duration_td: timedelta | None = None):
        if duration_td:
            duration_str = human_readable_duration(duration_td)
            first_line = f"{member.mention} has been **{action_text}** for **{duration_str}**."
        else:
            first_line = f"{member.mention} has been **{action_text}.**"

        dm_preposition = "from" if "ban" in action_text.lower() else "in"
        dm_action_text = action_text
        if "ban" in action_text.lower() and duration_td is None:
            dm_action_text = "permanently banned"

        if duration_td:
            dm_first_line = f"You have been **{dm_action_text}** {dm_preposition} **{guild.name}** for **{duration_str}**."
        else:
            dm_first_line = f"You have been **{dm_action_text}** {dm_preposition} **{guild.name}**."

        description = (
            f"User has **{points}** point(s) – {first_line}\n\n"
            f"**Reason:** {reason or 'No reason provided.'}"
        )

        dm_description = (
            f"You have **{points}** point(s) – {dm_first_line}\n\n"
            f"**Reason:** {reason or 'No reason provided.'}"
        )

        embed = discord.Embed(
            description=description,
            color=discord.Color.red() if "ban" in action_text.lower() else discord.Color.orange()
        )
        embed.set_author(
            name=f"{member} ({member.id})",
            icon_url=member.display_avatar.url
        )
        embed.set_footer(
            text=f"by {author}" if author else "by Unknown",
            icon_url=author.display_avatar.url if author else None
        )

        dm_embed = discord.Embed(
            description=dm_description,
            color=discord.Color.red() if "ban" in action_text.lower() else discord.Color.orange()
        )
        dm_embed.set_footer(
            text=f"by {author}" if author else "by Unknown",
            icon_url=author.display_avatar.url if author else None
        )

        return embed, dm_embed

    now_utc = datetime.now(timezone.utc)
    log_embed, dm_embed = build_embed(
        "warned" if action == "warning" else
        "timed out" if action == "timeout" else
        "banned" if action == "ban" else action,
        duration if action in ["timeout", "ban"] else None
    )

    try:
        await member.send(embed=dm_embed)
    except Exception:
        pass

    if action == "warning":
        if log_ch:
            await log_ch.send(embed=log_embed)

    elif action == "timeout":
        if duration is None:
            return "timeout"

        until = now_utc + duration
        await member.timeout(until, reason=reason_text)
        if log_ch:
            await log_ch.send(embed=log_embed)
        return "timeout"

    elif action == "ban":
        if duration is None:
            await guild.ban(member, reason=reason_text, delete_message_days=0)
            await insert_perma_ban_record(str(guild.id), str(member.id))
            if log_ch:
                await log_ch.send(embed=log_embed)
        else:
            await guild.ban(member, reason=reason_text, delete_message_days=0)
            unban_at_ts = dt_to_ts(now_utc + duration)
            await schedule_unban_in_db(str(guild.id), str(member.id), unban_at_ts)
            if log_ch:
                await log_ch.send(embed=log_embed)
    return None




def build_pointvalues_embed(guild_name: str, thresholds: list) -> discord.Embed:
    punishment_labels = [
        "Warning:", "15-minute Timeout:", "30-minute Timeout:", "45-minute Timeout:",
        "60-minute Timeout:", "12-hour Ban:", "12-hour Ban:", "1-day Ban:",
        "3-day Ban:", "7-day Ban:", "7-day Ban:", "Permanent Ban:"
    ]
    desc = "".join([
        f"**{i}. {label}** **{'disabled' if thresholds[i-1] == 0 else str(thresholds[i-1]) + ' Points'}**\n\n"
        for i, label in enumerate(punishment_labels, 1)
    ])

    return discord.Embed(
        title=f"Punishment Point Settings for {guild_name}",
        description=desc,
        color=discord.Color(0x337fd5)
    )

class PointValueModal(discord.ui.Modal, title="Set New Point Value"):
    def __init__(self, index: int, guild_id: str, message: discord.Message = None):
        super().__init__()
        self.index = index
        self.guild_id = guild_id
        self.message = message
        self.value = discord.ui.TextInput(
            label=f"New point value for Action {index}",
            placeholder="Enter a number (0 to disable action)"
        )
        self.add_item(self.value)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            new_val = int(self.value.value)
        except ValueError:
            await interaction.response.send_message("Action Failed. Invalid number.", ephemeral=True)
            return

        if not (0 <= new_val <= 1000):
            await interaction.response.send_message("Action Failed. Value must be between 0 and 1000 (0 = disabled).", ephemeral=True)
            return

        current = await get_guild_pointvalues(self.guild_id)

        if new_val == 0:
            try:
                await update_guild_pointvalue(self.guild_id, self.index, new_val)
                await interaction.response.send_message(f"Updated action {self.index} to **disabled**.", ephemeral=True)
                if self.message:
                    updated_thresholds = await get_guild_pointvalues(self.guild_id)
                    updated_embed = build_pointvalues_embed(interaction.guild.name, updated_thresholds)
                    try:
                        await self.message.edit(embed=updated_embed)
                    except Exception:
                        pass
            except Exception as e:
                await interaction.response.send_message(f"Action Failed: {str(e)}", ephemeral=True)
            return

        if self.index > 1:
            prev_threshold = current[self.index - 2]
            if prev_threshold > 0 and new_val <= prev_threshold:
                await interaction.response.send_message("Action Failed. Must be at least 1 higher than the previous threshold.", ephemeral=True)
                return

        if self.index < 12:
            next_threshold = current[self.index]
            if next_threshold > 0 and new_val >= next_threshold:
                await interaction.response.send_message("Action Failed. Must be less than the next threshold.", ephemeral=True)
                return

        try:
            await update_guild_pointvalue(self.guild_id, self.index, new_val)
            await interaction.response.send_message(f"Updated action {self.index} to **{new_val}** points.", ephemeral=True)
            if self.message:
                updated_thresholds = await get_guild_pointvalues(self.guild_id)
                updated_embed = build_pointvalues_embed(interaction.guild.name, updated_thresholds)
                try:
                    await self.message.edit(embed=updated_embed)
                except Exception:
                    pass
        except Exception as e:
            await interaction.response.send_message(f"Action Failed: {str(e)}", ephemeral=True)

class PointValueButtons(discord.ui.View):
    def __init__(self, guild_id: str):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        for i in range(1, 13):
            self.add_item(self.PointButton(i, guild_id))

    class PointButton(discord.ui.Button):
        def __init__(self, index: int, guild_id: str):
            super().__init__(label=str(index), style=discord.ButtonStyle.blurple, custom_id=f"pointvalue_{guild_id}_{index}")
            self.index = index
            self.guild_id = guild_id

        async def callback(self, interaction: discord.Interaction):
            custom_id_parts = self.custom_id.split("_")
            if len(custom_id_parts) >= 3:
                extracted_guild_id = custom_id_parts[1]
                extracted_index = int(custom_id_parts[2])
                guild_id_to_use = extracted_guild_id
                index_to_use = extracted_index
            else:
                guild_id_to_use = self.guild_id
                index_to_use = self.index
            
            if str(interaction.guild.id) != guild_id_to_use:
                await interaction.response.send_message("This button is for a different server.", ephemeral=True)
                return
            
            original_message = interaction.message if interaction.message else None
            await interaction.response.send_modal(PointValueModal(index_to_use, guild_id_to_use, original_message))

@bot.tree.command(name="pointvalues", description="Show and edit the guild's punishment point thresholds.")
@app_commands.check(slash_mod_check)
async def pointvalues(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    try:
        guild_id = str(interaction.guild.id)

        await init_values_db()

        thresholds = await get_guild_pointvalues(guild_id)

        if not isinstance(thresholds, (list, tuple)) or len(thresholds) < 12:
            await interaction.edit_original_response(content="Could not load thresholds (missing or invalid data).")
            return

        embed = build_pointvalues_embed(interaction.guild.name, thresholds)
        await interaction.edit_original_response(embed=embed, view=PointValueButtons(guild_id))
    except Exception as e:
        import traceback
        traceback.print_exc()
        await interaction.edit_original_response(
            content=f"Action Failed: Something went wrong while loading point values.\n\n{type(e).__name__}: {e}"
        )



@tasks.loop(seconds=60)
async def unban_loop():
    now = now_ts()
    try:
        async with get_core_db_pool().acquire() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM ban_schedule WHERE unban_at <= ?", (now,))
            rows = await cursor.fetchall()
            await cursor.close()
        for row in rows:
            user_id = row["user_id"]
            guild_id = row["guild_id"]
            guild = bot.get_guild(int(guild_id))
            if not guild:
                await unschedule_unban_in_db(guild_id, user_id)
                continue
            try:
                user_obj = await bot.fetch_user(int(user_id))
                await guild.unban(user_obj, reason="Temporary ban expired")
            except Exception as e:
                logger.exception("Failed to unban %s from %s: %s", user_id, guild_id, e)
            await unschedule_unban_in_db(guild_id, user_id)
    except Exception as e:
        logger.exception("Error in unban_loop: %s", e)

@tasks.loop(hours=24)
async def decay_loop():
    now = now_ts()
    TWO_WEEKS = 14 * 24 * 3600

    try:
        async with get_core_db_pool().acquire() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM users WHERE points > 0 AND last_punishment IS NOT NULL"
            )

            batch_size = 1000
            while True:
                rows = await cursor.fetchmany(batch_size)
                if not rows:
                    break

                for row in rows:
                    guild_id = row["guild_id"]
                    user_id = row["user_id"]
                    points = row["points"]
                    last_punishment = row["last_punishment"]
                    last_decay = row["last_decay"]

                    ref = last_decay if last_decay and last_decay > (last_punishment or 0) else last_punishment
                    if not ref:
                        continue

                    periods = (now - ref) // TWO_WEEKS
                    if periods <= 0:
                        continue

                    new_points = max(0, points - periods)
                    new_last_decay = None if new_points == 0 else ref + periods * TWO_WEEKS

                    await db.execute(
                        "UPDATE users SET points = ?, last_decay = ? WHERE guild_id = ? AND user_id = ?",
                        (new_points, new_last_decay, guild_id, user_id)
                    )

                    logger.info("Decayed %s by %s periods -> %s points", user_id, periods, new_points)

            await cursor.close()
            await db.commit()
    except Exception as e:
        logger.exception("Error in decay_loop: %s", e)

@bot.event
async def on_ready():
    try:
        await init_core_db()
        await init_values_db()
        await init_log_channels_table()
        await init_welcome_table()
    except Exception as e:
        print(f"❌ Database init failed: {e}")
        import traceback
        traceback.print_exc()

    cogs_to_load = [
        'cogs.temphide',
        'cogs.starboard',
        'cogs.topgg',
        'cogs.help',
        'cogs.alerts',
        'cogs.scheduled_messages',
        'cogs.sticky_messages',
        'cogs.autoreact',
        'cogs.haiku',
        'cogs.notes',
        'cogs.member_tracker',
        'cogs.maxwithstrapon',
        'cogs.battery_monitor',
        'cogs.slowmode',
        'cogs.nickname'
    ]

    for cog in cogs_to_load:
        try:
            await bot.load_extension(cog)
            print(f"Loaded {cog} Successfully")
        except Exception as e:
            print(f"ERROR: Failed to load {cog}: {e}")
            import traceback
            traceback.print_exc()
    bot.start_time = time.time()
    try:
        bot.tree.add_command(LatencyGroup(bot))
        await bot.tree.sync()
        print(f"Synced slash commands")
    except Exception as e:
        print(f"Error: Failed to sync commands: {e}")
        import traceback
        traceback.print_exc()

    print(f"Bot ready: {bot.user} (id: {bot.user.id})")

    if not unban_loop.is_running():
        unban_loop.start()
    if not decay_loop.is_running():
        decay_loop.start()
    async def _keepalive():
        while True:
            try:
                async with get_core_db_pool().acquire() as db:
                    cursor = await db.execute("SELECT 1")
                    await cursor.fetchone()
                    await cursor.close()
                _ = bot.user and bot.user.id
            except Exception:
                pass
            await asyncio.sleep(60)

    bot.loop.create_task(_keepalive())

    scheduled_cog = bot.get_cog('ScheduledMessages')
    if scheduled_cog and not scheduled_cog.send_scheduled_messages.is_running():
        scheduled_cog.send_scheduled_messages.start()

    await bot.change_presence(
        status=discord.Status.dnd,
        activity=discord.CustomActivity(name="Flirting with your neurons")
    )

@bot.event
async def on_member_unban(guild, user):
    rec = await get_pending_perma_for_user_guild(guild.id, str(user.id))
    if rec:
        await mark_perma_unban_pending(guild.id, str(user.id))
        logger.info("Marked perma ban unban pending for user %s in guild %s", user.id, guild.id)


@bot.command(name="point")
@mod_check()
async def point(ctx, member: discord.Member, amount: int, *, reason: str = None):
    """!!point <member> <amount> [reason...]  -> add points, apply punishment automatically."""
    guild_id = str(ctx.guild.id)
    user_id = str(member.id)

    row = await db_get_user(guild_id, user_id)
    if not row:
        await db_create_or_update_user(guild_id, user_id, 0, None, None)
        row = await db_get_user(guild_id, user_id)

    await ctx.message.delete()

    new_points = row["points"] + amount
    if new_points < 0:
        new_points = 0

    await db_create_or_update_user(guild_id, user_id, new_points, now_ts(), None)

    action, duration = await get_punishment(new_points, guild_id)
    _, _, punishment_text = format_punishment_text(action, duration)

    reason_text = reason or "No reason provided."
    embed = discord.Embed(
        description=(
            f"**{member.mention}** now has **{new_points}** points – {punishment_text}.\n\n"
            f"**Reason:** {reason_text}"
        ),
        color=discord.Color.red()
    )
    embed.set_author(
        name=f"{member.display_name} ({member.id})",
        icon_url=member.display_avatar.url
    )
    embed.set_footer(text=f"by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)

    await apply_punishment_for_points(
        ctx=ctx,
        guild=ctx.guild,
        member=member,
        points=new_points,
        reason=reason,
        author=ctx.author
    )

@bot.tree.command(name="point", description="Add points to a user and apply appropriate punishment automatically.")
@app_commands.check(slash_mod_check)
@app_commands.describe(member="Member to add points to", amount="Amount of points to add", reason="Optional reason")
async def point_slash(interaction: discord.Interaction, member: discord.Member, amount: int, reason: str | None = None):
    guild_id = str(interaction.guild.id)
    user_id = str(member.id)

    row = await db_get_user(guild_id, user_id)
    if not row:
        await db_create_or_update_user(guild_id, user_id, 0, None, None)
        row = await db_get_user(guild_id, user_id)

    new_points = max(0, row["points"] + amount)
    await db_create_or_update_user(guild_id, user_id, new_points, now_ts(), None)

    action, duration = await get_punishment(new_points, guild_id)
    _, _, punishment_text = format_punishment_text(action, duration)
    reason_text = reason or "No reason provided."
    embed = discord.Embed(
        description=(
            f"**{member.mention}** now has **{new_points}** points – {punishment_text}.\n\n"
            f"**Reason:** {reason_text}"
        ),
        color=discord.Color.red()
    )
    embed.set_author(name=f"{member.display_name} ({member.id})", icon_url=member.display_avatar.url)
    embed.set_footer(text=f"by {interaction.user}", icon_url=interaction.user.display_avatar.url)
    await interaction.response.send_message(embed=embed)

    await apply_punishment_for_points(
        ctx=interaction,
        guild=interaction.guild,
        member=member,
        points=new_points,
        reason=reason,
        author=interaction.user
    )


@bot.command(name="pardon")
@mod_check()
async def pardon(ctx, member: discord.Member, amount: int, *, reason: str = None):
    """!!pardon <member> <amount> [reason]  -> remove points"""
    guild_id = str(ctx.guild.id)
    user_id = str(member.id)
    guild = ctx.guild

    row = await db_get_user(guild_id, user_id)

    await ctx.message.delete()

    if not row:
        await ctx.send(f"{member.mention} has no points recorded.")
        return

    old_points = row["points"]
    new_points = max(0, old_points - amount)

    await db_update_points(guild_id, user_id, new_points)

    reason_text = reason if reason else "No reason provided."

    embed = discord.Embed(
        description=(
            f"## Points Updated\n\n"
            f"Points removed: **{amount}** point(s)\n\n"
            f"Old Points: **{old_points}**\n"
            f"New Points: **{new_points}**"
        ),
        color=discord.Color(0x337fd5)
    )
    embed.set_author(name=f"{member.name} ({user_id})", icon_url=member.display_avatar.url)

    await ctx.send(embed=embed)

    log_ch = await get_log_channel(ctx.guild)
    if log_ch:

        log_embed = discord.Embed(
            description=f"## Points Updated\n\nPoints removed: **{amount}** point(s)\n\nOld Points:**{old_points}**\nNew Points:**{new_points}**\n\n**Reason:** {reason_text}",
            color=discord.Color(0x337fd5)
        )
        log_embed.set_author(name=f"{member.name} ({member.id})", icon_url=member.display_avatar.url)
        log_embed.set_footer(text=f"by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await log_ch.send(embed=log_embed)

@bot.tree.command(name="pardon", description="Remove points from a user.")
@app_commands.check(slash_mod_check)
@app_commands.describe(member="Member to remove points from", amount="Amount to remove", reason="Optional reason")
async def pardon_slash(interaction: discord.Interaction, member: discord.Member, amount: int, reason: str | None = None):
    guild_id = str(interaction.guild.id)
    user_id = str(member.id)

    row = await db_get_user(guild_id, user_id)
    if not row:
        await interaction.response.send_message(f"{member.mention} has no points recorded.")
        return

    old_points = row["points"]
    new_points = max(0, old_points - amount)
    await db_update_points(guild_id, user_id, new_points)

    reason_text = reason if reason else "No reason provided."
    embed = discord.Embed(
        description=(f"## Points Updated\n\nPoints removed: **{amount}** point(s)\n\n"
                     f"Old Points: **{old_points}**\nNew Points: **{new_points}**"),
        color=discord.Color(0x337fd5)
    )
    embed.set_author(name=f"{member.name} ({user_id})", icon_url=member.display_avatar.url)
    await interaction.response.send_message(embed=embed)

    log_ch = await get_log_channel(interaction.guild)
    if log_ch:
        log_embed = discord.Embed(
            description=(f"## Points Updated\n\nPoints removed: **{amount}** point(s)\n\n"
                         f"Old Points:**{old_points}**\nNew Points:**{new_points}**\n\n**Reason:** {reason_text}"),
            color=discord.Color(0x337fd5)
        )
        log_embed.set_author(name=f"{member.name} ({member.id})", icon_url=member.display_avatar.url)
        log_embed.set_footer(text=f"by {interaction.user}", icon_url=interaction.user.display_avatar.url)
        await log_ch.send(embed=log_embed)

@bot.command(name="unban")
@mod_check()
async def unban(ctx, user: discord.User, *, reason: str = None):
    """!!unban <user> [reason] -> sets points to 4. If the user is banned, unbans them first."""
    guild_id = str(ctx.guild.id)
    user_id = str(user.id)
    guild = ctx.guild

    await ctx.message.delete()

    try:
        bans = [entry async for entry in ctx.guild.bans()]
    except discord.Forbidden:
        await ctx.send("I don't have permission to view/unban members. Give the bot `Ban Members` permission.")
        return
    except Exception as e:
        logger.exception("Error fetching bans: %s", e)
        await ctx.send("Failed to check server bans. See logs.")
        return

    was_banned = False
    for ban_entry in bans:
        if ban_entry.user.id == user.id:
            try:
                await ctx.guild.unban(ban_entry.user, reason=f"Unban by {ctx.author}")
                await unschedule_unban_in_db(guild_id, user_id)
                async with get_core_db_pool().acquire() as db:
                    await db.execute("DELETE FROM perma_bans WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
                    await db.commit()
                was_banned = True
            except discord.Forbidden:
                await ctx.send("I don't have permission to unban that user. Bot needs `Ban Members` permission.")
                return
            except Exception as e:
                logger.exception("Failed to unban %s: %s", user.id, e)
                await ctx.send(f"Failed to unban {user.mention}: {e}")
                return

    await db_create_or_update_user(guild_id, user_id, 4, None, None)

    embed = discord.Embed(
        description=f"**{user.name}** has been unbanned.",
        color=discord.Color(0x337fd5)
    )
    reason_text = reason if reason else "no reason provided."

    await ctx.send(embed=embed)

    log_ch = await get_log_channel(ctx.guild)
    if log_ch:

        log_description = f"**{user.name}** has been unbanned.\n\n**Reason:** {reason_text}"
        log_embed = discord.Embed(
            description=log_description,
            color=discord.Color(0x337fd5)
        )
        log_embed.set_author(name=f"{user.name} ({user.id})", icon_url=user.display_avatar.url)
        log_embed.set_footer(text=f"by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await log_ch.send(embed=log_embed)

@bot.tree.command(name="unban", description="Unban a user.")
@app_commands.check(slash_mod_check)
@app_commands.describe(user="User to unban", reason="Optional reason")
async def unban_slash(interaction: discord.Interaction, user: discord.User, reason: str | None = None):
    guild_id = str(interaction.guild.id)
    user_id = str(user.id)

    try:
        bans = [entry async for entry in interaction.guild.bans()]
    except discord.Forbidden:
        await interaction.response.send_message("I don't have permission to view/unban members. Give the bot `Ban Members` permission.", ephemeral=True)
        return
    except Exception as e:
        logger.exception("Error fetching bans: %s", e)
        await interaction.response.send_message("Failed to check server bans. See logs.", ephemeral=True)
        return

    for ban_entry in bans:
        if ban_entry.user.id == user.id:
            try:
                await interaction.guild.unban(ban_entry.user, reason=f"Unban by {interaction.user}")
                await unschedule_unban_in_db(guild_id, user_id)
                async with get_core_db_pool().acquire() as db:
                    await db.execute("DELETE FROM perma_bans WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
                    await db.commit()
            except discord.Forbidden:
                await interaction.response.send_message("I don't have permission to unban that user. Bot needs `Ban Members` permission.", ephemeral=True)
                return
            except Exception as e:
                logger.exception("Failed to unban %s: %s", user.id, e)
                await interaction.response.send_message(f"Failed to unban {user.mention}: {e}", ephemeral=True)
                return

    await db_create_or_update_user(guild_id, user_id, 4, None, None)

    embed = discord.Embed(description=f"**{user.name}** has been unbanned.", color=discord.Color(0x337fd5))
    await interaction.response.send_message(embed=embed)

    reason_text = reason if reason else "no reason provided."
    log_ch = await get_log_channel(interaction.guild)
    if log_ch:
        log_embed = discord.Embed(description=f"**{user.name}** has been unbanned.\n\n**Reason:** {reason_text}", color=discord.Color(0x337fd5))
        log_embed.set_author(name=f"{user.name} ({user.id})", icon_url=user.display_avatar.url)
        log_embed.set_footer(text=f"by {interaction.user}", icon_url=interaction.user.display_avatar.url)
        await log_ch.send(embed=log_embed)

@bot.command(name="points")
@mod_check()
async def points_cmd(ctx, user: discord.User):
    guild_id = str(ctx.guild.id)
    user_id = str(user.id)

    row = await db_get_user(guild_id, user_id)
    if not row:
        message_embed = discord.Embed(
            description=f"{user.name} has **0** points. No record found.",
            color=discord.Color(0x337fd5)
        )
        await ctx.send(embed=message_embed)
        return

    last_p_ts = row["last_punishment"]
    last_d_ts = row["last_decay"]

    last_p_display = f"<t:{last_p_ts}:f>" if last_p_ts else "never"
    last_d_display = f"<t:{last_d_ts}:f>" if last_d_ts else "never"

    message_embed = discord.Embed(
        description=(
            f"## Points info\n\n"
            f"Points: **{row['points']}**\n"
            f"Last punishment: **{last_p_display}**\n"
            f"Last decay: **{last_d_display}**"
        ),
        color=discord.Color(0x337fd5)
    )
    message_embed.set_author(name=f"{user.name} ({user_id})", icon_url=user.display_avatar.url)
    await ctx.send(embed=message_embed)

@bot.tree.command(name="points", description="Show a user's points and last actions.")
@app_commands.check(slash_mod_check)
@app_commands.describe(user="User to inspect")
async def points_slash(interaction: discord.Interaction, user: discord.User):
    guild_id = str(interaction.guild.id)
    user_id = str(user.id)

    row = await db_get_user(guild_id, user_id)
    if not row:
        message_embed = discord.Embed(
            description=f"{user.name} has **0** points. No record found.",
            color=discord.Color(0x337fd5)
        )
        await interaction.response.send_message(embed=message_embed)
        return

    last_p_ts = row["last_punishment"]
    last_d_ts = row["last_decay"]
    last_p_display = f"<t:{last_p_ts}:f>" if last_p_ts else "never"
    last_d_display = f"<t:{last_d_ts}:f>" if last_d_ts else "never"

    message_embed = discord.Embed(
        description=(f"## Points info\n\nPoints: **{row['points']}**\n"
                     f"Last punishment: **{last_p_display}**\nLast decay: **{last_d_display}**"),
        color=discord.Color(0x337fd5)
    )
    message_embed.set_author(name=f"{user.name} ({user_id})", icon_url=user.display_avatar.url)
    await interaction.response.send_message(embed=message_embed)



@bot.tree.command(name="avatar", description="Get a user's avatar.")
@app_commands.describe(user="The user whose avatar you want to see.")
async def avatar(interaction: discord.Interaction, user: discord.User):
    embed = discord.Embed(
        title=f"{user.name}",
        description="### User Avatar",
        color=discord.Color(0x337fd5)
    )
    embed.set_image(url=user.avatar.url if user.avatar else user.default_avatar.url)
    await interaction.response.send_message(embed=embed)


bot.tree.command(name="purge", description="Delete recent messages.")
@app_commands.check(slash_mod_check)
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.describe(number="Number of messages to delete (max 100)")
async def purge_slash(interaction: discord.Interaction, number: int):
    number = max(1, min(number, 100))

    await interaction.response.defer(ephemeral=True)

    try:

        messages = [msg async for msg in interaction.channel.history(limit=number)]

        if not messages:
            return await interaction.followup.send("No messages found to delete.", ephemeral=True)

        await interaction.channel.delete_messages(messages)
        deleted_count = len(messages)

    except discord.Forbidden:
        return await interaction.followup.send("I don't have permission to delete messages here.", ephemeral=True)
    except discord.HTTPException as e:
        if e.code == 50034:
            return await interaction.followup.send(
                "Cannot delete messages older than 14 days using bulk delete.",
                ephemeral=True
            )
        return await interaction.followup.send(f"An error occurred: {e}", ephemeral=True)
    log_ch = await get_log_channel(interaction.guild)
    if log_ch:
        log_embed = discord.Embed(
            description=f"**{deleted_count}** message(s) purged in {interaction.channel.mention}.",
            color=discord.Color.red()
        )
        log_embed.set_footer(text=f"By {interaction.user}", icon_url=interaction.user.display_avatar.url)
        await log_ch.send(embed=log_embed)

    await interaction.followup.send(f"Successfully purged **{deleted_count}** messages.", ephemeral=True)


@bot.tree.command(name="fuckoff", description="Is the bot annoying you? Tell it to fuck off and shut itself down using this.")
async def fuckoff_slash(interaction: discord.Interaction):
    """Gracefully stop the bot (developer only)."""
    if interaction.user.id != 758576879715483719:
        await interaction.response.send_message(
            "What do you think you're doing? Who do you think you are?? Why do you want to kill me???\nYou're not my dev. Don't tell me what to do. Go away.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        "K 👍\nFucking off now.",
        ephemeral=True
    )

    async def _shutdown():
        for loop in (unban_loop, decay_loop):
            if loop.is_running():
                loop.stop()

        scheduled = bot.get_cog('ScheduledMessages')
        if scheduled and scheduled.send_scheduled_messages.is_running():
            scheduled.send_scheduled_messages.stop()

        await close_db_connections()
        await bot.close()

    asyncio.create_task(_shutdown())

last_ban_time = {}

@bot.command(name="ban")
async def ban(ctx, member: discord.Member = None, duration: str = None, *, reason: str = None):
    cooldown_seconds = 60
    now = time.time()
    guild_id = ctx.guild.id

    last_time = last_ban_time.get(guild_id, 0)

    if now - last_time < cooldown_seconds:
        remaining = int(cooldown_seconds - (now - last_time))

        await ctx.message.delete()

        cooldown_embed = discord.Embed(
            title="Slow down!",
            description=f"This command is on cooldown. Try again in **{remaining}** seconds.",
            color=discord.Color.red()
        )

        await ctx.send(embed=cooldown_embed, delete_after=5)
        return

    cleanup_old_cooldowns(last_ban_time, 120)

    last_ban_time[guild_id] = now

    if not member:
        ban_embed = discord.Embed(
            title="!!ban Command",
            description=(
                "**Function:** Used to scare members with the threat of a ban.\n"
                "**Usage:** `!!ban <user> <duration> <reason>`\n"
                "**Cooldown:** 60 Seconds (Per Server)\n"
                "**Example:** `!!ban <@155149108183695360>`\n"
                "-# Disclaimer: This command is purely cosmetic and does NOT actually ban anyone. It's reason for existing is to mock the poor little moderation bots that don't use a point-based system like me :)"
            ),
            color=discord.Color(0x337fd5)
        )
        await ctx.send(embed=ban_embed)
        return

    await ctx.message.delete()

    embed = discord.Embed(
        description=f"**{member.mention}** has been **banned**"
                    + (f" for {duration}" if duration else "")
                    + (f"\n\n**Reason:** {reason}\n\n" if reason else "."),
        color=discord.Color.red()
    )
    embed.set_author(
        name=f"{member.display_name} ({member.id})",
        icon_url=member.display_avatar.url
    )
    embed.set_footer(text=f"by {ctx.author}", icon_url=ctx.author.display_avatar.url)

    await ctx.send(embed=embed)

@bot.tree.command(name="ban", description="Fake-ban someone (cosmetic).")
@app_commands.describe(member="Who to fake-ban", duration="How long (text)", reason="Optional reason")
async def ban_slash(interaction: discord.Interaction, member: discord.Member | None = None, duration: str | None = None, reason: str | None = None):
    try:
        cooldown_seconds = 60
        now_sec = time.time()
        gid = interaction.guild.id
        last_time = last_ban_time.get(gid, 0)
        if now_sec - last_time < cooldown_seconds:
            remaining = int(cooldown_seconds - (now_sec - last_time))
            cooldown_embed = discord.Embed(
                title="Slow down!",
                description=f"This command is on cooldown. Try again in **{remaining}** seconds.",
                color=discord.Color.red()
            )

            await interaction.response.send_message(
                embed=cooldown_embed
                ,
                ephemeral=True
            )
            return

        cleanup_old_cooldowns(last_ban_time, 120)

        last_ban_time[gid] = now_sec

        if not member:
            ban_embed = discord.Embed(
                title="/ban Command",
                description=("Function: Used to scare members with the threat of a ban.\n"
                             "Usage: `/ban <user> <duration> <reason>`\n"
                             "Cooldown: 60 Seconds (Per Server)\n"
                             "Example: `/ban <@155149108183695360>`\n"
                             "-# Disclaimer: Cosmetic only; does not actually ban anyone."),
                color=discord.Color(0x337fd5)
            )
            await interaction.response.send_message(embed=ban_embed, ephemeral=True)
            return

        embed = discord.Embed(
            description=f"**{member.mention}** has been **banned**"
                        + (f" for {duration}" if duration else "")
                        + (f"\n\n**Reason:** {reason}\n\n" if reason else "."),
            color=discord.Color.red()
        )
        embed.set_author(name=f"{member.display_name} ({member.id})", icon_url=member.display_avatar.url)
        embed.set_footer(text=f"by {interaction.user}", icon_url=interaction.user.display_avatar.url)
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        logger.exception("Error in /ban command: %s", e)
        if interaction.response.is_done():
            try:
                await interaction.followup.send(
                    "An unexpected error occurred while running this command.", ephemeral=True
                )
            except Exception:
                pass
        else:
            try:
                await interaction.response.send_message(
                    "An unexpected error occurred while running this command.", ephemeral=True
                )
            except Exception:
                pass

@ban_slash.error
async def ban_slash_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    from discord.app_commands import CheckFailure

    if isinstance(error, CheckFailure):
        if interaction.response.is_done():
            await interaction.followup.send(
                "You can't use this command here.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "You can't use this command here.", ephemeral=True
            )
        return

    logger.exception("Unhandled error in /ban command: %s", error)
    if not interaction.response.is_done():
        try:
            await interaction.response.send_message(
                "An error occurred while running this command.", ephemeral=True
            )
        except Exception:
            pass

from discord.ext import commands

@bot.command(name="echo")
@mod_check()
async def echo(ctx, channel: discord.TextChannel, *, message: str):
    try:
        await ctx.message.delete()
        await channel.send(message)
    except Exception as e:
        await ctx.send(f"Error: Could not send message: {e}")

@bot.tree.command(name="echo", description="Make the bot say a message in a channel.")
@app_commands.check(slash_mod_check)
@app_commands.describe(channel="Where to send the message", message="What to say")
async def echo_slash(interaction: discord.Interaction, channel: discord.TextChannel, message: str):
    try:
        await channel.send(message)
        await interaction.response.send_message("Message echoed successfully.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Error: Could not send message: {e}", ephemeral=True)

@echo.error
async def echo_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        message_embed = discord.Embed(
            description="You don't have the permission to use this command.",
            color=discord.Color(0x337fd5)
        )
        await ctx.send(embed=message_embed)


last_say_time = {}

@bot.command(name="say")
async def say(ctx, channel: discord.TextChannel, *, message: str):
    cooldown_seconds = 60
    now = time.time()
    guild_id = ctx.guild.id

    last_time = last_say_time.get(guild_id, 0)

    if now - last_time < cooldown_seconds:
        remaining = int(cooldown_seconds - (now - last_time))
        await ctx.message.delete()
        cooldown_embed = discord.Embed(
            title="Slow down!",
            description=f"This command is on cooldown. Try again in **{remaining}** seconds.",
            color=discord.Color.red()
        )

        await ctx.send(embed=cooldown_embed, delete_after=5)
        return

    cleanup_old_cooldowns(last_say_time, 120)
    last_say_time[guild_id] = now

    try:
        await ctx.message.delete()
        text = f"{ctx.author.mention} has desperately begged on their knees and asked me to say: {message}"
        await channel.send(text)
    except Exception as e:
        await ctx.send(f"❌ Could not send message: {e}")


@bot.tree.command(name="say", description="Ask the bot to say something")
@app_commands.describe(channel="Where to send it", message="What to say")
async def say_slash(interaction: discord.Interaction, channel: discord.TextChannel, message: str):
    cooldown_seconds = 60
    now_sec = time.time()
    gid = interaction.guild.id
    last_time = last_say_time.get(gid, 0)

    if now_sec - last_time < cooldown_seconds:
        remaining = int(cooldown_seconds - (now_sec - last_time))
        cooldown_embed = discord.Embed(
            title="Slow down!",
            description=f"This command is on cooldown. Try again in **{remaining}** seconds.",
            color=discord.Color.red()
        )

        await interaction.response.send_message(
            embed=cooldown_embed,
            ephemeral=True
        )
        return

    cleanup_old_cooldowns(last_say_time, 120)
    last_say_time[gid] = now_sec

    try:
        text = f"{interaction.user.mention} has desperately begged on their knees and asked me to say: {message}"
        await channel.send(text)
        await interaction.response.send_message("Sent.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Error: Could not send message: {e}", ephemeral=True)



@bot.tree.command(name="ping", description="Show bot latency.")
async def ping_slash(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!")
    latency_ms = round(bot.latency * 1000)
    await interaction.edit_original_response(content=f"Pong! `{latency_ms}ms`")


async def init_welcome_table():
    async with get_core_db_pool().acquire() as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS welcome_channels (
            guild_id INTEGER PRIMARY KEY,
            channel_id INTEGER
        )
        """)
        await db.commit()

async def db_set_welcome_channel(guild_id: int, channel_id: int):
    async with get_core_db_pool().acquire() as db:
        await db.execute(
            "INSERT OR REPLACE INTO welcome_channels(guild_id, channel_id) VALUES (?, ?)",
            (guild_id, channel_id)
        )
        await db.commit()
    welcome_channel_cache[guild_id] = (channel_id, time.time() + WELCOME_CHANNEL_CACHE_TTL)

async def db_get_welcome_channel(guild_id: int):
    now = time.time()
    _cleanup_ttl_cache(welcome_channel_cache)
    cached = welcome_channel_cache.get(guild_id)
    if cached:
        channel_id, expires_at = cached
        if now < expires_at:
            return channel_id
        else:
            welcome_channel_cache.pop(guild_id, None)

    async with get_core_db_pool().acquire() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT channel_id FROM welcome_channels WHERE guild_id = ?", (guild_id,))
        row = await cursor.fetchone()
        await cursor.close()
        channel_id = row["channel_id"] if row else None

    welcome_channel_cache[guild_id] = (channel_id, now + WELCOME_CHANNEL_CACHE_TTL)
    return channel_id

async def get_welcome_channel(guild: discord.Guild):
    channel_id = await db_get_welcome_channel(guild.id)
    if not channel_id:
        return None
    channel = guild.get_channel(channel_id)
    if channel:
        return channel
    try:
        return await guild.fetch_channel(channel_id)
    except (discord.NotFound, discord.Forbidden):
        return None

@bot.command(name="welcome")
@mod_check()
async def welcome(ctx, channel: discord.TextChannel = None):
    """!!welcome #channel -> enable welcome messages in that channel
       !!welcome -> disable welcome messages"""
    if channel:
        await db_set_welcome_channel(ctx.guild.id, channel.id)
        embed = discord.Embed(
            description=f"Welcome messages have been **enabled**, sending them to {channel.mention}.",
            color=discord.Color(0x337fd5)
        )
    else:
        async with get_core_db_pool().acquire() as db:
            await db.execute("DELETE FROM welcome_channels WHERE guild_id = ?", (ctx.guild.id,))
            await db.commit()
        welcome_channel_cache.pop(ctx.guild.id, None)
        embed = discord.Embed(
            description="Welcome messages have been **disabled**.",
            color=discord.Color(0x337fd5)
        )

    await ctx.send(embed=embed)

@bot.tree.command(name="welcome", description="Enable/disable welcome messages or set the target channel.")
@app_commands.check(slash_mod_check)
@app_commands.describe(channel="Channel for welcome messages (Leave blank to disable)")
async def welcome_slash(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    if channel:
        await db_set_welcome_channel(interaction.guild.id, channel.id)
        embed = discord.Embed(
            description=f"Welcome messages have been **enabled**, sending them to {channel.mention}.",
            color=discord.Color(0x337fd5)
        )
    else:
        async with get_core_db_pool().acquire() as db:
            await db.execute("DELETE FROM welcome_channels WHERE guild_id = ?", (interaction.guild.id,))
            await db.commit()
        welcome_channel_cache.pop(interaction.guild.id, None)
        embed = discord.Embed(
            description="Welcome messages have been **disabled**.",
            color=discord.Color(0x337fd5)
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.event
async def on_member_join(member):
    rec = await get_pending_perma_for_user_guild(member.guild.id, str(member.id))
    if rec and rec["unban_pending"] == 1 and rec["applied"] == 0:
        existing = await db_get_user(str(member.guild.id), str(member.id))
        new_points = max(existing["points"], 4) if existing else 4
        await db_create_or_update_user(str(member.guild.id), str(member.id), new_points, None, None)
        await mark_perma_applied(member.guild.id, str(member.id))

    channel = await get_welcome_channel(member.guild)
    if channel:
        await channel.send(f"Welcome to **{member.guild.name}**, {member.mention}!")


@bot.event
async def on_guild_join(guild):
    embed = discord.Embed(
        description=(
            "### Thank you for inviting me!\n\n"
            "I'm a point-based moderation and utility bot. The moderation system is inspired by the core functionality of the moderation bot in the **teenserv** Discord server ([**__discord.gg/teenserv__**](https://www.discord.gg/teenserv)).\n\n"
            "**Use `/help` to get started! ^_^**\n\n"
            "-# [**__Vote__**](https://top.gg/bot/1411266382380924938/vote) • [**__Support Server__**](https://discord.gg/VWDcymz648)"
        ),
        color=discord.Color.purple()
    )

    embed.set_author(
        name="Dopamine — Advanced point-based Moderation Bot",
        icon_url=bot.user.display_avatar.url
    )

    target_channel = None
    keywords = ["general", "chat", "lounge"]
    for channel in guild.text_channels:
        if any(word in channel.name.lower() for word in keywords):
            if channel.permissions_for(guild.me).send_messages:
                target_channel = channel
                break

    if not target_channel:
        if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
            target_channel = guild.system_channel

    if not target_channel:
        for channel in guild.text_channels:
            if channel.permissions_for(guild.me).send_messages:
                target_channel = channel
                break

    if target_channel:
        await target_channel.send(embed=embed)

rate_limits = {}


def format_uptime(seconds):
    """Format uptime into weeks, days, hours, minutes, seconds"""
    weeks = seconds // (7 * 24 * 60 * 60)
    seconds %= (7 * 24 * 60 * 60)
    days = seconds // (24 * 60 * 60)
    seconds %= (24 * 60 * 60)
    hours = seconds // (60 * 60)
    seconds %= (60 * 60)
    minutes = seconds // 60
    seconds %= 60

    parts = []
    if weeks > 0:
        parts.append(f"{int(weeks)}w")
    if days > 0:
        parts.append(f"{int(days)}d")
    if hours > 0:
        parts.append(f"{int(hours)}h")
    if minutes > 0:
        parts.append(f"{int(minutes)}m")
    if seconds > 0 or not parts:
        parts.append(f"{int(seconds)}s")

    return " ".join(parts)


class LatencyGroup(app_commands.Group):
    def __init__(self, bot):
        super().__init__(name="latency", description="Latency information commands")
        self.bot = bot

    @app_commands.command(name="info", description="Get detailed latency and bot information")
    async def info(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        current_time = time.time()

        cleanup_old_cooldowns(rate_limits, 120)

        if user_id in rate_limits:
            time_since_last = current_time - rate_limits[user_id]
            if time_since_last < 60:
                remaining = int(60 - time_since_last)
                cooldown_embed = discord.Embed(
                    title="Slow down!",
                    description=f"This command is on cooldown. Try again in **{remaining}** seconds.",
                    color=discord.Color.red()
                )

                await interaction.response.send_message(embed=cooldown_embed, ephemeral=True)
                return

        rate_limits[user_id] = current_time

        initial_message = (
            "Pinging...\n"
            "Digging around for your IP address...\n"
            "Getting your location...\n"
            "Calculating distance to your home...\n"
            "Sending you some icecream...\n"
            "Calculating the dev's love for his sexy girlfriend...\n"
            "Done! Icecream sent, love exceeds 64-bit integer limit."
        )

        discord_latency = round(self.bot.latency * 1000, 2)
        start = time.perf_counter()
        await interaction.response.send_message(initial_message)
        end = time.perf_counter()
        connection_latency = round((end - start) * 1000, 2)

        total_latency = round(discord_latency + connection_latency, 2)

        if hasattr(self.bot, 'start_time'):
            uptime_seconds = int(time.time() - self.bot.start_time)
        else:
            uptime_seconds = 0
        uptime_formatted = format_uptime(uptime_seconds)

        try:
            process = psutil.Process(os.getpid())
            memory_bytes = process.memory_info().rss
            memory_mb = memory_bytes / (1024 * 1024)

            if memory_mb >= 1024:
                memory_gb = int(memory_mb // 1024)
                memory_remaining_mb = round(memory_mb % 1024, 2)
                memory_usage = f"{memory_gb}GB {memory_remaining_mb}MB"
            else:
                memory_usage = f"{round(memory_mb, 2)}MB"
        except Exception:
            memory_usage = "Unable to calculate"

        try:
            battery = psutil.sensors_battery()
            if battery:
                percent = battery.percent
                charging = battery.power_plugged
                battery_status = f"Host Device Battery Status: `{percent}% ({'Charging' if charging else 'Discharging'})`"
            else:
                battery_status = "Host Device Battery Status: `Not available`"
        except Exception:
            battery_status = "Host Device Battery Status: `Unable to determine`"

        embed = discord.Embed(
            title="Latency Info",
            description=(
                f"> Bot Version: `{bot_version}`\n\n"
                f"> Discord Latency: `{discord_latency}ms`\n"
                f"> Connection Latency: `{connection_latency}ms`\n"
                f"> Total Latency: `{total_latency}ms`\n\n"
                f"> Average Latency: `Measuring...`\n\n"
                f"> Uptime: `{uptime_formatted}`\n"
                f"> Memory Usage: `{memory_usage}`\n"
                f"> {battery_status}"
            ),
            color=discord.Color(0x337fd5)
        )

        message = await interaction.original_response()
        await message.edit(content=None, embed=embed)

        asyncio.create_task(
            self.measure_average_latency(message, embed, discord_latency, connection_latency, total_latency,
                                         uptime_formatted, memory_usage, battery_status))

    async def measure_average_latency(self, message, embed, discord_latency, connection_latency, total_latency,
                                      uptime_formatted, memory_usage, battery_status):
        """Measure average TOTAL latency over 30 seconds by sampling and measuring API calls"""
        total_latencies = []
        start_time = time.time()

        while time.time() - start_time < 30:
            try:
                ws_latency = self.bot.latency * 1000

                api_before = time.perf_counter()
                try:
                    channel = await self.bot.fetch_channel(message.channel.id)
                except Exception:
                    channel = None
                api_after = time.perf_counter()
                api_latency = (api_after - api_before) * 1000

                current_total_latency = ws_latency + api_latency
                total_latencies.append(current_total_latency)

            except Exception as e:
                ws_latency = self.bot.latency * 1000
                estimated_connection = ws_latency * 0.1
                current_total_latency = ws_latency + estimated_connection
                total_latencies.append(current_total_latency)

            await asyncio.sleep(2)

        if total_latencies:
            average_total_latency = round(sum(total_latencies) / len(total_latencies), 2)
        else:
            average_total_latency = total_latency

        embed.description = (
            f"> Bot Version: `{bot_version}`\n\n"
            f"> Discord Latency: `{discord_latency}ms`\n"
            f"> Connection Latency: `{connection_latency}ms`\n"
            f"> Total Latency: `{total_latency}ms`\n\n"
            f"> Average Latency: `{average_total_latency}ms`\n\n"
            f"> Uptime: `{uptime_formatted}`\n"
            f"> Memory Usage: `{memory_usage}`\n"
            f"> {battery_status}"
        )

        try:
            await message.edit(embed=embed)
        except Exception:
            pass


@bot.command(name="ping")
async def ping(ctx):
    """Get detailed latency and bot information (prefix version of /latency info)"""
    user_id = ctx.author.id
    current_time = time.time()

    cleanup_old_cooldowns(rate_limits, 120)

    if user_id in rate_limits:
        time_since_last = current_time - rate_limits[user_id]
        if time_since_last < 60:
            remaining = int(60 - time_since_last)
            cooldown_embed = discord.Embed(
                title="Slow down!",
                description=f"This command is on cooldown. Try again in **{remaining}** seconds.",
                color=discord.Color.red()
            )

            try:
                await ctx.message.delete()
            except Exception:
                pass

            error_message = await ctx.send(embed=cooldown_embed)
            await asyncio.sleep(5)
            try:
                await error_message.delete()
            except Exception:
                pass
            return

    rate_limits[user_id] = current_time

    initial_message = (
        "Pinging...\n"
        "Digging around for your IP address...\n"
        "Getting your location...\n"
        "Calculating distance to your home...\n"
        "Sending you some icecream...\n"
        "Calculating the dev's love for his sexy girlfriend...\n"
        "Done! Icecream sent, love exceeds 64-bit integer limit."
    )

    discord_latency = round(ctx.bot.latency * 1000, 2)
    start = time.perf_counter()
    message = await ctx.send(initial_message)
    end = time.perf_counter()
    connection_latency = round((end - start) * 1000, 2)

    total_latency = round(discord_latency + connection_latency, 2)

    if hasattr(ctx.bot, 'start_time'):
        uptime_seconds = int(time.time() - ctx.bot.start_time)
    else:
        uptime_seconds = 0
    uptime_formatted = format_uptime(uptime_seconds)

    try:
        process = psutil.Process(os.getpid())
        memory_bytes = process.memory_info().rss
        memory_mb = memory_bytes / (1024 * 1024)

        if memory_mb >= 1024:
            memory_gb = int(memory_mb // 1024)
            memory_remaining_mb = round(memory_mb % 1024, 2)
            memory_usage = f"{memory_gb}GB {memory_remaining_mb}MB"
        else:
            memory_usage = f"{round(memory_mb, 2)}MB"
    except Exception:
        memory_usage = "Unable to calculate"

    try:
        battery = psutil.sensors_battery()
        if battery:
            percent = battery.percent
            charging = battery.power_plugged
            battery_status = f"Host Device Battery Status: `{percent}% ({'Charging' if charging else 'Discharging'})`"
        else:
            battery_status = "Host Device Battery Status: `Not available`"
    except Exception:
        battery_status = "Host Device Battery Status: `Unable to determine`"

    embed = discord.Embed(
        title="Pong!",
        description=(
            f"> Bot Version: `{bot_version}`\n\n"
            f"> Discord Latency: `{discord_latency}ms`\n"
            f"> Connection Latency: `{connection_latency}ms`\n"
            f"> Total Latency: `{total_latency}ms`\n\n"
            f"> Average Latency: `Measuring...`\n\n"
            f"> Uptime: `{uptime_formatted}`\n"
            f"> Memory Usage: `{memory_usage}`\n"
            f"> {battery_status}"
        ),
        color=discord.Color(0x337fd5)
    )

    await message.edit(content=None, embed=embed)

    asyncio.create_task(
        measure_average_latency_prefix(message, embed, discord_latency, connection_latency, total_latency,
                                      uptime_formatted, memory_usage, battery_status, ctx.bot))


async def measure_average_latency_prefix(message, embed, discord_latency, connection_latency, total_latency,
                                         uptime_formatted, memory_usage, battery_status, bot):
    """Measure average TOTAL latency over 30 seconds by sampling and measuring API calls"""
    total_latencies = []
    start_time = time.time()

    while time.time() - start_time < 30:
        try:
            ws_latency = bot.latency * 1000

            api_before = time.perf_counter()
            try:
                channel = await bot.fetch_channel(message.channel.id)
            except Exception:
                channel = None
            api_after = time.perf_counter()
            api_latency = (api_after - api_before) * 1000

            current_total_latency = ws_latency + api_latency
            total_latencies.append(current_total_latency)

        except Exception as e:
            ws_latency = bot.latency * 1000
            estimated_connection = ws_latency * 0.1
            current_total_latency = ws_latency + estimated_connection
            total_latencies.append(current_total_latency)

        await asyncio.sleep(2)

    if total_latencies:
        average_total_latency = round(sum(total_latencies) / len(total_latencies), 2)
    else:
        average_total_latency = total_latency

    embed.description = (
        f"> Bot Version: `{bot_version}`\n\n"
            f"> Discord Latency: `{discord_latency}ms`\n"
            f"> Connection Latency: `{connection_latency}ms`\n"
            f"> Total Latency: `{total_latency}ms`\n\n"
            f"> Average Latency: `{average_total_latency}ms`\n\n"
            f"> Uptime: `{uptime_formatted}`\n"
            f"> Memory Usage: `{memory_usage}`\n"
            f"> {battery_status}"
    )

    try:
        await message.edit(embed=embed)
    except Exception:
        pass


user_rate_limits = {}

@bot.tree.command(name="servercount", description="Get the number of servers the bot is in.")
async def servercount(interaction: discord.Interaction):
    """Get the number of servers the bot is in."""
    user_id = interaction.user.id
    cooldown_seconds = 60
    now = time.time()

    cleanup_old_cooldowns(user_rate_limits, 120)



    if user_id in user_rate_limits:
        time_since_last = now - user_rate_limits.get(user_id, 0)
        if time_since_last < cooldown_seconds:
            remaining = int(cooldown_seconds - time_since_last)
            cooldown_embed = discord.Embed(
                title="Slow down!",
                description=f"This command is on cooldown. Try again in **{remaining}** seconds.",
                color=discord.Color.red()
            )

            await interaction.response.send_message(
                embed=cooldown_embed,
                ephemeral=True
            )
            return

    user_rate_limits[user_id] = now
    server_count = len(bot.guilds)
    await interaction.response.send_message(f"I am currently in **{server_count}** servers.")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        permission_error = discord.Embed(
            description="You don't have the permission to use this command."
        )
        await ctx.send(embed=permission_error)

    elif ctx.command and ctx.command.name == "point" and isinstance(error, (
        commands.BadArgument, commands.MissingRequiredArgument
    )):
        usage_embed = discord.Embed(
            title="!!point Command",
            description=(
                "Add points to a user.\n\n"
                "The correct usage of `!!point` command is:\n"
                "`!!point <user> <amount> <reason>`\n\n"
                "-# Make sure the user you're trying to point is in the server."
            ),
            color=discord.Color(0x337fd5)
        )
        await ctx.send(embed=usage_embed)

    elif ctx.command and ctx.command.name == "pardon" and isinstance(error, (
        commands.BadArgument, commands.MissingRequiredArgument
    )):
        usage_embed = discord.Embed(
            title="!!pardon Command",
            description=(
                "Removes points from a user.\n\n"
                "The correct usage of `!!pardon` command is:\n"
                "`!!pardon <user> <amount> <reason>`\n\n"
                "-# Make sure the user whose points you're trying to subtract is in the server."
            ),
            color=discord.Color(0x337fd5)
        )
        await ctx.send(embed=usage_embed)

    elif ctx.command and ctx.command.name == "unban" and isinstance(error, (
        commands.BadArgument, commands.MissingRequiredArgument
    )):
        usage_embed = discord.Embed(
            title="!!unban Command",
            description=(
                "Unban a user.\n\n"
                "The correct usage of `!!unban` command is:\n"
                "`!!unban <user>`\n\n"
                "-# Make sure the user you're trying to unban was actually banned."
            ),
            color=discord.Color(0x337fd5)
        )
        await ctx.send(embed=usage_embed)

    elif ctx.command and ctx.command.name == "purge" and isinstance(error, (
        commands.BadArgument, commands.MissingRequiredArgument
    )):
        usage_embed = discord.Embed(
            title="!!purge Command",
            description=(
                "Purge a specific amount of messages in the current channel.\n\n"
                "The correct usage of `!!purge` command is:\n"
                "`!!purge <amount>`\n\n"
            ),
            color=discord.Color(0x337fd5)
        )
        await ctx.send(embed=usage_embed)

    elif ctx.command and ctx.command.name == "setlog" and isinstance(error, (
        commands.BadArgument, commands.MissingRequiredArgument
    )):
        usage_embed = discord.Embed(
            title="!!setlog Command",
            description=(
                "Set the log channel for the bot.\n\n"
                "The correct usage of `!!setlog` command is:\n"
                 "`!!setlog <channel name>`\n\n"
             ),
            color=discord.Color(0x337fd5)
        )
        await ctx.send(embed=usage_embed)

    elif ctx.command and ctx.command.name == "note" and isinstance(error, (
        commands.BadArgument, commands.MissingRequiredArgument
    )):
        usage_embed = discord.Embed(
            title="!!note Command",
            description=(
                "Note something, and retrieve it later using `/get_note`.\n\n"
                "The correct usage of `!!note` command is:\n"
                "`!!note <name> <content>`\n\n"
            ),
            color=discord.Color(0x337fd5)
        )
        await ctx.send(embed=usage_embed)


    elif isinstance(error, commands.CommandNotFound):
        pass

    else:
        logger.exception("Unhandled command error: %s", error)
        finalerror_embed = discord.Embed(
            description=(
            f"An error occurred: {error}"),
            color=discord.Color.red()
        )


if __name__ == "__main__":
    async def main_async():
        try:
            await init_core_db()
            await init_values_db()
            await init_log_channels_table()
            await init_welcome_table()
        except Exception as e:
            print(f"Error: Initialization failed: {e}")
            return

        try:
            async with bot:
                await bot.start(TOKEN)
        finally:
            await close_db_connections()

    asyncio.run(main_async())
