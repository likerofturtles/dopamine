import discord
from discord.ext import commands, tasks
from discord import app_commands
import codecs
import aiosqlite
import asyncio
import time
from contextlib import asynccontextmanager
from typing import Optional
from config import TDB_PATH

class TempHideCog(commands.Cog):
    """Temporary hidden message functionality with ROT13 encoding."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.DB_PATH = TDB_PATH
        self.cooldowns: dict[int, float] = {}
        self._db_pool: list[aiosqlite.Connection] = []
        self._pool_lock = asyncio.Lock()
        self._pool_semaphore: Optional[asyncio.Semaphore] = None
        self._max_pool_size = 5
    
    def cleanup_old_cooldowns(self, max_age_seconds: int = 300):
        """Remove cooldown entries older than max_age_seconds (default 5 minutes) to prevent memory leaks"""
        current_time = time.time()
        for user_id in list(self.cooldowns.keys()):
            if current_time - self.cooldowns[user_id] > max_age_seconds:
                del self.cooldowns[user_id]

    async def cog_load(self):
        """Initialize database on cog load."""
        await self.init_temp_db()
        self.bot.add_view(RevealView(self, 0))
        if not self._cooldown_cleanup.is_running():
            self._cooldown_cleanup.start()

    async def cog_unload(self):
        """Close database connections on cog unload."""
        if self._cooldown_cleanup.is_running():
            self._cooldown_cleanup.cancel()
        for conn in self._db_pool:
            try:
                await conn.close()
            except Exception:
                pass
        self._db_pool.clear()
        self._pool_semaphore = None

    async def _init_db_pool(self):
        """Initialize the connection pool with optimized settings."""
        async with self._pool_lock:
            if self._db_pool:
                return

            max_retries = 5
            created_conns: list[aiosqlite.Connection] = []

            for _ in range(self._max_pool_size):
                attempt = 0
                while True:
                    try:
                        db = await aiosqlite.connect(self.DB_PATH, timeout=5.0)
                        await db.execute("PRAGMA busy_timeout=5000")
                        await db.execute("PRAGMA journal_mode=WAL")
                        await db.execute("PRAGMA wal_autocheckpoint=1000")
                        await db.execute("PRAGMA synchronous=NORMAL")
                        await db.execute("PRAGMA cache_size=-64000")
                        await db.execute("PRAGMA foreign_keys=ON")
                        await db.execute("PRAGMA optimize")
                        await db.commit()
                        created_conns.append(db)
                        break
                    except Exception:
                        attempt += 1
                        if attempt < max_retries:
                            await asyncio.sleep(0.1 * (2 ** (attempt - 1)))
                            continue
                        raise

            self._db_pool = created_conns
            self._pool_semaphore = asyncio.Semaphore(len(self._db_pool))

    @asynccontextmanager
    async def get_db_connection(self):
        """Async context manager that yields a pooled database connection."""
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

    @tasks.loop(minutes=5)
    async def _cooldown_cleanup(self):
        """Periodically clean up old cooldown entries to keep memory usage bounded."""
        self.cleanup_old_cooldowns(300)

    async def init_temp_db(self):
        """Initialize the database with optimized settings."""
        async with self.get_db_connection() as db:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS temp_messages (
                    message_id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    hidden_text TEXT NOT NULL,
                    timestamp REAL NOT NULL
                )
            ''')
            await db.execute('''
                CREATE INDEX IF NOT EXISTS idx_temp_user 
                ON temp_messages(user_id)
            ''')
            await db.execute('''
                CREATE INDEX IF NOT EXISTS idx_temp_timestamp 
                ON temp_messages(timestamp)
            ''')
            await db.commit()

    async def store_message(self, user_id: int, hidden_text: str, message_id: int, timestamp: float):
        """Store a hidden message in the database."""
        async with self.get_db_connection() as db:
            await db.execute(
                'INSERT INTO temp_messages (message_id, user_id, hidden_text, timestamp) VALUES (?, ?, ?, ?)',
                (message_id, user_id, hidden_text, timestamp)
            )
            await db.commit()

    async def get_message(self, message_id: int) -> Optional[tuple[int, str]]:
        """Retrieve a hidden message from the database."""
        async with self.get_db_connection() as db:
            cursor = await db.execute(
                'SELECT user_id, hidden_text FROM temp_messages WHERE message_id = ?',
                (message_id,)
            )
            row = await cursor.fetchone()
            await cursor.close()
            return row if row else None

    async def delete_message(self, message_id: int):
        """Delete a hidden message from the database."""
        async with self.get_db_connection() as db:
            await db.execute('DELETE FROM temp_messages WHERE message_id = ?', (message_id,))
            await db.commit()

    @staticmethod
    async def send_error_reply(interaction_or_ctx, embed=None, message=None, ephemeral=True):
        """Helper function to send error messages."""
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
        """Handle the temphide command logic."""
        is_slash_command = hasattr(interaction_or_ctx, 'response')

        if is_slash_command:
            user = interaction_or_ctx.user
            channel = interaction_or_ctx.channel
        else:
            user = interaction_or_ctx.author
            channel = interaction_or_ctx.channel

        word_count = len(message_text.split())
        if word_count > 1000:
            embed = discord.Embed(
                title="Message Too Long",
                description="Your message exceeds the 1000 word limit.",
                color=discord.Color.red()
            )
            await self.send_error_reply(interaction_or_ctx, embed=embed)
            return

        current_time = time.time()
        if user.id in self.cooldowns:
            time_since_last = current_time - self.cooldowns[user.id]
            if time_since_last < 60:
                remaining_time = int(60 - time_since_last)
                embed = discord.Embed(
                    description=f"This command is under cooldown. Please wait **{remaining_time}** seconds.",
                    color=discord.Color.red()
                )
                if is_slash_command:
                    await interaction_or_ctx.response.send_message(embed=embed, ephemeral=True)
                else:
                    await self.send_error_reply(interaction_or_ctx, embed=embed)
                return

        self.cooldowns[user.id] = current_time

        if is_slash_command:
            await interaction_or_ctx.response.defer()

        if not is_slash_command:
            try:
                await interaction_or_ctx.message.delete()
            except:
                pass

        encoded_message = await asyncio.to_thread(codecs.encode, message_text, 'rot13')

        view = RevealView(self, 0)

        try:
            if is_slash_command:
                sent_message = await interaction_or_ctx.followup.send(
                    f"{user.name}: {encoded_message}",
                    view=view
                )
            else:
                sent_message = await channel.send(
                    f"{user.name}: {encoded_message}",
                    view=view
                )

            view.message_id = sent_message.id

            await self.store_message(user.id, message_text, sent_message.id, current_time)

            if is_slash_command:
                await interaction_or_ctx.followup.send(
                    "Hidden message created successfully! Click the Reveal button to reveal it.",
                    ephemeral=True
                )

        except Exception as e:
            embed = discord.Embed(
                title="Error",
                description="Failed to create hidden message. Please try again.",
                color=discord.Color.red()
            )
            await self.send_error_reply(interaction_or_ctx, embed=embed)

    @app_commands.command(name="temphide", description="Send a hidden message that only you can reveal")
    @app_commands.describe(message="The message you want to hide (max 1000 words)")
    async def temphide_slash(self, interaction: discord.Interaction, message: str):
        """Slash command for temphide."""
        await self.handle_temphide(interaction, message)

    @commands.command(name="temphide")
    async def temphide_prefix(self, ctx: commands.Context, *, message: str):
        """Prefix command for temphide."""
        await self.handle_temphide(ctx, message)


class RevealView(discord.ui.View):
    """Persistent view for revealing hidden messages."""

    def __init__(self, cog: TempHideCog, message_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.message_id = message_id

    @discord.ui.button(label='Reveal', style=discord.ButtonStyle.primary, custom_id='reveal_button')
    async def reveal_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle the reveal button click."""
        message_data = await self.cog.get_message(self.message_id)

        if not message_data:
            await interaction.response.send_message(
                "This message has already been revealed or doesn't exist.",
                ephemeral=True
            )
            return

        user_id, hidden_text = message_data

        if interaction.user.id != user_id:
            await interaction.response.send_message(
                "You can only reveal your own hidden messages.",
                ephemeral=True
            )
            return

        await interaction.response.defer()

        try:
            await interaction.message.edit(
                content=f"{interaction.user.name}: {hidden_text}",
                view=None
            )

            await self.cog.delete_message(self.message_id)

        except discord.NotFound:
            await self.cog.delete_message(self.message_id)
        except Exception as e:
            pass

async def setup(bot):
    await bot.add_cog(TempHideCog(bot))
