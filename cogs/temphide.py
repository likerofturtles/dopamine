import discord
from discord.ext import commands, tasks
from discord import app_commands
import codecs
import aiosqlite
import asyncio
import time
from contextlib import asynccontextmanager
from typing import Optional, Dict
from config import TDB_PATH


class TempHideCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.DB_PATH = TDB_PATH
        self.message_cache: Dict[int, dict] = {}
        self.db_pool: Optional[asyncio.Queue[aiosqlite.Connection]] = None
        self._max_pool_size = 5

    async def cog_load(self):
        await self.init_pools(self._max_pool_size)
        await self.init_db()
        await self.populate_caches()
        self.bot.add_view(RevealView(self, 0))


    async def cog_unload(self):

        if self.db_pool:
            while not self.db_pool.empty():
                conn = await self.db_pool.get()
                await conn.close()

    async def init_pools(self, pool_size: int = 5):
        if self.db_pool is None:
            self.db_pool = asyncio.Queue(maxsize=pool_size)
            for _ in range(pool_size):
                conn = await aiosqlite.connect(
                    self.DB_PATH,
                    timeout=5,
                    isolation_level=None,
                )
                await conn.execute("PRAGMA busy_timeout=5000")
                await conn.execute("PRAGMA journal_mode=WAL")
                await conn.execute("PRAGMA synchronous=NORMAL")
                await conn.commit()
                await self.db_pool.put(conn)

    @asynccontextmanager
    async def acquire_db(self):
        conn = await self.db_pool.get()
        try:
            yield conn
        finally:
            await self.db_pool.put(conn)

    async def init_db(self):
        async with self.acquire_db() as db:
            await db.execute('''
                             CREATE TABLE IF NOT EXISTS temp_messages
                             (
                                 message_id INTEGER PRIMARY KEY,
                                 user_id INTEGER NOT NULL,
                                 hidden_text TEXT NOT NULL,
                                 timestamp REAL NOT NULL
                             )
                             ''')
            await db.commit()

    async def populate_caches(self):
        self.message_cache.clear()
        async with self.acquire_db() as db:
            async with db.execute("SELECT * FROM temp_messages") as cursor:
                rows = await cursor.fetchall()
                columns = [column[0] for column in cursor.description]
                for row in rows:
                    data = dict(zip(columns, row))
                    self.message_cache[data["message_id"]] = data


    async def store_message(self, user_id: int, hidden_text: str, message_id: int, timestamp: float):
        data = {
            "message_id": message_id,
            "user_id": user_id,
            "hidden_text": hidden_text,
            "timestamp": timestamp
        }

        async with self.acquire_db() as db:
            await db.execute(
                'INSERT INTO temp_messages (message_id, user_id, hidden_text, timestamp) VALUES (?, ?, ?, ?)',
                (message_id, user_id, hidden_text, timestamp)
            )
            await db.commit()

        self.message_cache[message_id] = data

    async def delete_message(self, message_id: int):
        async with self.acquire_db() as db:
            await db.execute('DELETE FROM temp_messages WHERE message_id = ?', (message_id,))
            await db.commit()

        if message_id in self.message_cache:
            del self.message_cache[message_id]

    async def get_message(self, message_id: int) -> Optional[tuple[int, str]]:
        data = self.message_cache.get(message_id)
        if data:
            return (data["user_id"], data["hidden_text"])
        return None


    @staticmethod
    async def send_error_reply(interaction_or_ctx, embed=None, message=None, ephemeral=True):
        try:
            if hasattr(interaction_or_ctx, 'response') and not interaction_or_ctx.response.is_done():
                if embed:
                    await interaction_or_ctx.response.send_message(embed=embed, ephemeral=ephemeral)
                else:
                    await interaction_or_ctx.response.send_message(message, ephemeral=ephemeral)
            elif hasattr(interaction_or_ctx, 'send'):
                if embed:
                    await interaction_or_ctx.send(embed=embed)
                else:
                    await interaction_or_ctx.send(message)
            else:
                if embed:
                    await interaction_or_ctx.followup.send(embed=embed, ephemeral=ephemeral)
                else:
                    await interaction_or_ctx.followup.send(message, ephemeral=ephemeral)
        except:
            pass

    async def handle_temphide(self, interaction_or_ctx, message_text: str):
        is_slash = hasattr(interaction_or_ctx, 'response')
        user = interaction_or_ctx.user if is_slash else interaction_or_ctx.author
        channel = interaction_or_ctx.channel

        if len(message_text.split()) > 1000:
            embed = discord.Embed(title="Message Too Long", description="Max 1000 words.", color=discord.Color.red())
            await self.send_error_reply(interaction_or_ctx, embed=embed)
            return

        current_time = time.time()
        encoded = await asyncio.to_thread(codecs.encode, message_text, 'rot13')
        view = RevealView(self, 0)

        try:
            content = f"{user.name}: {encoded}"
            sent_message = await interaction_or_ctx.followup.send(content,
                                                                  view=view) if is_slash else await channel.send(
                content, view=view)

            view.message_id = sent_message.id
            await self.store_message(user.id, message_text, sent_message.id, current_time)

            if is_slash:
                await interaction_or_ctx.followup.send("Hidden message created!", ephemeral=True)
        except Exception:
            embed = discord.Embed(title="Error", description="Failed to create message.", color=discord.Color.red())
            await self.send_error_reply(interaction_or_ctx, embed=embed)

    @app_commands.command(name="temphide", description="Send a hidden message that only you can reveal")
    async def temphide_slash(self, interaction: discord.Interaction, message: str):
        await self.handle_temphide(interaction, message)


class RevealView(discord.ui.View):
    def __init__(self, cog: TempHideCog, message_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.message_id = message_id

    @discord.ui.button(label='Reveal', style=discord.ButtonStyle.primary, custom_id='reveal_button')
    async def reveal_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        message_data = await self.cog.get_message(self.message_id)

        if not message_data:
            return await interaction.response.send_message("Already revealed or expired.", ephemeral=True)

        user_id, hidden_text = message_data
        if interaction.user.id != user_id:
            return await interaction.response.send_message("Not your message!", ephemeral=True)

        await interaction.response.defer()
        try:
            await interaction.message.edit(content=f"{interaction.user.name}: {hidden_text}", view=None)
            await self.cog.delete_message(self.message_id)
        except discord.NotFound:
            await self.cog.delete_message(self.message_id)
        except:
            pass


async def setup(bot):
    await bot.add_cog(TempHideCog(bot))