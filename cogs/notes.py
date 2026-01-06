import discord
from discord.ext import commands, tasks
from discord import app_commands
from discord.ui import Modal, TextInput
import aiosqlite
import asyncio
from typing import Optional
from contextlib import asynccontextmanager
from config import NOTEDB_PATH

note_group = app_commands.Group(name="note", description="Note management commands")

class Notes(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._db_pool: Optional[asyncio.Queue[aiosqlite.Connection]] = None
        self._pool_size: int = 8

    async def cog_load(self):
        """Called when the cog is loaded"""
        await self.init_db_pool()
        if not self._db_keepalive.is_running():
            self._db_keepalive.start()

    async def cog_unload(self):
        """Close database connection when cog is unloaded"""
        try:
            self.bot.tree.remove_command(note_group.name, type=discord.app_commands.AppCommandType.chat_input)
        except Exception:
            pass
        if self._db_keepalive.is_running():
            self._db_keepalive.cancel()
        if self._db_pool is not None:
            while not self._db_pool.empty():
                conn = await self._db_pool.get()
                try:
                    await conn.close()
                except Exception:
                    pass
            self._db_pool = None

    async def _create_one_connection(self):
        """Create a single aiosqlite connection with optimized settings."""
        max_retries = 5
        for attempt in range(max_retries):
            try:
                conn = await aiosqlite.connect(NOTEDB_PATH, timeout=5.0)
                await conn.execute("PRAGMA journal_mode=WAL")
                await conn.execute("PRAGMA wal_autocheckpoint=3000")
                await conn.execute("PRAGMA synchronous=NORMAL")
                await conn.execute("PRAGMA cache_size=-65536")
                await conn.execute("PRAGMA foreign_keys=ON")

                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS user_notes (
                        user_id INTEGER,
                        note_name TEXT,
                        note_content TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (user_id, note_name)
                    )
                    """
                )
                await conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_user_notes_user_id
                    ON user_notes (user_id)
                    """
                )
                await conn.commit()
                return conn
            except Exception:
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.1 * (2**attempt))
                    continue
                raise

    async def init_db_pool(self):
        """Initialize the async connection pool."""
        if self._db_pool is not None:
            return

        self._db_pool = asyncio.Queue(maxsize=self._pool_size)
        tasks = [self._create_one_connection() for _ in range(self._pool_size)]
        connections = await asyncio.gather(*tasks)
        for conn in connections:
            await self._db_pool.put(conn)

    @asynccontextmanager
    async def db_connection(self):
        """
        Async context manager that provides a connection from the pool
        and returns it when done.
        """
        if self._db_pool is None:
            await self.init_db_pool()

        conn = await self._db_pool.get()
        try:
            yield conn
        finally:
            await self._db_pool.put(conn)

    @tasks.loop(seconds=60)
    async def _db_keepalive(self):
        try:
            async with self.db_connection() as db:
                cursor = await db.execute("SELECT 1")
                await cursor.fetchone()
        except Exception:
            pass

    async def check_vote_access(self, user_id: int) -> bool:
        """Check if user has vote access, with lightweight in-memory caching.

        This should be implemented based on your TopGGVoter cog.
        """
        return True

    class NoteModal(discord.ui.Modal, title="Create/Update Note"):
        note_name = discord.ui.TextInput(
            label="Note Name",
            placeholder="Enter a name for your note...",
            required=True,
            max_length=100
        )

        note_content = discord.ui.TextInput(
            label="Note Content",
            placeholder="Enter your note content here...",
            required=True,
            style=discord.TextStyle.paragraph,
            max_length=2000
        )

        def __init__(self, cog):
            super().__init__()
            self.cog = cog

        async def on_submit(self, interaction: discord.Interaction):
            name = self.note_name.value
            content = self.note_content.value

            try:
                async with self.cog.db_connection() as db:
                    await db.execute(
                        """
                        INSERT INTO user_notes (user_id, note_name, note_content)
                        VALUES (?, ?, ?)
                        ON CONFLICT(user_id, note_name) DO UPDATE SET
                            note_content = excluded.note_content,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        (interaction.user.id, name, content),
                    )
                    await db.commit()

                embed = discord.Embed(
                    title=name,
                    description=content,
                    color=discord.Color.green()
                )
                embed.set_footer(text="Note has been saved successfully! To retrieve it, use /note fetch <name>.")

                await interaction.response.send_message(embed=embed, ephemeral=True)

            except Exception as e:
                embed = discord.Embed(
                    title="Error: Failed to Save Note",
                    description=f"An error occurred while saving your note: {str(e)}",
                    color=discord.Color.red()
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)


async def _get_notes_cog(interaction: discord.Interaction) -> Optional[Notes]:
    bot = interaction.client
    return bot.get_cog('Notes')

@note_group.command(name="create", description="Open the UI to create a note")
@app_commands.allowed_contexts(discord.app_commands.AppCommandContext.guild, discord.app_commands.AppCommandContext.private_channel, discord.app_commands.AppCommandContext.dm_channel)
async def note_create(interaction: discord.Interaction):
    cog = await _get_notes_cog(interaction)
    if not cog:
        await interaction.response.send_message("Notes system unavailable.", ephemeral=True)
        return
    if not await cog.check_vote_access(interaction.user.id):
        embed = discord.Embed(
            title="Vote to Use This Feature!",
            description=f"This command requires voting! To access this feature, please vote for Dopamine here: [top.gg](https://top.gg/bot/{interaction.client.user.id})",
            color=0xffaa00
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    await interaction.response.send_modal(cog.NoteModal(cog))

async def _fetch_names_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    cog = await _get_notes_cog(interaction)
    if not cog:
        return []
    try:
        async with cog.db_connection() as db:
            cursor = await db.execute(
                "SELECT note_name FROM user_notes WHERE user_id = ? AND note_name LIKE ? ORDER BY note_name LIMIT 25",
                (interaction.user.id, f"{current}%"),
            )
            notes = await cursor.fetchall()
            return [app_commands.Choice(name=n[0], value=n[0]) for n in notes]
    except Exception:
        return []

@note_group.command(name="fetch", description="Retrieve a note by name")
@app_commands.autocomplete(name=_fetch_names_autocomplete)
@app_commands.allowed_contexts(discord.app_commands.AppCommandContext.guild, discord.app_commands.AppCommandContext.private_channel, discord.app_commands.AppCommandContext.dm_channel)
async def note_fetch(interaction: discord.Interaction, name: str):
    cog = await _get_notes_cog(interaction)
    if not cog:
        await interaction.response.send_message("Notes system unavailable.", ephemeral=True)
        return
    if not await cog.check_vote_access(interaction.user.id):
        embed = discord.Embed(
            title="Vote to Use This Feature!",
            description=f"This command requires voting! To access this feature, please vote for Dopamine here: [top.gg](https://top.gg/bot/{interaction.client.user.id})",
            color=0xffaa00
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    try:
        async with cog.db_connection() as db:
            cursor = await db.execute(
                "SELECT note_content FROM user_notes WHERE user_id = ? AND note_name = ?",
                (interaction.user.id, name),
            )
            note = await cursor.fetchone()
        if note:
            await interaction.response.send_message(note[0], ephemeral=True)
        else:
            embed = discord.Embed(
                title="Error: Note Not Found",
                description=f"No note found with the name '{name}'.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        embed = discord.Embed(
            title="Error: Failed to Retrieve Note",
            description=f"An error occurred while retrieving your note: {str(e)}",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

async def _delete_names_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    cog = await _get_notes_cog(interaction)
    if not cog:
        return []
    try:
        async with cog.db_connection() as db:
            cursor = await db.execute(
                "SELECT note_name FROM user_notes WHERE user_id = ? AND note_name LIKE ? ORDER BY note_name LIMIT 25",
                (interaction.user.id, f"{current}%"),
            )
            notes = await cursor.fetchall()
            return [app_commands.Choice(name=n[0], value=n[0]) for n in notes]
    except Exception:
        return []

@note_group.command(name="list", description="List all of your saved notes")
@app_commands.allowed_contexts(discord.app_commands.AppCommandContext.guild, discord.app_commands.AppCommandContext.private_channel, discord.app_commands.AppCommandContext.dm_channel)
async def note_list(interaction: discord.Interaction):
    cog = await _get_notes_cog(interaction)
    if not cog:
        await interaction.response.send_message("Notes system unavailable.", ephemeral=True)
        return
    if not await cog.check_vote_access(interaction.user.id):
        embed = discord.Embed(
            title="Vote to Use This Feature!",
            description=f"This command requires voting! To access this feature, please vote for Dopamine here: [top.gg](https://top.gg/bot/{interaction.client.user.id})",
            color=0xffaa00
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    try:
        async with cog.db_connection() as db:
            cursor = await db.execute(
                "SELECT note_name FROM user_notes WHERE user_id = ? ORDER BY note_name",
                (interaction.user.id,),
            )
            notes = await cursor.fetchall()
        embed = discord.Embed(title="Your Notes", color=discord.Color.blurple())
        embed.set_footer(text="To fetch a note, use /note fetch")
        if notes:
            embed.description = "\n".join(f"- {n[0]}" for n in notes)
        else:
            embed.description = "No notes found. Use `/note create` to create one!"
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        embed = discord.Embed(
            title="Error: Failed to List Notes",
            description=f"An error occurred while listing your notes: {str(e)}",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

@note_group.command(name="delete", description="Delete a note by name")
@app_commands.autocomplete(name=_delete_names_autocomplete)
@app_commands.allowed_contexts(discord.app_commands.AppCommandContext.guild, discord.app_commands.AppCommandContext.private_channel, discord.app_commands.AppCommandContext.dm_channel)
async def note_delete(interaction: discord.Interaction, name: str):
    cog = await _get_notes_cog(interaction)
    if not cog:
        await interaction.response.send_message("Notes system unavailable.", ephemeral=True)
        return
    if not await cog.check_vote_access(interaction.user.id):
        embed = discord.Embed(
            title="Vote to Use This Feature!",
            description=f"This command requires voting! To access this feature, please vote for Dopamine here: [top.gg](https://top.gg/bot/{interaction.client.user.id})",
            color=0xffaa00
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    try:
        async with cog.db_connection() as db:
            cursor = await db.execute(
                "SELECT note_content FROM user_notes WHERE user_id = ? AND note_name = ?",
                (interaction.user.id, name),
            )
            existing_note = await cursor.fetchone()
        if existing_note:
            async with cog.db_connection() as db_write:
                await db_write.execute(
                    "DELETE FROM user_notes WHERE user_id = ? AND note_name = ?",
                    (interaction.user.id, name),
                )
                await db_write.commit()
            embed = discord.Embed(
                title="Note Deleted Successfully",
                description=f"Note '{name}' has been deleted.",
                color=discord.Color.green()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            embed = discord.Embed(
                title="Error: Note Not Found",
                description=f"No note found with the name '{name}'.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        embed = discord.Embed(
            title="Error: Failed to Delete Note",
            description=f"An error occurred while deleting your note: {str(e)}",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

async def setup(bot):
    bot.tree.add_command(note_group, override=True)

    await bot.add_cog(Notes(bot))
