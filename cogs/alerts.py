import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from config import ALERTDB_PATH


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
        self._pool: Optional[asyncio.Queue[aiosqlite.Connection]] = None
        self._current_alert: Optional[CurrentAlert] = None

        self._position_cache: Dict[Tuple[int, int], Tuple[int, float]] = {}
        self._reminder_cooldowns: Dict[int, float] = {}

        self.POSITION_CACHE_TTL = 300.0
        self.REMINDER_COOLDOWN_TTL = 300.0

    async def cog_load(self):
        await self._init_pool()
        await self._init_db()
        await self._load_current_alert()

    async def cog_unload(self):
        if self._pool is not None:
            while not self._pool.empty():
                conn = await self._pool.get()
                try:
                    await conn.close()
                except Exception:
                    pass
            self._pool = None

    async def _create_pooled_connection(self) -> aiosqlite.Connection:
    
        max_retries = 5
        for attempt in range(max_retries):
            try:
                conn = await aiosqlite.connect(
                    ALERTDB_PATH,
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

    async def _init_pool(self, pool_size: int = 3):
        if self._pool is not None:
            return
        self._pool = asyncio.Queue(maxsize=pool_size)
        for _ in range(pool_size):
            conn = await self._create_pooled_connection()
            await self._pool.put(conn)

    @asynccontextmanager
    async def _acquire_db(self) -> aiosqlite.Connection:
        assert self._pool is not None, "Alerts DB pool not initialized"
        conn = await self._pool.get()
        try:
            yield conn
        finally:
            await self._pool.put(conn)

    async def _init_db(self) -> None:
       
        async with self._acquire_db() as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    read_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS alert_reads (
                    alert_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    position INTEGER NOT NULL,
                    PRIMARY KEY (alert_id, user_id)
                )
                """
            )
            await db.commit()

    async def _load_current_alert(self) -> None:
       
        async with self._acquire_db() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM alerts ORDER BY id DESC LIMIT 1"
            )
            row = await cursor.fetchone()
            await cursor.close()

        if row:
            self._current_alert = CurrentAlert(
                id=row["id"],
                title=row["title"],
                description=row["description"],
                created_at=row["created_at"],
                read_count=row["read_count"],
            )
        else:
            self._current_alert = None

        self._position_cache.clear()
        self._reminder_cooldowns.clear()

    @staticmethod
    def _cleanup_ttl_cache(cache: Dict, now: Optional[float] = None) -> None:
        if not cache:
            return
        if now is None:
            now = time.time()
        to_delete = [k for k, (_, expiry) in cache.items()] if cache and isinstance(
            next(iter(cache.values())), tuple
        ) else [
            k for k, expiry in cache.items() if expiry <= now
        ]
        for key in to_delete:
            cache.pop(key, None)

    class PushAlertModal(discord.ui.Modal, title="Push New Alert"):
        def __init__(self, parent_cog: "Alerts"):
            super().__init__()
            self.parent_cog = parent_cog

            self.alert_title = discord.ui.TextInput(
                label="Alert Title",
                max_length=256,
            )
            self.description = discord.ui.TextInput(
                label="Description",
                style=discord.TextStyle.paragraph,
                max_length=4000,
            )

            self.add_item(self.alert_title)
            self.add_item(self.description)

        async def on_submit(self, interaction: discord.Interaction) -> None:
            title = str(self.alert_title.value).strip()
            description = str(self.description.value).strip()

            if not title or not description:
                embed = discord.Embed(
                    title="Invalid Alert",
                    description="Title and description cannot be empty.",
                    color=discord.Color.red(),
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            now_ts = int(datetime.now(timezone.utc).timestamp())

            async with self.parent_cog._acquire_db() as db:
                try:
                    await db.execute("BEGIN IMMEDIATE")
                    await db.execute("DELETE FROM alert_reads")
                    await db.execute("DELETE FROM alerts")
                    await db.execute(
                        """
                        INSERT INTO alerts (title, description, created_at, read_count)
                        VALUES (?, ?, ?, 0)
                        """,
                        (title, description, now_ts),
                    )
                    await db.commit()
                except Exception:
                    await db.execute("ROLLBACK")
                    raise

            await self.parent_cog._load_current_alert()

            embed = discord.Embed(
                title="Alert Pushed",
                description="The alert has been successfully pushed!",
                color=discord.Color.green(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

        async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
            embed = discord.Embed(
                title="Error",
                description=f"An error occurred while pushing the alert: `{error}`",
                color=discord.Color.red(),
            )
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="pa", description="Push an alert (dev only)")
    async def push_alert(self, interaction: discord.Interaction) -> None:
     
        if interaction.user.id != 758576879715483719:
            embed = discord.Embed(
                title="This is a developer-only command!",
                description="You are not allowed to use this command.",
                color=discord.Color.red(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        await interaction.response.send_modal(self.PushAlertModal(self))

    @app_commands.command(name="alert", description="Read the last important message from the developer.")
    async def alert(self, interaction: discord.Interaction) -> None:
        if self._current_alert is None:
            embed = discord.Embed(
                title="No Active Alerts",
                description="There are currently no active alerts.",
                color=discord.Color(0x337fd5),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        alert = self._current_alert
        user_id = interaction.user.id
        key = (alert.id, user_id)
        now = time.time()

        self._cleanup_position_cache(now)

        cached = self._position_cache.get(key)
        if cached is not None:
            position = cached[0]
        else:
            async with self._acquire_db() as db:
                db.row_factory = aiosqlite.Row
                try:
                    await db.execute("BEGIN IMMEDIATE")

                    cursor = await db.execute(
                        """
                        SELECT position FROM alert_reads
                        WHERE alert_id = ? AND user_id = ?
                        """,
                        (alert.id, user_id),
                    )
                    row = await cursor.fetchone()
                    await cursor.close()

                    if row:
                        position = row["position"]
                    else:
                        await db.execute(
                            "UPDATE alerts SET read_count = read_count + 1 WHERE id = ?",
                            (alert.id,),
                        )

                        cursor2 = await db.execute(
                            "SELECT read_count FROM alerts WHERE id = ?",
                            (alert.id,),
                        )
                        row2 = await cursor2.fetchone()
                        await cursor2.close()

                        position = int(row2["read_count"]) if row2 else 1

                        await db.execute(
                            """
                            INSERT INTO alert_reads (alert_id, user_id, position)
                            VALUES (?, ?, ?)
                            """,
                            (alert.id, user_id, position),
                        )

                    await db.commit()
                except Exception:
                    await db.execute("ROLLBACK")
                    raise

            self._position_cache[key] = (position, now + self.POSITION_CACHE_TTL)

        embed = discord.Embed(
            title=alert.title,
            description=alert.description,
            color=discord.Color(0xFFFFFF),
        )
        embed.set_footer(text=f"You are #{position} to read this alert!")

        await interaction.response.send_message(embed=embed, ephemeral=False)

    def _cleanup_position_cache(self, now: Optional[float] = None) -> None:
        if not self._position_cache:
            return
        if now is None:
            now = time.time()
        to_delete = [k for k, (_, expiry) in self._position_cache.items() if expiry <= now]
        for key in to_delete:
            self._position_cache.pop(key, None)

    def _cleanup_reminder_cooldowns(self, now: Optional[float] = None) -> None:
        if not self._reminder_cooldowns:
            return
        if now is None:
            now = time.time()
        to_delete = [k for k, expiry in self._reminder_cooldowns.items() if expiry <= now]
        for key in to_delete:
            self._reminder_cooldowns.pop(key, None)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type is not discord.InteractionType.application_command:
            return

        alert = self._current_alert
        if alert is None:
            return

        if interaction.user.bot:
            return

        user_id = interaction.user.id
        now = time.time()

        self._cleanup_reminder_cooldowns(now)

        expiry = self._reminder_cooldowns.get(user_id)
        if expiry is not None and expiry > now:
            return

        key = (alert.id, user_id)
        self._cleanup_position_cache(now)
        if key in self._position_cache:
            return

        async with self._acquire_db() as db:
            cursor = await db.execute(
                """
                SELECT 1 FROM alert_reads
                WHERE alert_id = ? AND user_id = ?
                """,
                (alert.id, user_id),
            )
            row = await cursor.fetchone()
            await cursor.close()

        if row:
            self._position_cache[key] = (0, now + self.POSITION_CACHE_TTL)
            return

        self._reminder_cooldowns[user_id] = now + self.REMINDER_COOLDOWN_TTL

        async def send_reminder():
            try:
                for _ in range(20):
                    if interaction.response.is_done():
                        break
                    await asyncio.sleep(0.1)

                embed = discord.Embed(
                    title="You have an unread alert!",
                    description="Use </alert:1445801945775214715> to read it!",
                    color=discord.Color(0x337fd5),
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
            except Exception:
                pass

        asyncio.create_task(send_reminder())


async def setup(bot: commands.Bot):
    await bot.add_cog(Alerts(bot))
