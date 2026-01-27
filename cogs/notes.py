import discord
from discord.ext import commands, tasks
from discord import app_commands
from discord.ui import Modal, TextInput
import aiosqlite
import asyncio
from typing import Optional, Dict, List
from contextlib import asynccontextmanager
from config import NOTEDB_PATH

note_group = app_commands.Group(name="note", description="Note management commands")


class Notes(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.notes_cache: Dict[int, Dict[str, str]] = {}
        self.db_pool: Optional[asyncio.Queue[aiosqlite.Connection]] = None

    async def cog_load(self):
        await self.init_pools()
        await self.init_db()
        await self.populate_caches()

    async def cog_unload(self):
        try:
            self.bot.tree.remove_command(note_group.name, type=discord.app_commands.AppCommandType.chat_input)
        except Exception:
            pass

        if self.db_pool is not None:
            while not self.db_pool.empty():
                conn = await self.db_pool.get()
                try:
                    await conn.close()
                except Exception:
                    pass
            self.db_pool = None

    async def init_pools(self, pool_size: int = 5):
        if self.db_pool is None:
            self.db_pool = asyncio.Queue(maxsize=pool_size)
            for _ in range(pool_size):
                conn = await aiosqlite.connect(
                    NOTEDB_PATH,
                    timeout=5,
                    isolation_level=None,
                )
                await conn.execute("PRAGMA busy_timeout=5000")
                await conn.execute("PRAGMA journal_mode=WAL")
                await conn.execute("PRAGMA synchronous=NORMAL")
                await conn.execute("PRAGMA cache_size=-65536")
                await conn.execute("PRAGMA foreign_keys=ON")
                await conn.commit()
                await self.db_pool.put(conn)

    @asynccontextmanager
    async def acquire_db(self):
        if self.db_pool is None:
            await self.init_pools()

        conn = await self.db_pool.get()
        try:
            yield conn
        finally:
            await self.db_pool.put(conn)

    async def init_db(self):
        async with self.acquire_db() as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS user_notes
                (
                    user_id INTEGER,
                    note_name TEXT,
                    note_content TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, note_name)
                    )
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_user_notes_user_id
                    ON user_notes (user_id)
                """
            )
            await db.commit()

    async def populate_caches(self):
        self.notes_cache.clear()
        async with self.acquire_db() as db:
            async with db.execute("SELECT user_id, note_name, note_content FROM user_notes") as cursor:
                rows = await cursor.fetchall()
                for user_id, name, content in rows:
                    if user_id not in self.notes_cache:
                        self.notes_cache[user_id] = {}
                    self.notes_cache[user_id][name] = content

    async def check_vote_access(self, user_id: int) -> bool:
        voter_cog = self.bot.get_cog('TopGGVoter')
        return await voter_cog.check_vote_access(user_id) if voter_cog else True

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
            user_id = interaction.user.id

            try:
                async with self.cog.acquire_db() as db:
                    await db.execute(
                        """
                        INSERT INTO user_notes (user_id, note_name, note_content)
                        VALUES (?, ?, ?) ON CONFLICT(user_id, note_name) DO
                        UPDATE SET
                            note_content = excluded.note_content,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        (user_id, name, content),
                    )
                    await db.commit()

                # Update Cache
                if user_id not in self.cog.notes_cache:
                    self.cog.notes_cache[user_id] = {}
                self.cog.notes_cache[user_id][name] = content

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
    return interaction.client.get_cog('Notes')


async def _get_names_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    cog = await _get_notes_cog(interaction)
    if not cog:
        return []

    user_notes = cog.notes_cache.get(interaction.user.id, {})
    choices = [
        app_commands.Choice(name=name, value=name)
        for name in user_notes.keys()
        if current.lower() in name.lower()
    ]
    return choices[:25]


@note_group.command(name="create", description="Open the UI to create a note")
@app_commands.allowed_contexts(guild=True, dms=True, private_channels=True)
async def note_create(interaction: discord.Interaction):
    cog = await _get_notes_cog(interaction)
    if not cog:
        return await interaction.response.send_message("Notes system unavailable.", ephemeral=True)

    if not await cog.check_vote_access(interaction.user.id):
        embed = discord.Embed(
            title="Vote to Use This Feature!",
            description=f"This command requires voting! To access this feature, please vote for Dopamine here: [top.gg](https://top.gg/bot/{interaction.client.user.id})",
            color=0xffaa00
        )
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    await interaction.response.send_modal(cog.NoteModal(cog))


@note_group.command(name="get", description="Retrieve a note by name")
@app_commands.autocomplete(name=_get_names_autocomplete)
@app_commands.allowed_contexts(guild=True, dms=True, private_channels=True)
async def note_fetch(interaction: discord.Interaction, name: str):
    cog = await _get_notes_cog(interaction)
    if not cog:
        return await interaction.response.send_message("Notes system unavailable.", ephemeral=True)

    if not await cog.check_vote_access(interaction.user.id):
        return await interaction.response.send_message("Please vote to use this feature.", ephemeral=True)

    user_id = interaction.user.id
    content = cog.notes_cache.get(user_id, {}).get(name)

    if content:
        await interaction.response.send_message(content, ephemeral=True)
    else:
        embed = discord.Embed(
            title="Error: Note Not Found",
            description=f"No note found with the name '{name}'.",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


@note_group.command(name="list", description="List all of your saved notes")
@app_commands.allowed_contexts(guild=True, dms=True, private_channels=True)
async def note_list(interaction: discord.Interaction):
    cog = await _get_notes_cog(interaction)
    if not cog:
        return await interaction.response.send_message("Notes system unavailable.", ephemeral=True)

    if not await cog.check_vote_access(interaction.user.id):
        return await interaction.response.send_message("Please vote to use this feature.", ephemeral=True)

    user_notes = cog.notes_cache.get(interaction.user.id, {})

    embed = discord.Embed(title="Your Notes", color=discord.Color.blurple())
    embed.set_footer(text="To fetch a note, use /note fetch")

    if user_notes:
        embed.description = "\n".join(f"- {name}" for name in sorted(user_notes.keys()))
    else:
        embed.description = "No notes found. Use `/note create` to create one!"

    await interaction.response.send_message(embed=embed, ephemeral=True)


@note_group.command(name="delete", description="Delete a note by name")
@app_commands.autocomplete(name=_get_names_autocomplete)
@app_commands.allowed_contexts(guild=True, dms=True, private_channels=True)
async def note_delete(interaction: discord.Interaction, name: str):
    cog = await _get_notes_cog(interaction)
    if not cog:
        return await interaction.response.send_message("Notes system unavailable.", ephemeral=True)

    user_id = interaction.user.id
    user_notes = cog.notes_cache.get(user_id, {})

    if name in user_notes:
        try:
            async with cog.acquire_db() as db:
                await db.execute(
                    "DELETE FROM user_notes WHERE user_id = ? AND note_name = ?",
                    (user_id, name),
                )
                await db.commit()

            del cog.notes_cache[user_id][name]

            embed = discord.Embed(
                title="Note Deleted Successfully",
                description=f"Note '{name}' has been deleted.",
                color=discord.Color.green()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Error deleting note: {e}", ephemeral=True)
    else:
        embed = discord.Embed(
            title="Error: Note Not Found",
            description=f"No note found with the name '{name}'.",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    bot.tree.add_command(note_group, override=True)
    await bot.add_cog(Notes(bot))