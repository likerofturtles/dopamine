import asyncio
import aiosqlite
import os
from typing import Optional, Dict
from config import LDB_PATH


class LoggingManager:
    def __init__(self):
        self.log_channel_cache: Dict[int, int] = {}
        self.db_pool: Optional[asyncio.Queue] = None

    async def init_pools(self, pool_size: int = 5):
        if self.db_pool is None:
            self.db_pool = asyncio.Queue(maxsize=pool_size)
            for _ in range(pool_size):
                conn = await aiosqlite.connect(
                    LDB_PATH,
                    timeout=5,
                )
                await conn.execute("PRAGMA busy_timeout=5000")
                await conn.execute("PRAGMA journal_mode=WAL")
                await conn.execute("PRAGMA synchronous = NORMAL")
                await conn.commit()
                await self.db_pool.put(conn)

    async def acquire_db(self):
        if self.db_pool is None:
            await self.init_pools()
        conn = await self.db_pool.get()
        try:
            yield conn
        finally:
            await self.db_pool.put(conn)

    async def init_db(self):
        async def run_init():
            async with self.acquire_db() as db:
                await db.execute('''
                                 CREATE TABLE IF NOT EXISTS log_channels
                                 (
                                     guild_id
                                     INTEGER
                                     PRIMARY
                                     KEY,
                                     channel_id
                                     INTEGER
                                 )
                                 ''')

        await run_init()

    async def populate_cache(self):
        self.log_channel_cache.clear()
        async with self.acquire_db() as db:
            async with db.execute("SELECT guild_id, channel_id FROM log_channels") as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    self.log_channel_cache[row[0]] = row[1]

    async def log_get(self, guild_id: int) -> Optional[int]:
        if guild_id in self.log_channel_cache:
            return self.log_channel_cache[guild_id]

        async with self.acquire_db() as db:
            async with db.execute("SELECT channel_id FROM log_channels WHERE guild_id = ?", (guild_id,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    self.log_channel_cache[guild_id] = row[0]
                    return row[0]
        return None

    async def log_set(self, guild_id: int, channel_id: int):
        async with self.acquire_db() as db:
            await db.execute(
                "INSERT OR REPLACE INTO log_channels (guild_id, channel_id) VALUES (?, ?)",
                (guild_id, channel_id)
            )
            await db.commit()
        self.log_channel_cache[guild_id] = channel_id