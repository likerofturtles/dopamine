from datetime import datetime, timedelta
from typing import Optional, Dict

import aiohttp
import discord
from discord.ext import commands

from config import TOPGG_API_URL, TOPGG_TOKEN
from utils.data_handlers import export_table
from utils.data_protocol import DataDeleteResult, DataExportChunk, DataFeatureMeta, DataMonitorResult

TOPGG_BOT_TOKEN = TOPGG_TOKEN
VOTE_CHECK_COOLDOWN = timedelta(hours=12, minutes=30)


class TopGGVoter(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        self.voter_cache: Dict[int, dict] = {}

    async def populate_caches(self):
        self.voter_cache.clear()
        rows = await self.bot.db.execute("SELECT user_id, voted_at, last_checked FROM voters")
        for row in rows:
            user_id = row["user_id"]
            voted_at_str = row["voted_at"]
            last_checked_str = row["last_checked"]

            voted_at = datetime.fromisoformat(voted_at_str) if voted_at_str else None
            last_checked = datetime.fromisoformat(last_checked_str) if last_checked_str else datetime.now()

            self.voter_cache[user_id] = {
                "voted_at": voted_at,
                "last_checked": last_checked
            }

    async def cog_load(self):
        self.session = aiohttp.ClientSession()
        await self.bot.db.wait_ready()
        await self.populate_caches()

    async def cog_unload(self):
        if self.session:
            await self.session.close()

    async def _update_vote_record(self, user_id: int, has_voted: bool):
        now = datetime.now()

        if has_voted:
            await self.bot.db.execute_write(
                """
                INSERT INTO voters (user_id, voted_at, last_checked)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    voted_at = excluded.voted_at,
                    last_checked = excluded.last_checked
                """,
                (user_id, now.isoformat(), now.isoformat()),
            )
            self.voter_cache[user_id] = {"voted_at": now, "last_checked": now}
        else:
            await self.bot.db.execute_write(
                """
                INSERT INTO voters (user_id, last_checked)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    last_checked = excluded.last_checked
                """,
                (user_id, now.isoformat()),
            )
            if user_id in self.voter_cache:
                self.voter_cache[user_id]["last_checked"] = now
            else:
                self.voter_cache[user_id] = {"voted_at": None, "last_checked": now}

    async def has_user_voted(self, user_id: int) -> bool:
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
                    print("Rate limited by Top.gg API")
                    return False
                else:
                    print(f"Top.gg API error: {response.status}")
                    return False
        except Exception as e:
            print(f"Error checking vote status: {e}")
            return False

    async def is_voter(self, user_id: int) -> bool:
        data = self.voter_cache.get(user_id)
        if not data or data["voted_at"] is None:
            return False

        voter_window = timedelta(hours=12)
        return datetime.now() - data["voted_at"] < voter_window

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

        return await self.has_user_voted(user_id)

    async def cleanup_old_voters(self, max_age_days: int = 15):
        cutoff_date = datetime.now() - timedelta(days=max_age_days)
        await self.bot.db.execute_write(
            "DELETE FROM voters WHERE voted_at < ? AND last_checked < ?",
            (cutoff_date.isoformat(), cutoff_date.isoformat())
        )
        await self.populate_caches()

    def data_features(self) -> list[DataFeatureMeta]:
        return [DataFeatureMeta(
            feature_id="topgg",
            name="Top.gg Voting",
            user_export=True,
            user_delete=True,
        )]

    async def data_export_user(self, user_id: int, *, guild_ids: list[int] | None) -> DataExportChunk:
        chunk = DataExportChunk(feature_id="topgg")
        rows = await export_table(
            self.bot.db,
            "SELECT user_id, voted_at, last_checked FROM voters WHERE user_id = ?",
            (user_id,),
        )
        if rows:
            chunk.global_data["voter_record"] = rows[0]
        return chunk

    async def data_export_guild(self, guild_id: int) -> DataExportChunk:
        return DataExportChunk(feature_id="topgg")

    async def data_delete_user(self, user_id: int, *, guild_ids: list[int] | None, feature_id: str | None) -> DataDeleteResult:
        if feature_id and feature_id != "topgg":
            return DataDeleteResult(feature_id="topgg")
        rows_affected = await self.bot.db.execute_write("DELETE FROM voters WHERE user_id = ?", (user_id,))
        self.voter_cache.pop(user_id, None)
        return DataDeleteResult(feature_id="topgg", deleted=True, rows_affected=rows_affected)

    async def data_delete_guild(self, guild_id: int, feature_id: str | None) -> DataDeleteResult:
        return DataDeleteResult(feature_id="topgg")

    async def data_monitor_guild(self, guild: discord.Guild) -> DataMonitorResult:
        return DataMonitorResult(feature_id="topgg")


async def setup(bot):
    await bot.add_cog(TopGGVoter(bot))
