import asyncio
import time
from dataclasses import dataclass
from typing import Optional, Dict, Tuple, List

import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiosqlite

from config import STICKYDB_PATH
from utils.checks import slash_mod_check


POOL_SIZE = 6
PANEL_NAMES_TTL_SECONDS = 600


class _PooledConnection:
    """Async context manager returned by the connection pool."""

    def __init__(self, pool: "AioSqlitePool"):
        self._pool = pool
        self._conn: Optional[aiosqlite.Connection] = None

    async def __aenter__(self) -> aiosqlite.Connection:
        self._conn = await self._pool._acquire()
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        await self._pool.release(self._conn)
        self._conn = None


class AioSqlitePool:
    """Lightweight connection pool for aiosqlite."""

    def __init__(self, path: str, size: int = POOL_SIZE):
        self._path = path
        self._size = max(2, size)
        self._pool: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue()
        self._connections: list[aiosqlite.Connection] = []
        self._closed = False

    async def init(self):
        for _ in range(self._size):
            conn = await aiosqlite.connect(self._path, timeout=5.0)
            await self._configure_connection(conn)
            await self._pool.put(conn)
            self._connections.append(conn)

    async def _configure_connection(self, conn: aiosqlite.Connection):
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA wal_autocheckpoint=1000")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA cache_size=-32000")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA optimize")
        await conn.commit()

    async def _acquire(self) -> aiosqlite.Connection:
        if self._closed:
            raise RuntimeError("Attempt to use closed connection pool")
        return await self._pool.get()

    async def release(self, conn: Optional[aiosqlite.Connection]):
        if conn is None:
            return
        if self._closed:
            await conn.close()
            return
        await self._pool.put(conn)

    def acquire(self) -> _PooledConnection:
        return _PooledConnection(self)

    async def close(self):
        self._closed = True
        while not self._pool.empty():
            self._pool.get_nowait()
        while self._connections:
            conn = self._connections.pop()
            await conn.close()


@dataclass
class ActivePanel:
    guild_id: int
    panel_id: int
    last_message_id: int
    image_only_mode: int
    member_whitelist_enabled: int
    member_whitelist_id: Optional[int]


