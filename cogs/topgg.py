import discord
from discord.ext import commands
import aiosqlite
import asyncio
import aiohttp
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from typing import Optional, Dict, Tuple, Set
from config import TOPDB_PATH, TOPGG_API_URL, TOPGG_TOKEN
from config import OVERRIDE_VOTEWALL

TOPGG_BOT_TOKEN = TOPGG_TOKEN
VOTE_CHECK_COOLDOWN = timedelta(hours=12, minutes=30)


class TopGGVoter(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        self.voter_cache: Dict[int, dict] = {}
        self.db_pool: Optional[asyncio.Queue[aiosqlite.Connection]] = None

    @asynccontextmanager
    async def acquire_db(self):
        if self.db_pool is None:
            await self.init_pools()

        conn = await self.db_pool.get()
        try:
            yield conn
            await conn.commit()
        finally:
            await self.db_pool.put(conn)

    async def init_pools(self, pool_size: int = 5):
        if self.db_pool is None:
            self.db_pool = asyncio.Queue(maxsize=pool_size)
            for _ in range(pool_size):
                conn = await aiosqlite.connect(
                    TOPDB_PATH,
                    timeout=5.0,
                    isolation_level=None,
                )
                await conn.execute("PRAGMA busy_timeout=5000")
                await conn.execute("PRAGMA journal_mode=WAL")
                await conn.execute("PRAGMA synchronous=NORMAL")
                await conn.execute("PRAGMA cache_size=-64000")
                await conn.execute("PRAGMA foreign_keys=ON")
                await conn.commit()
                await self.db_pool.put(conn)

    async def init_db(self):
        async with self.acquire_db() as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS voters (
                    user_id INTEGER PRIMARY KEY,
                    voted_at TIMESTAMP,
                    last_checked TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.execute("CREATE INDEX IF NOT EXISTS idx_voters_voted_at ON voters(voted_at)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_voters_last_checked ON voters(last_checked)")

    async def populate_caches(self):
        self.voter_cache.clear()
        async with self.acquire_db() as db:
            async with db.execute("SELECT user_id, voted_at, last_checked FROM voters") as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    user_id, voted_at_str, last_checked_str = row

                    voted_at = datetime.fromisoformat(voted_at_str) if voted_at_str else None
                    last_checked = datetime.fromisoformat(last_checked_str) if last_checked_str else datetime.now()

                    self.voter_cache[user_id] = {
                        "voted_at": voted_at,
                        "last_checked": last_checked
                    }

    async def cog_load(self):
        self.session = aiohttp.ClientSession()
        await self.init_pools()
        await self.init_db()
        await self.populate_caches()

    async def cog_unload(self):
        if self.session:
            await self.session.close()

        if self.db_pool:
            while not self.db_pool.empty():
                conn = await self.db_pool.get()
                await conn.close()

    async def _update_vote_record(self, user_id: int, has_voted: bool):
        now = datetime.now()
        voted_at = now if has_voted else None

        async with self.acquire_db() as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO voters (user_id, voted_at, last_checked)
                VALUES (?, ?, ?)
                """,
                (user_id, voted_at.isoformat() if voted_at else None, now.isoformat()),
            )

        self.voter_cache[user_id] = {
            "voted_at": voted_at,
            "last_checked": now
        }

    async def has_user_voted(self, user_id: int) -> bool:
        if OVERRIDE_VOTEWALL:
            return True

        try:
            url = TOPGG_API_URL.format(bot_id=self.bot.user.id)
            headers = {"Authorization": TOPGG_BOT_TOKEN}
            params = {"userId": user_id}

            async with self.session.get(url, headers=headers, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    has_voted = data.get("voted", False)
                    await self._update_vote_record(user_id, has_voted)
                    return has_voted
                elif response.status == 429:
                    print(f"Rate limited by Top.gg API")
                    return False
                else:
                    print(f"Top.gg API error: {response.status}")
                    return False
        except Exception as e:
            print(f"Error checking vote status: {e}")
            return False

    async def is_voter(self, user_id: int) -> bool:
        data = self.voter_cache.get(user_id)
        if data and data["voted_at"] is not None:
            return True
        return False

    async def should_check_topgg(self, user_id: int) -> bool:
        data = self.voter_cache.get(user_id)
        if not data:
            return True

        last_checked = data["last_checked"]
        return datetime.now() - last_checked > VOTE_CHECK_COOLDOWN

    async def check_vote_access(self, user_id: int) -> bool:
        if await self.is_voter(user_id):
            return True

        if not await self.should_check_topgg(user_id):
            return False

        has_voted = await self.has_user_voted(user_id)
        return has_voted

    async def cleanup_old_voters(self, max_age_days: int = 15):
        cutoff_date = datetime.now() - timedelta(days=max_age_days)
        async with self.acquire_db() as db:
            await db.execute(
                "DELETE FROM voters WHERE voted_at < ? AND last_checked < ?",
                (cutoff_date.isoformat(), cutoff_date.isoformat())
            )
        await self.populate_caches()


async def setup(bot):
    await bot.add_cog(TopGGVoter(bot))