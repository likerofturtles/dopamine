import discord
from discord.ext import commands
import aiosqlite
import asyncio
import aiohttp
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from typing import Optional, Dict, Tuple
from config import TOPDB_PATH, TOPGG_API_URL, TOPGG_TOKEN

TOPGG_BOT_TOKEN = TOPGG_TOKEN

VOTER_CACHE_TTL = timedelta(minutes=15)
VOTE_CHECK_COOLDOWN = timedelta(hours=12, minutes=30)


class TopGGVoter(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        self._voter_cache: Dict[int, Tuple[bool, datetime]] = {}
        self._db_pool: list[aiosqlite.Connection] = []
        self._pool_lock = asyncio.Lock()
        self._pool_semaphore: Optional[asyncio.Semaphore] = None
        self._max_pool_size = 5

    @asynccontextmanager
    async def open_db(self):
        """Async context manager that yields a pooled SQLite connection."""
        if not self._db_pool:
            await self._init_db_pool()

        await self._pool_semaphore.acquire()
        db = self._db_pool.pop()
        try:
            yield db
            await db.commit()
        finally:
            self._db_pool.append(db)
            self._pool_semaphore.release()

    async def _init_db_pool(self):
        """Initialize the database connection pool with optimized settings."""
        async with self._pool_lock:
            if self._db_pool:
                return

            created_conns: list[aiosqlite.Connection] = []
            max_retries = 5

            for _ in range(self._max_pool_size):
                for attempt in range(max_retries):
                    try:
                        db = await aiosqlite.connect(TOPDB_PATH, timeout=5.0)
                        await db.execute("PRAGMA busy_timeout=5000")
                        await db.execute("PRAGMA journal_mode=WAL")
                        await db.execute("PRAGMA wal_autocheckpoint=1000")
                        await db.execute("PRAGMA synchronous=NORMAL")
                        await db.execute("PRAGMA cache_size=-64000")
                        await db.execute("PRAGMA foreign_keys=ON")
                        await db.commit()
                        created_conns.append(db)
                        break
                    except Exception:
                        if attempt < max_retries - 1:
                            await asyncio.sleep(0.1 * (2 ** attempt))
                            continue
                        raise

            self._db_pool = created_conns
            self._pool_semaphore = asyncio.Semaphore(len(self._db_pool))

    async def init_topgg_db(self):
        """Initialize the SQLite database for storing voter information"""
        async with self.open_db() as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS voters (
                    user_id INTEGER PRIMARY KEY,
                    voted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_checked TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_voters_voted_at 
                ON voters(voted_at)
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_voters_last_checked 
                ON voters(last_checked)
                """
            )

    async def cog_load(self):
        """Initialize database and HTTP session when cog loads"""
        await self.init_topgg_db()
        self.session = aiohttp.ClientSession()

    async def cog_unload(self):
        """Clean up HTTP session when cog unloads"""
        if self.session:
            await self.session.close()
        for conn in self._db_pool:
            try:
                await conn.close()
            except Exception:
                pass
        self._db_pool.clear()
        self._pool_semaphore = None

    async def _update_vote_check(self, user_id: int, has_voted: bool):
        """Update or insert a vote check record with the latest status and timestamp."""
        now = datetime.now()
        voted_at = now if has_voted else None
        async with self.open_db() as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO voters (user_id, voted_at, last_checked)
                VALUES (?, ?, ?)
                """,
                (user_id, voted_at, now),
            )

    async def has_user_voted(self, user_id: int) -> bool:
        """Check if user has voted on top.gg"""
        try:
            url = TOPGG_API_URL.format(bot_id=self.bot.user.id)
            headers = {"Authorization": TOPGG_BOT_TOKEN}
            params = {"userId": user_id}

            async with self.session.get(url, headers=headers, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    has_voted = data.get("voted", False)
                    await self._update_vote_check(user_id, has_voted)
                    return has_voted
                elif response.status == 429:
                    retry_after = response.headers.get("Retry-After")
                    try:
                        if retry_after is not None:
                            delay = float(retry_after)
                            if delay > 0:
                                await asyncio.sleep(delay)
                    except (ValueError, TypeError):
                        pass
                    print(f"Rate limited by Top.gg API")
                    return False
                else:
                    print(f"Top.gg API error: {response.status}")
                    return False
        except Exception as e:
            print(f"Error checking vote status: {e}")
            return False

    async def store_voter(self, user_id: int):
        """Store voter information in database"""
        now = datetime.now()
        async with self.open_db() as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO voters (user_id, voted_at, last_checked)
                VALUES (?, ?, ?)
                """,
                (user_id, now, now),
            )
        self._voter_cache[user_id] = (True, now + VOTER_CACHE_TTL)

    async def is_voter(self, user_id: int) -> bool:
        """Check if user is a registered voter in database"""
        now = datetime.now()

        cached = self._voter_cache.get(user_id)
        if cached:
            status, expiry = cached
            if expiry > now:
                return status
            del self._voter_cache[user_id]

        async with self.open_db() as db:
            cursor = await db.execute(
                "SELECT 1 FROM voters WHERE user_id = ? AND voted_at IS NOT NULL",
                (user_id,),
            )
            result = await cursor.fetchone()

        is_voter_status = result is not None
        expiry = now + VOTER_CACHE_TTL
        self._voter_cache[user_id] = (is_voter_status, expiry)
        return is_voter_status

    async def should_check_topgg(self, user_id: int) -> bool:
        """
        Determine whether we should call the Top.gg API for this user
        based on the last time we checked their vote status.
        """
        async with self.open_db() as db:
            cursor = await db.execute(
                "SELECT last_checked FROM voters WHERE user_id = ?", (user_id,)
            )
            row = await cursor.fetchone()

        if row and row[0]:
            try:
                last_checked = datetime.fromisoformat(row[0])
            except (TypeError, ValueError):
                return True

            return datetime.now() - last_checked > VOTE_CHECK_COOLDOWN

        return True

    async def check_vote_access(self, user_id: int) -> bool:
        """Check if user has vote access (either in DB or current vote)"""
        if await self.is_voter(user_id):
            return True

        if not await self.should_check_topgg(user_id):
            return False

        has_voted = await self.has_user_voted(user_id)

        if has_voted:
            await self.store_voter(user_id)
            return True

        return False
    
    async def cleanup_old_voters(self, max_age_days: int = 30):
        """Remove voters older than max_age_days to prevent database bloat (optional maintenance)"""
        cutoff_date = datetime.now() - timedelta(days=max_age_days)
        async with self.open_db() as db:
            await db.execute(
                "DELETE FROM voters WHERE voted_at < ? AND last_checked < ?",
                (cutoff_date, cutoff_date)
            )

async def setup(bot):
    await bot.add_cog(TopGGVoter(bot))