class StickyMessages(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._db_pool: Optional[AioSqlitePool] = None
        self._active_panels: Dict[int, ActivePanel] = {}
        self._active_cache_lock = asyncio.Lock()
        self._panel_names_cache: Dict[int, Tuple[float, List[str]]] = {}
        self._panel_cache_lock = asyncio.Lock()

    async def cog_load(self):
        """Initialize the sticky messages database when cog is loaded"""
        await self._create_db_pool()
        await self.init_sticky_db()
        await self._prime_active_cache()
        if not self.sticky_monitor.is_running():
            self.sticky_monitor.start()

    async def cog_unload(self):
        """Close database connection when cog is unloaded"""
        if self.sticky_monitor.is_running():
            self.sticky_monitor.cancel()
        if self._db_pool:
            await self._db_pool.close()

    async def _create_db_pool(self):
        if self._db_pool:
            return
        pool = AioSqlitePool(STICKYDB_PATH, size=POOL_SIZE)
        await pool.init()
        self._db_pool = pool

    def db_connection(self):
        if not self._db_pool:
            raise RuntimeError("Database pool not initialized")
        return self._db_pool.acquire()

    async def _prime_active_cache(self):
        async with self.db_connection() as db:
            cursor = await db.execute('''
                SELECT guild_id, panel_id, channel_id, last_message_id, image_only_mode,
                       member_whitelist_enabled, member_whitelist_id
                FROM sticky_panels
                WHERE channel_id IS NOT NULL AND last_message_id IS NOT NULL
            ''')
            rows = await cursor.fetchall()
            await cursor.close()

        async with self._active_cache_lock:
            self._active_panels.clear()
            for guild_id, panel_id, channel_id, last_message_id, image_only_mode, member_whitelist_enabled, member_whitelist_id in rows:
                self._active_panels[channel_id] = ActivePanel(
                    guild_id=guild_id,
                    panel_id=panel_id,
                    last_message_id=last_message_id,
                    image_only_mode=image_only_mode,
                    member_whitelist_enabled=member_whitelist_enabled,
                    member_whitelist_id=member_whitelist_id
                )

    async def _set_active_panel(self, channel_id: int, panel: ActivePanel):
        async with self._active_cache_lock:
            self._active_panels[channel_id] = panel

    async def _update_active_message_id(self, channel_id: int, message_id: int):
        async with self._active_cache_lock:
            panel = self._active_panels.get(channel_id)
            if panel:
                panel.last_message_id = message_id

    async def _update_active_panel_membership(self, channel_id: int, image_only: Optional[int] = None,
                                              member_whitelist_enabled: Optional[int] = None,
                                              member_whitelist_id: Optional[int] = None):
        async with self._active_cache_lock:
            panel = self._active_panels.get(channel_id)
            if not panel:
                return
            if image_only is not None:
                panel.image_only_mode = image_only
            if member_whitelist_enabled is not None:
                panel.member_whitelist_enabled = member_whitelist_enabled
            if member_whitelist_id is not None or member_whitelist_enabled == 0:
                panel.member_whitelist_id = member_whitelist_id

    async def _remove_active_panel(self, channel_id: int):
        async with self._active_cache_lock:
            self._active_panels.pop(channel_id, None)

    async def _get_active_panel(self, channel_id: int) -> Optional[ActivePanel]:
        async with self._active_cache_lock:
            panel = self._active_panels.get(channel_id)
            if panel:
                return ActivePanel(**panel.__dict__)
            return None

    def _invalidate_panel_names_cache(self, guild_id: int):
        self._panel_names_cache.pop(guild_id, None)

    async def _get_panel_names_cached(self, guild_id: int) -> List[str]:
        cached = self._panel_names_cache.get(guild_id)
        now = time.monotonic()
        if cached and cached[0] > now:
            return cached[1]

        async with self._panel_cache_lock:
            cached = self._panel_names_cache.get(guild_id)
            if cached and cached[0] > now:
                return cached[1]
            async with self.db_connection() as db:
                cursor = await db.execute('''
                    SELECT name FROM sticky_panels
                    WHERE guild_id = ?
                    ORDER BY name
                ''', (guild_id,))
                rows = await cursor.fetchall()
                await cursor.close()
            names = [row[0] for row in rows]
            self._panel_names_cache[guild_id] = (now + PANEL_NAMES_TTL_SECONDS, names)
            return names

    async def init_sticky_db(self):
        """Initialize the sticky messages database"""
        async with self.db_connection() as db:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS sticky_panels (
                    guild_id INTEGER,
                    panel_id INTEGER,
                    name TEXT,
                    embed_color TEXT,
                    title TEXT,
                    description TEXT,
                    message_content TEXT,
                    image_url TEXT,
                    footer TEXT,
                    channel_id INTEGER,
                    last_message_id INTEGER,
                    image_only_mode INTEGER DEFAULT 0,
                    member_whitelist_enabled INTEGER DEFAULT 0,
                    member_whitelist_id INTEGER,
                    PRIMARY KEY (guild_id, panel_id)
                )
            ''')

            for statement in (
                'ALTER TABLE sticky_panels ADD COLUMN embed_color TEXT',
                'ALTER TABLE sticky_panels ADD COLUMN title TEXT',
                'ALTER TABLE sticky_panels ADD COLUMN description TEXT',
                'ALTER TABLE sticky_panels ADD COLUMN footer TEXT',
                'ALTER TABLE sticky_panels ADD COLUMN member_whitelist_enabled INTEGER DEFAULT 0',
                'ALTER TABLE sticky_panels ADD COLUMN member_whitelist_id INTEGER',
            ):
                try:
                    await db.execute(statement)
                except aiosqlite.OperationalError:
                    pass

            try:
                await db.execute('ALTER TABLE sticky_panels DROP COLUMN title_url')
            except aiosqlite.OperationalError:
                pass

            await db.execute('''
                CREATE INDEX IF NOT EXISTS idx_sticky_panels_active 
                ON sticky_panels(guild_id, channel_id, last_message_id)
            ''')
            await db.execute('''
                CREATE INDEX IF NOT EXISTS idx_sticky_monitor
                ON sticky_panels(last_message_id, channel_id, guild_id)
            ''')

            await db.commit()

    async def get_next_panel_id(self, guild_id: int) -> int:
        """Find the lowest unused panel ID for a guild"""
        async with self.db_connection() as db:
            cursor = await db.execute('''
                SELECT MAX(panel_id) FROM sticky_panels 
                WHERE guild_id = ?
            ''', (guild_id,))
            row = await cursor.fetchone()
            await cursor.close()
            max_id = row[0] if row and row[0] is not None else 0
            return max_id + 1

    async def get_panel_names(self, guild_id: int) -> list[str]:
        """Get all panel names for a guild"""
        return await self._get_panel_names_cached(guild_id)

    async def get_panel_by_name(self, guild_id: int, name: str):
        """Get panel data by name"""
        async with self.db_connection() as db:
            cursor = await db.execute('''
                SELECT panel_id, name, embed_color, title, description, message_content, 
                       image_url, footer, channel_id, last_message_id, image_only_mode,
                       member_whitelist_enabled, member_whitelist_id
                FROM sticky_panels 
                WHERE guild_id = ? AND name = ?
            ''', (guild_id, name))
            result = await cursor.fetchone()
            await cursor.close()
            return result

    async def get_panel_by_id(self, guild_id: int, panel_id: int):
        async with self.db_connection() as db:
            cursor = await db.execute('''
                SELECT panel_id, name, embed_color, title, description, message_content, 
                       image_url, footer, channel_id, last_message_id, image_only_mode,
                       member_whitelist_enabled, member_whitelist_id
                FROM sticky_panels 
                WHERE guild_id = ? AND panel_id = ?
            ''', (guild_id, panel_id))
            result = await cursor.fetchone()
            await cursor.close()
            return result

    @staticmethod
    def parse_color(color_str: str) -> Optional[discord.Color]:
        """Parse a color string to discord.Color"""
        if not color_str:
            return None

        color_str = color_str.strip()

        if color_str.startswith('#'):
            color_str = color_str[1:]

        try:
            color_int = int(color_str, 16)
            return discord.Color(color_int)
        except ValueError:
            color_lower = color_str.lower()
            color_map = {
                'red': discord.Color.red(),
                'blue': discord.Color.blue(),
                'green': discord.Color.green(),
                'yellow': discord.Color.gold(),
                'orange': discord.Color.orange(),
                'purple': discord.Color.purple(),
                'pink': discord.Color.magenta(),
                'teal': discord.Color.teal(),
                'cyan': discord.Color.blue(),
                'white': discord.Color.from_rgb(255, 255, 255),
                'black': discord.Color.from_rgb(0, 0, 0),
            }
            return color_map.get(color_lower, None)

    @staticmethod
    def build_panel_embed(embed_color: Optional[str], title: Optional[str], description: Optional[str],
                          message_content: Optional[str], image_url: Optional[str], footer: Optional[str]) -> discord.Embed:
        embed = discord.Embed()

        if embed_color:
            color = StickyMessages.parse_color(embed_color)
            if color:
                embed.color = color

        if title:
            embed.title = title
        if description:
            embed.description = description
        if message_content:
            if embed.description:
                embed.add_field(name="Message", value=message_content, inline=False)
            else:
                embed.description = message_content
        if image_url:
            embed.set_image(url=image_url)
        if footer:
            embed.set_footer(text=footer)

        return embed

    sticky_group = app_commands.Group(name="sticky", description="Sticky message commands")

    panel_group = app_commands.Group(name="panel", description="Sticky panel commands", parent=sticky_group)

    @panel_group.command(name="setup", description="Create a new sticky panel")
    @app_commands.check(slash_mod_check)
    async def panel_setup(
            self,
            interaction: discord.Interaction,
            name: str,
            channel: discord.TextChannel
    ):
        """Create a new sticky panel"""

        async with self.db_connection() as db:
            cursor = await db.execute('''
                SELECT panel_id FROM sticky_panels 
                WHERE guild_id = ? AND name = ?
            ''', (interaction.guild.id, name))
            exists = await cursor.fetchone()
            await cursor.close()

        if exists:
            embed = discord.Embed(
                title="Error: Panel Name Already Exists",
                description=f"A sticky panel with the name **{name}** already exists. Please choose a different name.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        panel_id = await self.get_next_panel_id(interaction.guild.id)

        modal = PanelSetupModal(self.bot, interaction.guild.id, panel_id, name, channel.id)
        await interaction.response.send_modal(modal)

    @panel_group.command(name="start", description="Start a sticky panel")
    @app_commands.check(slash_mod_check)
    async def panel_start(
            self,
            interaction: discord.Interaction,
            name: str
    ):
        """Start a sticky panel"""

        panel_data = await self.get_panel_by_name(interaction.guild.id, name)

        if not panel_data:
            embed = discord.Embed(
                title="Error: Panel Not Found",
                description=f"No sticky panel found with the name **{name}**.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        panel_id, name, embed_color, title, description, message_content, image_url, footer, channel_id, last_message_id, image_only_mode, member_whitelist_enabled, member_whitelist_id = panel_data

        if not channel_id:
            embed = discord.Embed(
                title="Error: No Channel Set",
                description=f"Panel **{name}** does not have a channel assigned. Please set it up first.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if last_message_id:
            embed = discord.Embed(
                title="Error: Panel Already Active",
                description=f"Panel **{name}** is already active in the channel.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        channel = self.bot.get_channel(channel_id)
        if not channel:
            embed = discord.Embed(
                title="Error: Channel Not Found",
                description="The channel for this panel no longer exists.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        try:
            embed = self.build_panel_embed(embed_color, title, description, message_content, image_url, footer)
            message = await channel.send(embed=embed)

            async with self.db_connection() as db:
                await db.execute('''
                    UPDATE sticky_panels 
                    SET last_message_id = ?
                    WHERE guild_id = ? AND panel_id = ?
                ''', (message.id, interaction.guild.id, panel_id))

                await db.commit()

            await self._set_active_panel(
                channel_id,
                ActivePanel(
                    guild_id=interaction.guild.id,
                    panel_id=panel_id,
                    last_message_id=message.id,
                    image_only_mode=image_only_mode,
                    member_whitelist_enabled=member_whitelist_enabled,
                    member_whitelist_id=member_whitelist_id
                )
            )

            success_embed = discord.Embed(
                title="Sticky Panel Started",
                description=f"Panel **{name}** is now active in {channel.mention}!",
                color=discord.Color.green()
            )
            await interaction.response.send_message(embed=success_embed, ephemeral=True)

        except Exception as e:
            error_embed = discord.Embed(
                title="Error: Failed to Start",
                description=f"Failed to start sticky panel. Error: {str(e)}",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=error_embed, ephemeral=True)

    @panel_start.autocomplete('name')
    async def panel_start_autocomplete(
            self,
            interaction: discord.Interaction,
            current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for panel start command"""
        names = await self.get_panel_names(interaction.guild.id)
        return [
                   app_commands.Choice(name=name, value=name)
                   for name in names
                   if current.lower() in name.lower()
               ][:25]

    @panel_group.command(name="stop", description="Stop a sticky panel")
    @app_commands.check(slash_mod_check)
    async def panel_stop(
            self,
            interaction: discord.Interaction,
            name: str
    ):
        """Stop a sticky panel"""

        panel_data = await self.get_panel_by_name(interaction.guild.id, name)

        if not panel_data:
            embed = discord.Embed(
                title="Error: Panel Not Found",
                description=f"No sticky panel found with the name **{name}**.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        panel_id, name, embed_color, title, description, message_content, image_url, footer, channel_id, last_message_id, image_only_mode, member_whitelist_enabled, member_whitelist_id = panel_data

        if not last_message_id:
            embed = discord.Embed(
                title="Error: Panel Not Active",
                description=f"Panel **{name}** is not currently active.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if channel_id:
            try:
                channel = self.bot.get_channel(channel_id)
                if channel:
                    message = await channel.fetch_message(last_message_id)
                    await message.delete()
            except (discord.NotFound, discord.Forbidden):
                pass

        async with self.db_connection() as db:
            await db.execute('''
                UPDATE sticky_panels 
                SET last_message_id = NULL
                WHERE guild_id = ? AND panel_id = ?
            ''', (interaction.guild.id, panel_id))
            await db.commit()

        if channel_id:
            await self._remove_active_panel(channel_id)
        self._invalidate_panel_names_cache(interaction.guild.id)

        success_embed = discord.Embed(
            title="Sticky Panel Stopped",
            description=f"Panel **{name}** has been stopped and is no longer active.",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=success_embed, ephemeral=True)

    @panel_stop.autocomplete('name')
    async def panel_stop_autocomplete(
            self,
            interaction: discord.Interaction,
            current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for panel stop command"""
        names = await self.get_panel_names(interaction.guild.id)
        return [
                   app_commands.Choice(name=name, value=name)
                   for name in names
                   if current.lower() in name.lower()
               ][:25]

    @panel_group.command(name="modes", description="Configure sticky panel modes")
    @app_commands.check(slash_mod_check)
    async def panel_modes(
            self,
            interaction: discord.Interaction,
            name: str,
            image_only: Optional[str] = None,
            member_whitelist: Optional[str] = None,
            member: Optional[discord.Member] = None
    ):
        """Configure sticky panel modes"""

        if image_only and image_only.lower() not in ['on', 'off']:
            embed = discord.Embed(
                title="Error: Invalid Image Only Value",
                description="`image_only` must be either **On** or **Off**.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if member_whitelist and member_whitelist.lower() not in ['on', 'off']:
            embed = discord.Embed(
                title="Error: Invalid Member Whitelist Value",
                description="`member_whitelist` must be either **On** or **Off**.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if member_whitelist and member_whitelist.lower() == 'on' and not member:
            embed = discord.Embed(
                title="Error: Member Required",
                description="You must specify a member when enabling member whitelist.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        panel_data = await self.get_panel_by_name(interaction.guild.id, name)

        if not panel_data:
            embed = discord.Embed(
                title="Error: Panel Not Found",
                description=f"No sticky panel found with the name **{name}**.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        panel_id, panel_name, embed_color, title, description, message_content, image_url, footer, channel_id, last_message_id, current_image_only, current_member_whitelist_enabled, current_member_whitelist_id = panel_data

        update_fields = []
        update_values = []
        needs_null_update = False
        new_image_only = current_image_only
        new_member_whitelist_enabled = current_member_whitelist_enabled
        new_member_whitelist_id = current_member_whitelist_id

        if image_only is not None:
            new_image_only = 1 if image_only.lower() == 'on' else 0
            update_fields.append('image_only_mode = ?')
            update_values.append(new_image_only)

        if member_whitelist is not None:
            new_member_whitelist_enabled = 1 if member_whitelist.lower() == 'on' else 0
            update_fields.append('member_whitelist_enabled = ?')
            update_values.append(new_member_whitelist_enabled)

            if new_member_whitelist_enabled and member:
                new_member_whitelist_id = member.id
                update_fields.append('member_whitelist_id = ?')
                update_values.append(member.id)
            elif not new_member_whitelist_enabled:
                new_member_whitelist_id = None
                update_fields.append('member_whitelist_id = NULL')

        if update_fields:
            if 'member_whitelist_id = NULL' in update_fields:
                update_fields.remove('member_whitelist_id = NULL')
                needs_null_update = True

            async with self.db_connection() as db:
                if update_fields:
                    update_values.extend([interaction.guild.id, panel_id])
                    query = f'UPDATE sticky_panels SET {", ".join(update_fields)} WHERE guild_id = ? AND panel_id = ?'
                    await db.execute(query, update_values)

                if needs_null_update:
                    await db.execute('''
                        UPDATE sticky_panels 
                        SET member_whitelist_id = NULL
                        WHERE guild_id = ? AND panel_id = ?
                    ''', (interaction.guild.id, panel_id))

                await db.commit()

            if channel_id and last_message_id:
                await self._update_active_panel_membership(
                    channel_id,
                    image_only=new_image_only if image_only is not None else None,
                    member_whitelist_enabled=new_member_whitelist_enabled if member_whitelist is not None else None,
                    member_whitelist_id=new_member_whitelist_id if member_whitelist is not None else None
                )

            description_parts = []
            if image_only is not None:
                description_parts.append(
                    f"**Image Only Mode:** {'Enabled' if image_only.lower() == 'on' else 'Disabled'}")
            if member_whitelist is not None:
                status = 'Enabled' if member_whitelist.lower() == 'on' else 'Disabled'
                if member_whitelist.lower() == 'on' and member:
                    status += f" ({member.mention})"
                description_parts.append(f"**Member Whitelist:** {status}")

            success_embed = discord.Embed(
                title="Panel Modes Updated",
                description=f"Panel **{panel_name}** modes have been updated:\n\n" + "\n".join(description_parts),
                color=discord.Color.green()
            )
            await interaction.response.send_message(embed=success_embed, ephemeral=True)
        else:
            embed = discord.Embed(
                title="No Changes",
                description="No modes were specified to update.",
                color=discord.Color.orange()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @panel_modes.autocomplete('name')
    async def panel_modes_autocomplete(
            self,
            interaction: discord.Interaction,
            current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for panel modes command"""
        names = await self.get_panel_names(interaction.guild.id)
        return [
                   app_commands.Choice(name=name, value=name)
                   for name in names
                   if current.lower() in name.lower()
               ][:25]

    @panel_modes.autocomplete('image_only')
    async def panel_modes_image_only_autocomplete(
            self,
            interaction: discord.Interaction,
            current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for image_only parameter"""
        options = ['On', 'Off']
        return [
            app_commands.Choice(name=option, value=option)
            for option in options
            if current.lower() in option.lower()
        ]

    @panel_modes.autocomplete('member_whitelist')
    async def panel_modes_member_whitelist_autocomplete(
            self,
            interaction: discord.Interaction,
            current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for member_whitelist parameter"""
        options = ['On', 'Off']
        return [
            app_commands.Choice(name=option, value=option)
            for option in options
            if current.lower() in option.lower()
        ]

    @panel_group.command(name="delete", description="Delete a sticky panel")
    @app_commands.check(slash_mod_check)
    async def panel_delete(
            self,
            interaction: discord.Interaction,
            name: str
    ):
        """Delete a sticky panel"""

        panel_data = await self.get_panel_by_name(interaction.guild.id, name)

        if not panel_data:
            embed = discord.Embed(
                title="Error: Panel Not Found",
                description=f"No sticky panel found with the name **{name}**.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        panel_id, panel_name, embed_color, title, description, message_content, image_url, footer, channel_id, last_message_id, image_only_mode, member_whitelist_enabled, member_whitelist_id = panel_data

        if channel_id and last_message_id:
            try:
                channel = self.bot.get_channel(channel_id)
                if channel:
                    message = await channel.fetch_message(last_message_id)
                    await message.delete()
            except (discord.NotFound, discord.Forbidden):
                pass

        async with self.db_connection() as db:
            await db.execute('''
                DELETE FROM sticky_panels 
                WHERE guild_id = ? AND panel_id = ?
            ''', (interaction.guild.id, panel_id))

            await db.commit()

        if channel_id:
            await self._remove_active_panel(channel_id)

        embed = discord.Embed(
            title="Sticky Panel Deleted Successfully",
            description=f"Successfully deleted sticky panel **{panel_name}**.",
            color=discord.Color.green()
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @panel_delete.autocomplete('name')
    async def panel_delete_autocomplete(
            self,
            interaction: discord.Interaction,
            current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for panel delete command"""
        names = await self.get_panel_names(interaction.guild.id)
        return [
                   app_commands.Choice(name=name, value=name)
                   for name in names
                   if current.lower() in name.lower()
               ][:25]

    @sticky_group.command(name="panels", description="View all sticky panels in this server")
    @app_commands.check(slash_mod_check)
    async def panels(self, interaction: discord.Interaction):
        """Display all sticky panels for the guild"""

        async with self.db_connection() as db:
            cursor = await db.execute('''
                SELECT panel_id, name, embed_color, title, description, message_content, image_url, footer,
                       channel_id, last_message_id, image_only_mode, member_whitelist_enabled, member_whitelist_id
                FROM sticky_panels 
                WHERE guild_id = ?
                ORDER BY panel_id
            ''', (interaction.guild.id,))

            panels = await cursor.fetchall()
            await cursor.close()

        if not panels:
            embed = discord.Embed(
                title="Your Sticky Panels",
                description="No sticky panels found, use `/sticky panel setup` to create one!",
                color=discord.Color(0x337fd5)
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(
            title="Your Sticky Panels",
            color=discord.Color(0x337fd5)
        )

        description = ""
        for panel_id, name, embed_color, title, description_text, message_content, image_url, footer, channel_id, last_message_id, image_only_mode, member_whitelist_enabled, member_whitelist_id in panels:
            channel = self.bot.get_channel(channel_id) if channel_id else None
            channel_name = channel.name if channel else "Not assigned"
            member = self.bot.get_user(member_whitelist_id) if member_whitelist_id else None

            description += f"## {panel_id}. {name}\n"
            if embed_color:
                description += f"* **Color:** {embed_color}\n"
            if title:
                description += f"* **Title:** {title}\n"
            if description_text:
                description += f"* **Description:** {description_text[:50]}{'...' if len(description_text) > 50 else ''}\n"
            if message_content:
                description += f"* **Message:** {message_content[:50]}{'...' if len(message_content) > 50 else ''}\n"
            description += f"* **Image:** {'Provided' if image_url else 'None'}\n"
            description += f"* **Channel:** {channel.mention if channel else 'Not assigned'}\n"
            description += f"* **Status:** {'Active' if last_message_id else 'Inactive'}\n"
            description += f"* **Image-Only Mode:** {'Enabled' if image_only_mode else 'Disabled'}\n"
            if member_whitelist_enabled:
                description += f"* **Member Whitelist:** Enabled ({member.mention if member else 'Unknown user'})\n"
            else:
                description += f"* **Member Whitelist:** Disabled\n"
            description += f"* **Panel ID: {panel_id}**\n\n"

        embed.description = description

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tasks.loop(seconds=120)
    async def sticky_monitor(self):
        """Background task to monitor and update sticky messages"""
        try:
            async with self._active_cache_lock:
                active_snapshot = list(self._active_panels.items())

            for channel_id, panel in active_snapshot:
                guild = self.bot.get_guild(panel.guild_id)
                if not guild:
                    continue

                channel = guild.get_channel(channel_id)
                if not channel:
                    await self._remove_active_panel(channel_id)
                    continue

                try:
                    await channel.fetch_message(panel.last_message_id)
                except discord.NotFound:
                    panel_row = await self.get_panel_by_id(panel.guild_id, panel.panel_id)
                    if not panel_row:
                        await self._remove_active_panel(channel_id)
                        continue
                    await self.update_sticky_message(panel_row, channel)
                except discord.Forbidden:
                    await self._remove_active_panel(channel_id)
                    continue
                except Exception as e:
                    print(f"Error monitoring sticky panel {panel.panel_id} in guild {panel.guild_id}: {e}")
                    continue

        except Exception as e:
            print(f"Error in sticky_monitor task: {e}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """React to new messages to maintain sticky behavior without polling."""
        if not message.guild or message.author.bot:
            return

        panel = await self._get_active_panel(message.channel.id)
        if not panel:
            return

        if message.id == panel.last_message_id:
            return

        if panel.member_whitelist_enabled and panel.member_whitelist_id:
            if message.author.id != panel.member_whitelist_id:
                return

        if panel.image_only_mode:
            has_images = bool(message.attachments)
            if not has_images and message.embeds:
                has_images = any(getattr(embed, "image", None) for embed in message.embeds)
            if not has_images:
                return

        panel_row = await self.get_panel_by_id(panel.guild_id, panel.panel_id)
        if not panel_row:
            await self._remove_active_panel(message.channel.id)
            return

        await self.update_sticky_message(panel_row, message.channel)

    async def update_sticky_message(self, panel_row, channel: discord.TextChannel):
        """Update a sticky message by deleting the old one and sending a new one."""
        try:
            (panel_id, _name, embed_color, title, description, message_content, image_url, footer,
             channel_id, last_message_id, image_only_mode, member_whitelist_enabled,
             member_whitelist_id) = panel_row

            if channel.id != channel_id:
                return

            if last_message_id:
                try:
                    old_message = await channel.fetch_message(last_message_id)
                    await old_message.delete()
                except discord.NotFound:
                    pass
                except discord.Forbidden:
                    return

            embed = self.build_panel_embed(embed_color, title, description, message_content, image_url, footer)
            new_message = await channel.send(embed=embed)

            async with self.db_connection() as db:
                await db.execute('''
                    UPDATE sticky_panels 
                    SET last_message_id = ?
                    WHERE guild_id = ? AND panel_id = ?
                ''', (new_message.id, channel.guild.id, panel_id))
                await db.commit()

            await self._set_active_panel(
                channel.id,
                ActivePanel(
                    guild_id=channel.guild.id,
                    panel_id=panel_id,
                    last_message_id=new_message.id,
                    image_only_mode=image_only_mode,
                    member_whitelist_enabled=member_whitelist_enabled,
                    member_whitelist_id=member_whitelist_id
                )
            )

        except Exception as e:
            print(f"Error updating sticky message for panel {panel_row[0]}: {e}")


class PanelSetupModal(discord.ui.Modal):
    def __init__(self, bot, guild_id: int, panel_id: int, name: str, channel_id: int):
        super().__init__(title="Configure Sticky Panel")
        self.bot = bot
        self.guild_id = guild_id
        self.panel_id = panel_id
        self.name = name
        self.channel_id = channel_id

        self.color_input = discord.ui.TextInput(
            label="Embed Color",
            placeholder="Enter HEX code (#FF5733 or FF5733) or color name...",
            required=False,
            max_length=20
        )
        self.add_item(self.color_input)

        self.title_input = discord.ui.TextInput(
            label="Title",
            placeholder="Enter the embed title...",
            required=False,
            max_length=256
        )
        self.add_item(self.title_input)

        self.description_input = discord.ui.TextInput(
            label="Description",
            placeholder="Enter the embed description...",
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=4000
        )
        self.add_item(self.description_input)

        self.image_url_input = discord.ui.TextInput(
            label="Image URL",
            placeholder="Enter the image URL...",
            required=False,
            max_length=4000
        )
        self.add_item(self.image_url_input)

        self.footer_input = discord.ui.TextInput(
            label="Footer",
            placeholder="Enter the footer text...",
            required=False,
            max_length=2048
        )
        self.add_item(self.footer_input)

    async def on_submit(self, interaction: discord.Interaction):
        """Handle modal submission"""
        embed_color = self.color_input.value.strip() if self.color_input.value else None
        title = self.title_input.value.strip() if self.title_input.value else None
        description = self.description_input.value.strip() if self.description_input.value else None
        image_url = self.image_url_input.value.strip() if self.image_url_input.value else None
        footer = self.footer_input.value.strip() if self.footer_input.value else None

        if embed_color:
            parsed_color = StickyMessages.parse_color(embed_color)
            if not parsed_color:
                embed = discord.Embed(
                    title="Error: Invalid Color",
                    description="Please provide a valid hex color code (e.g., #FF5733 or FF5733) or a color name (red, blue, green, etc.).",
                    color=discord.Color.red()
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

        if image_url and not (image_url.startswith('http://') or image_url.startswith('https://')):
            embed = discord.Embed(
                title="Error: Invalid Image URL",
                description="Please provide a valid HTTP/HTTPS URL for the image.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if title and len(title) > 256:
            embed = discord.Embed(
                title="Error: Title Too Long",
                description="The title cannot exceed 256 characters.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if description and len(description) > 4096:
            embed = discord.Embed(
                title="Error: Description Too Long",
                description="The description cannot exceed 4096 characters.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if footer and len(footer) > 2048:
            embed = discord.Embed(
                title="Error: Footer Too Long",
                description="The footer cannot exceed 2048 characters.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        cog = self.bot.get_cog('StickyMessages')
        if cog:
            async with cog.db_connection() as db:
                await db.execute('''
                    INSERT INTO sticky_panels 
                    (guild_id, panel_id, name, embed_color, title, description, message_content, image_url, footer, channel_id, last_message_id, image_only_mode, member_whitelist_enabled, member_whitelist_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (self.guild_id, self.panel_id, self.name, embed_color, title, description, None, image_url, footer,
                      self.channel_id, None, 0, 0, None))

                await db.commit()

            cog._invalidate_panel_names_cache(self.guild_id)

        channel = self.bot.get_channel(self.channel_id)
        if channel:
            try:
                embed = StickyMessages.build_panel_embed(
                    embed_color,
                    title,
                    description,
                    None,
                    image_url,
                    footer
                )

                message = await channel.send(embed=embed)

                cog = self.bot.get_cog('StickyMessages')
                if cog:
                    async with cog.db_connection() as db:
                        await db.execute('''
                            UPDATE sticky_panels 
                            SET last_message_id = ?
                            WHERE guild_id = ? AND panel_id = ?
                        ''', (message.id, self.guild_id, self.panel_id))

                        await db.commit()

                    await cog._set_active_panel(
                        self.channel_id,
                        ActivePanel(
                            guild_id=self.guild_id,
                            panel_id=self.panel_id,
                            last_message_id=message.id,
                            image_only_mode=0,
                            member_whitelist_enabled=0,
                            member_whitelist_id=None
                        )
                    )

                success_embed = discord.Embed(
                    title="Sticky Panel Created and Started",
                    description=f"Sticky panel **{self.name}** has been created and is now active in {channel.mention}!",
                    color=discord.Color.green()
                )
                await interaction.response.send_message(embed=success_embed, ephemeral=True)

            except Exception as e:
                error_embed = discord.Embed(
                    title="Error: Failed to Start Panel",
                    description=f"Panel was created but failed to start. Error: {str(e)}",
                    color=discord.Color.red()
                )
                await interaction.response.send_message(embed=error_embed, ephemeral=True)
        else:
            error_embed = discord.Embed(
                title="Error: Channel Not Found",
                description="The target channel could not be found.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=error_embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(StickyMessages(bot))
