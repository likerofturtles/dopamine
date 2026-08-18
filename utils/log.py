from typing import Optional, Dict


class LoggingManager:
    def __init__(self, db):
        self.log_channel_cache: Dict[int, int] = {}
        self.db = db

    async def populate_cache(self):
        self.log_channel_cache.clear()
        rows = await self.db.execute("SELECT guild_id, channel_id FROM log_channels")
        for row in rows:
            self.log_channel_cache[row["guild_id"]] = row["channel_id"]

    async def log_get(self, guild_id: int) -> Optional[int]:
        if guild_id in self.log_channel_cache:
            return self.log_channel_cache[guild_id]

        rows = await self.db.execute("SELECT channel_id FROM log_channels WHERE guild_id = ?", (guild_id,))
        if rows:
            self.log_channel_cache[guild_id] = rows[0]["channel_id"]
            return rows[0]["channel_id"]
        return None

    async def log_set(self, guild_id: int, channel_id: int):
        await self.db.execute(
            "INSERT OR REPLACE INTO log_channels (guild_id, channel_id) VALUES (?, ?)",
            (guild_id, channel_id)
        )
        self.log_channel_cache[guild_id] = channel_id

    async def log_remove(self, guild_id: int):
        await self.db.execute("DELETE FROM log_channels WHERE guild_id = ?", (guild_id,))
        self.log_channel_cache.pop(guild_id, None)