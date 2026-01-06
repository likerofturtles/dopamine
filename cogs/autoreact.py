import asyncio
import discord
from discord.ext import commands, tasks
from discord import app_commands
from discord.ui import View, Button, Modal, TextInput
import aiosqlite
import time
from typing import Optional, List, Dict, Tuple, Set
from datetime import datetime, timezone
import re
from functools import lru_cache
from config import ARDB_PATH
from utils.checks import slash_mod_check

EMOJI_REGEX = re.compile(
    r'(<a?:\w{2,32}:\d{15,25}>)'
    r'|([\U0001F1E6-\U0001F1FF]{2})'
    r'|([\U0001F300-\U0001FAFF]\uFE0F?)'
    r'|([\u2600-\u27BF]\uFE0F?)',
    flags=re.UNICODE
)


@lru_cache(maxsize=1024)
def _parse_emoji_input_cached(emoji_input: str) -> Tuple[str, ...]:
    """
    Cached implementation of emoji parsing to avoid repeated regex work
    for identical inputs. Returns a tuple for hashability.
    """
    if not emoji_input:
        return tuple()

    tokens: List[str] = []
    primary_parts = [p.strip() for p in re.split(r'[,\s]+', emoji_input) if p.strip()]

    for part in primary_parts:
        matches = [m.group(0) for m in EMOJI_REGEX.finditer(part)]
        if matches:
            tokens.extend(matches)
        else:
            tokens.append(part)

    return tuple(tokens)

class AutoReact(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._db_conn = None
        self._active_panel_cache: Dict[Tuple[int, int], Dict] = {}
        self._reaction_queue: "asyncio.Queue[Tuple[discord.Message, str]]" = asyncio.Queue()
        self._reaction_semaphore = asyncio.Semaphore(5)
        self._reaction_task: Optional[asyncio.Task] = None
        self._channel_semaphore = asyncio.Semaphore(5)

    async def cog_load(self):
        """Initialize the autoreact database when cog is loaded"""
        await self.init_ar_db()
        if not self.autoreact_monitor.is_running():
            self.autoreact_monitor.start()
        if not self._db_keepalive.is_running():
            self._db_keepalive.start()
        if self._reaction_task is None or self._reaction_task.done():
            self._reaction_task = asyncio.create_task(self.reaction_processor())

    async def cog_unload(self):
        """Close database connection when cog is unloaded"""
        if self._db_keepalive.is_running():
            self._db_keepalive.cancel()
        if self.autoreact_monitor.is_running():
            self.autoreact_monitor.cancel()
        if self._reaction_task is not None:
            self._reaction_task.cancel()
        if self._db_conn:
            await self._db_conn.close()

    async def get_db_connection(self):
        """Get a database connection with optimized settings for I/O performance"""
        if self._db_conn is None:
            max_retries = 5
            for attempt in range(max_retries):
                try:
                    self._db_conn = await aiosqlite.connect(ARDB_PATH, timeout=5.0)
                    await self._db_conn.execute("PRAGMA busy_timeout=5000")
                    await self._db_conn.execute("PRAGMA journal_mode=WAL")
                    await self._db_conn.execute("PRAGMA wal_autocheckpoint=1000")
                    await self._db_conn.execute("PRAGMA synchronous=NORMAL")
                    await self._db_conn.execute("PRAGMA cache_size=-64000")
                    await self._db_conn.execute("PRAGMA foreign_keys=ON")
                    await self._db_conn.execute("PRAGMA optimize")
                    await self._db_conn.commit()
                    break
                except Exception as e:
                    if attempt < max_retries - 1:
                        await asyncio.sleep(0.1 * (2 ** attempt))
                        continue
                    else:
                        raise
        return self._db_conn

    @tasks.loop(seconds=60)
    async def _db_keepalive(self):
        try:
            db = await self.get_db_connection()
            cur = await db.execute("SELECT 1")
            await cur.fetchone()
            await cur.close()
        except Exception:
            pass

    async def init_ar_db(self):
        """Initialize the autoreact database"""
        db = await self.get_db_connection()
        await db.execute('''
            CREATE TABLE IF NOT EXISTS autoreact_panels (
                guild_id INTEGER,
                panel_id INTEGER,
                name TEXT,
                emoji TEXT,
                channel_id INTEGER,
                is_active INTEGER DEFAULT 0,
                member_whitelist INTEGER DEFAULT 0,
                image_only_mode INTEGER DEFAULT 0,
                started_at REAL,
                PRIMARY KEY (guild_id, panel_id)
            )
        ''')

        await db.execute('''
            CREATE TABLE IF NOT EXISTS autoreact_whitelist (
                guild_id INTEGER,
                panel_id INTEGER,
                user_id INTEGER,
                PRIMARY KEY (guild_id, panel_id, user_id)
            )
        ''')

        await db.execute('''
            CREATE INDEX IF NOT EXISTS idx_autoreact_panels_active 
            ON autoreact_panels(guild_id, is_active, channel_id)
        ''')

        await db.execute('''
            CREATE INDEX IF NOT EXISTS idx_autoreact_whitelist_lookup 
            ON autoreact_whitelist(guild_id, panel_id, user_id)
        ''')

        await db.commit()

    async def get_next_panel_id(self, guild_id: int) -> int:
        """Find the lowest unused panel ID for a guild"""
        db = await self.get_db_connection()
        cursor = await db.execute('''
            SELECT panel_id FROM autoreact_panels 
            WHERE guild_id = ? 
            ORDER BY panel_id
        ''', (guild_id,))

        rows = await cursor.fetchall()
        existing_ids = [row[0] for row in rows]

        for i in range(1, len(existing_ids) + 2):
            if i not in existing_ids:
                return i

        return 1

    def parse_emoji_input(self, emoji_input: str) -> List[str]:
        """
        Parse user input for emoji(s).
        Accepts unicode and custom emojis (<:name:id> or <a:name:id>).
        Handles comma/space separated values and contiguous emoji glyphs.
        Uses a cached implementation under the hood for repeated inputs.
        """
        return list(_parse_emoji_input_cached(emoji_input or ""))

    def serialize_emojis(self, emojis: List[str]) -> str:
        """Store emoji(s) as a '|' separated string."""
        return '|'.join(emojis)

    def deserialize_emojis(self, value: str) -> List[str]:
        """Load emoji(s) from DB, accepting legacy formats."""
        if not value:
            return []
        if '|' in value:
            parts = value.split('|')
        elif ',' in value:
            parts = [p.strip() for p in value.split(',')]
        elif ' ' in value:
            parts = [p.strip() for p in value.split(' ')]
        else:
            parts = [value.strip()]
        return [p for p in parts if p]

    def format_emojis_for_display(self, emojis: List[str]) -> str:
        """Return emoji(s) joined by comma-space for human display."""
        return ', '.join(emojis) if emojis else 'None'

    autoreact_group = app_commands.Group(name="autoreact", description="AutoReact commands")

    panel_group = app_commands.Group(name="panel", description="AutoReact panel management", parent=autoreact_group)
    member_group = app_commands.Group(name="member", description="AutoReact member settings", parent=autoreact_group)
    image_group = app_commands.Group(name="image", description="AutoReact image settings", parent=autoreact_group)

    async def panel_name_autocomplete(self, interaction: discord.Interaction, current: str):
        db = await self.get_db_connection()
        cursor = await db.execute('''
            SELECT name FROM autoreact_panels
            WHERE guild_id = ?
        ''', (interaction.guild.id,))
        rows = await cursor.fetchall()
        names = [row[0] for row in rows]

        current_lower = (current or "").lower()
        filtered = [n for n in names if current_lower in n.lower()] if current else names
        filtered = filtered[:25]
        return [app_commands.Choice(name=n, value=n) for n in filtered]

    @panel_group.command(name="setup", description="Create a new autoreact panel")
    @app_commands.check(slash_mod_check)
    @app_commands.describe(
        name="Unique name for the panel",
        emojis="Up to 3 emoji(s); paste them in order",
        channel="Channel where reactions should appear"
    )
    async def setup_autoreact_panel(
            self,
            interaction: discord.Interaction,
            name: str,
            emojis: str,
            channel: discord.TextChannel
    ):
        if not await self.bot.get_cog('TopGGVoter').check_vote_access(interaction.user.id):
            embed = discord.Embed(
                title="Vote to Use This Feature!",
                description=f"This command requires voting! To access this feature, please vote for Dopamine [__here__](https://top.gg/bot/{self.bot.user.id}).",
                color=0xffaa00
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        emojis = self.parse_emoji_input(emojis)
        if len(emojis) == 0:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="Error: Invalid Emoji(s)",
                    description="Please provide at least one valid emoji.",
                    color=discord.Color.red()
                ),
                ephemeral=True
            )
            return
        if len(emojis) > 3:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="Error: Too Many Emoji(s)",
                    description="You can specify up to 3 emoji(s) per panel.",
                    color=discord.Color.red()
                ),
                ephemeral=True
            )
            return

        db = await self.get_db_connection()
        cursor = await db.execute('SELECT COUNT(*) FROM autoreact_panels WHERE guild_id = ?', (interaction.guild.id,))
        row = await cursor.fetchone()
        panel_count = row[0]

        if panel_count >= 3:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="Error: Maximum Panels Reached",
                    description="This server already has the maximum of 3 autoreact panels.",
                    color=discord.Color.red()
                ),
                ephemeral=True
            )
            return

        panel_id = await self.get_next_panel_id(interaction.guild.id)
        now_ts = time.time()

        await db.execute('''
            INSERT INTO autoreact_panels
            (guild_id, panel_id, name, emoji, channel_id, is_active, member_whitelist, image_only_mode, started_at)
            VALUES (?, ?, ?, ?, ?, 1, 0, 0, ?)
        ''', (interaction.guild.id, panel_id, name, self.serialize_emojis(emojis), channel.id, now_ts))

        await db.commit()

        embed = discord.Embed(
            title="AutoReact Panel Created and Started",
            description=f"Name: {name}\nEmoji(s): {self.format_emojis_for_display(emojis)}\nChannel: {channel.mention}",
            color=discord.Color.green()
        )
        embed.set_footer(text=f"AutoReact Panel ID: {panel_id}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @autoreact_group.command(name="panels", description="View all autoreact panels in this server")
    @app_commands.check(slash_mod_check)
    async def autoreact_panels(self, interaction: discord.Interaction):
        """Display all autoreact panels for the guild"""

        if not await self.bot.get_cog('TopGGVoter').check_vote_access(interaction.user.id):
            embed = discord.Embed(
                title="Vote to Use This Feature!",
                description="This command requires voting! To access this feature, please vote for Dopamine [__here__](https://top.gg/bot/{self.bot.user.id}).".format(
                    bot_id=self.bot.user.id
                ),
                color=0xffaa00
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        db = await self.get_db_connection()
        cursor = await db.execute('''
            SELECT panel_id, name, emoji, channel_id, is_active, member_whitelist, image_only_mode
            FROM autoreact_panels 
            WHERE guild_id = ?
            ORDER BY panel_id
        ''', (interaction.guild.id,))

        panels = await cursor.fetchall()

        if not panels:
            embed = discord.Embed(
                title="Your AutoReact Panels",
                description="No autoreact panels found, use `/autoreact panel setup` to create one!",
                color=discord.Color(0x337fd5)
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(
            title="Your AutoReact Panels",
            color=discord.Color(0x337fd5)
        )

        description = ""
        for panel_id, name, emoji_value, channel_id, is_active, member_whitelist, image_only_mode in panels:
            channel = self.bot.get_channel(channel_id) if channel_id else None
            channel_name = channel.mention if channel else "Not assigned"

            emojis_list = self.deserialize_emojis(emoji_value)
            emojis_display = self.format_emojis_for_display(emojis_list)

            whitelist_info = ""
            if member_whitelist:
                db2 = await self.get_db_connection()
                cursor2 = await db2.execute('''
                    SELECT COUNT(*) FROM autoreact_whitelist 
                    WHERE guild_id = ? AND panel_id = ?
                ''', (interaction.guild.id, panel_id))
                row2 = await cursor2.fetchone()
                whitelist_count = row2[0]
                whitelist_info = f" ({whitelist_count} users)"
            else:
                whitelist_info = " (All users)"

            description += f"## {panel_id}. {name}\n"
            description += f"* **Emoji(s):** {emojis_display}\n"
            description += f"* **Channel:** {channel_name}\n"
            description += f"* **Status:** {'🟢 Active' if is_active else '🔴 Inactive'}\n"
            description += f"* **Target:** {'Specific users' if member_whitelist else 'All users'}{whitelist_info}\n"
            description += f"* **Mode:** {'Image-only' if image_only_mode else 'All messages'}\n"
            description += f"* **Panel ID: {panel_id}**\n\n"

        embed.description = description
        embed.set_footer(text=f"Total panels: {len(panels)}/3")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @panel_group.command(name="start", description="Start an autoreact panel")
    @app_commands.check(slash_mod_check)
    @app_commands.autocomplete(name=panel_name_autocomplete)
    async def start_autoreact_panel(
            self,
            interaction: discord.Interaction,
            name: str
    ):
        if not await self.bot.get_cog('TopGGVoter').check_vote_access(interaction.user.id):
            embed = discord.Embed(
                title="Vote to Use This Feature!",
                description=f"This command requires voting! To access this feature, please vote for Dopamine [__here__](https://top.gg/bot/{self.bot.user.id}).",
                color=0xffaa00
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        db = await self.get_db_connection()
        cursor = await db.execute('''
            SELECT panel_id, emoji, channel_id, is_active FROM autoreact_panels
            WHERE guild_id = ? AND name = ?
        ''', (interaction.guild.id, name))
        row = await cursor.fetchone()

        if not row:
            await interaction.response.send_message(
                embed=discord.Embed(title="Error", description="Panel not found.", color=discord.Color.red()),
                ephemeral=True
            )
            return

        panel_id, emoji_value, channel_id, is_active = row

        if channel_id is None:
            await interaction.response.send_message(
                embed=discord.Embed(title="Channel Not Set",
                                    description="Set a channel for this panel before starting.",
                                    color=discord.Color.red()),
                ephemeral=True
            )
            return

        await db.execute('''
            UPDATE autoreact_panels
            SET is_active = 1, started_at = ?
            WHERE guild_id = ? AND panel_id = ?
        ''', (time.time(), interaction.guild.id, panel_id))
        await db.commit()

        emojis_display = self.format_emojis_for_display(self.deserialize_emojis(emoji_value))
        channel = interaction.guild.get_channel(channel_id)
        channel_mention = channel.mention if channel else f"<#{channel_id}>"
        await interaction.response.send_message(
            embed=discord.Embed(
                title="AutoReact Started",
                description=f"Panel **{name}** is now active in {channel_mention} and will react with {emojis_display}.",
                color=discord.Color.green()
            ),
            ephemeral=True
        )

    @panel_group.command(name="stop", description="Stop an autoreact panel")
    @app_commands.check(slash_mod_check)
    @app_commands.autocomplete(name=panel_name_autocomplete)
    async def stop_autoreact_panel(self, interaction: discord.Interaction, name: str):
        if not await self.bot.get_cog('TopGGVoter').check_vote_access(interaction.user.id):
            embed = discord.Embed(
                title="Vote to Use This Feature!",
                description=f"This command requires voting! To access this feature, please vote for Dopamine [__here__](https://top.gg/bot/{self.bot.user.id}).",
                color=0xffaa00
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        db = await self.get_db_connection()
        cursor = await db.execute('''
            SELECT panel_id, channel_id, is_active FROM autoreact_panels
            WHERE guild_id = ? AND name = ?
        ''', (interaction.guild.id, name))
        row = await cursor.fetchone()

        if not row:
            await interaction.response.send_message(
                embed=discord.Embed(title="Error", description="Panel not found.", color=discord.Color.red()),
                ephemeral=True
            )
            return

        panel_id, channel_id, is_active = row
        if not is_active:
            await interaction.response.send_message(
                embed=discord.Embed(title="Already Stopped", description="That panel is not currently active.",
                                    color=discord.Color.red()),
                ephemeral=True
            )
            return

        await db.execute('''
            UPDATE autoreact_panels
            SET is_active = 0, started_at = NULL
            WHERE guild_id = ? AND panel_id = ?
        ''', (interaction.guild.id, panel_id))
        await db.commit()

        channel = interaction.guild.get_channel(channel_id) if channel_id else None
        channel_mention = channel.mention if channel else "its set channel"
        await interaction.response.send_message(
            embed=discord.Embed(
                title="AutoReact Stopped",
                description=f"Panel **{name}** has been stopped in {channel_mention}.",
                color=discord.Color.green()
            ),
            ephemeral=True
        )

    @panel_group.command(name="delete", description="Delete an autoreact panel")
    @app_commands.check(slash_mod_check)
    @app_commands.autocomplete(name=panel_name_autocomplete)
    async def delete_autoreact_panel(self, interaction: discord.Interaction, name: str):
        if not await self.bot.get_cog('TopGGVoter').check_vote_access(interaction.user.id):
            embed = discord.Embed(
                title="Vote to Use This Feature!",
                description=f"This command requires voting! To access this feature, please vote for Dopamine [__here__](https://top.gg/bot/{self.bot.user.id}).",
                color=0xffaa00
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        db = await self.get_db_connection()
        cursor = await db.execute('''
            SELECT panel_id FROM autoreact_panels
            WHERE guild_id = ? AND name = ?
        ''', (interaction.guild.id, name))
        row = await cursor.fetchone()

        if not row:
            await interaction.response.send_message(
                embed=discord.Embed(title="Error", description="Panel not found.", color=discord.Color.red()),
                ephemeral=True
            )
            return

        panel_id = row[0]

        await db.execute('''
            DELETE FROM autoreact_whitelist
            WHERE guild_id = ? AND panel_id = ?
        ''', (interaction.guild.id, panel_id))

        await db.execute('''
            DELETE FROM autoreact_panels
            WHERE guild_id = ? AND panel_id = ?
        ''', (interaction.guild.id, panel_id))

        await db.commit()

        await interaction.response.send_message(
            embed=discord.Embed(
                title="AutoReact Panel Deleted",
                description=f"Deleted panel **{name}**.",
                color=discord.Color.green()
            ),
            ephemeral=True
        )

    @panel_group.command(name="edit", description="Edit an autoreact panel")
    @app_commands.check(slash_mod_check)
    @app_commands.autocomplete(name=panel_name_autocomplete)
    async def edit_autoreact_panel(
            self,
            interaction: discord.Interaction,
            name: str,
            emoji: Optional[str] = None,
            channel: Optional[discord.TextChannel] = None,
            new_name: Optional[str] = None
    ):
        if not await self.bot.get_cog('TopGGVoter').check_vote_access(interaction.user.id):
            embed = discord.Embed(
                title="Vote to Use This Feature!",
                description=f"This command requires voting! To access this feature, please vote for Dopamine [__here__](https://top.gg/bot/{self.bot.user.id}).",
                color=0xffaa00
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        db = await self.get_db_connection()
        cursor = await db.execute('''
            SELECT panel_id, name, emoji, channel_id, is_active FROM autoreact_panels
            WHERE guild_id = ? AND name = ?
        ''', (interaction.guild.id, name))
        row = await cursor.fetchone()

        if not row:
            await interaction.response.send_message(
                embed=discord.Embed(title="Error", description="Panel not found.", color=discord.Color.red()),
                ephemeral=True
            )
            return

        panel_id, cur_name, cur_emoji, cur_channel_id, is_active = row

        update_fields = []
        params = []

        if emoji is not None:
            emojis = self.parse_emoji_input(emoji)
            if len(emojis) == 0:
                await interaction.response.send_message(
                    embed=discord.Embed(
                        title="Error: Invalid Emoji(s)",
                        description="Please provide at least one valid emoji.",
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )
                return
            if len(emojis) > 3:
                await interaction.response.send_message(
                    embed=discord.Embed(
                        title="Error: Too Many Emoji(s)",
                        description="You can specify up to 3 emoji(s) per panel.",
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )
                return
            update_fields.append("emoji = ?")
            params.append(self.serialize_emojis(emojis))

        if channel is not None:
            update_fields.append("channel_id = ?")
            params.append(channel.id)

        if new_name is not None:
            update_fields.append("name = ?")
            params.append(new_name)

        if not update_fields:
            await interaction.response.send_message(
                embed=discord.Embed(title="No Changes", description="Provide at least one field to edit.",
                                    color=discord.Color.red()),
                ephemeral=True
            )
            return

        params.extend([interaction.guild.id, panel_id])

        await db.execute(f'''
            UPDATE autoreact_panels
            SET {", ".join(update_fields)}
            WHERE guild_id = ? AND panel_id = ?
        ''', params)

        await db.commit()

        await interaction.response.send_message(
            embed=discord.Embed(
                title="AutoReact Panel Updated",
                description=f"Updated panel **{cur_name}**.",
                color=discord.Color.green()
            ),
            ephemeral=True
        )

    @member_group.command(name="whitelist", description="Set up member whitelist for autoreact")
    @app_commands.check(slash_mod_check)
    async def autoreact_member_whitelist(
            self,
            interaction: discord.Interaction,
            member: discord.Member
    ):
        """Set up member whitelist for autoreact"""

        if member.bot:
            embed = discord.Embed(
                title="Error: Cannot Use Bot",
                description="You cannot add bots to the auto-react whitelist.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if not await self.bot.get_cog('TopGGVoter').check_vote_access(interaction.user.id):
            embed = discord.Embed(
                title="Vote to Use This Feature!",
                description="This command requires voting! To access this feature, please vote for Dopamine [__here__](https://top.gg/bot/{self.bot.user.id}).".format(
                    bot_id=self.bot.user.id
                ),
                color=0xffaa00
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        db = await self.get_db_connection()
        cursor = await db.execute('''
            SELECT panel_id, name, emoji
            FROM autoreact_panels 
            WHERE guild_id = ?
            ORDER BY panel_id
        ''', (interaction.guild.id,))

        panels = await cursor.fetchall()

        if not panels:
            embed = discord.Embed(
                title="No Available Panels",
                description="No autoreact panels found. Create one first!",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        view = MemberWhitelistSelectionView(self.bot, interaction.guild.id, member.id, panels)

        embed = discord.Embed(
            title="Select AutoReact Panel for Member Whitelist",
            description=f"Choose which autoreact panel to whitelist {member.mention} for:",
            color=discord.Color(0x337fd5)
        )

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @image_group.command(name="only", description="Set up image-only mode for autoreact")
    @app_commands.check(slash_mod_check)
    async def autoreact_image_only_mode(
            self,
            interaction: discord.Interaction
    ):
        """Set up image-only mode for autoreact"""

        if not await self.bot.get_cog('TopGGVoter').check_vote_access(interaction.user.id):
            embed = discord.Embed(
                title="Vote to Use This Feature!",
                description="This command requires voting! To access this feature, please vote for Dopamine [__here__](https://top.gg/bot/{self.bot.user.id}).".format(
                    bot_id=self.bot.user.id
                ),
                color=0xffaa00
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        db = await self.get_db_connection()
        cursor = await db.execute('''
            SELECT panel_id, name, emoji
            FROM autoreact_panels 
            WHERE guild_id = ?
            ORDER BY panel_id
        ''', (interaction.guild.id,))

        panels = await cursor.fetchall()

        if not panels:
            embed = discord.Embed(
                title="No Available Panels",
                description="No autoreact panels found. Create one first!",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        view = ImageOnlyModeSelectionView(self.bot, interaction.guild.id, panels)

        embed = discord.Embed(
            title="Select AutoReact Panel for Image-Only Mode",
            description="Choose which autoreact panel to enable image-only mode for:",
            color=discord.Color(0x337fd5)
        )

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @tasks.loop(seconds=3)
    async def autoreact_monitor(self):
        """Background task to monitor messages and add reactions"""
        try:
            try:
                db = await self.get_db_connection()
            except Exception as e:
                print(f"Error getting database connection in autoreact_monitor: {e}")
                return

            cursor = await db.execute('''
                SELECT guild_id,
                       panel_id,
                       name,
                       emoji,
                       channel_id,
                       member_whitelist,
                       image_only_mode,
                       started_at
                FROM autoreact_panels 
                WHERE channel_id IS NOT NULL
                  AND is_active = 1
                  AND started_at IS NOT NULL
            ''')

            active_panels = await cursor.fetchall()
            await cursor.close()

            if not active_panels:
                self._active_panel_cache = {}
                return

            panel_cache: Dict[Tuple[int, int], Dict] = {}
            for guild_id, panel_id, name, emoji_value, channel_id, member_whitelist, image_only_mode, started_at in active_panels:
                emojis_list = self.deserialize_emojis(emoji_value)
                if not emojis_list:
                    continue
                panel_cache[(guild_id, panel_id)] = {
                    "guild_id": guild_id,
                    "panel_id": panel_id,
                    "name": name,
                    "emojis": emojis_list,
                    "channel_id": channel_id,
                    "member_whitelist": bool(member_whitelist),
                    "image_only_mode": bool(image_only_mode),
                    "started_at": started_at,
                    "whitelist_users": set(),
                }

            whitelist_cursor = await db.execute('''
                SELECT guild_id, panel_id, user_id
                FROM autoreact_whitelist
            ''')
            whitelist_rows = await whitelist_cursor.fetchall()
            await whitelist_cursor.close()

            for wl_guild_id, wl_panel_id, user_id in whitelist_rows:
                key = (wl_guild_id, wl_panel_id)
                panel = panel_cache.get(key)
                if panel and panel["member_whitelist"]:
                    whitelist: Set[int] = panel["whitelist_users"]
                    whitelist.add(user_id)

            self._active_panel_cache = panel_cache

            channel_panel_map: Dict[Tuple[int, int], List[Dict]] = {}
            for (guild_id, _panel_id), panel in panel_cache.items():
                key = (guild_id, panel["channel_id"])
                channel_panel_map.setdefault(key, []).append(panel)

            tasks = [
                self._process_channel_panels(guild_id, channel_id, panels)
                for (guild_id, channel_id), panels in channel_panel_map.items()
            ]
            if tasks:
                await asyncio.gather(*tasks)

        except Exception as e:
            print(f"Error in autoreact_monitor task: {e}")

    async def reaction_processor(self):
        """Background worker that processes queued reactions with rate limiting."""
        while True:
            try:
                message, em = await self._reaction_queue.get()
                try:
                    async with self._reaction_semaphore:
                        try:
                            await message.add_reaction(em)
                        except (discord.Forbidden, discord.HTTPException):
                            pass
                finally:
                    self._reaction_queue.task_done()
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                break
            except Exception:
                continue

    async def _process_channel_panels(self, guild_id: int, channel_id: int, panels: List[Dict]):
        """Fetch history for a single channel and apply all relevant panels."""
        try:
            guild = self.bot.get_guild(guild_id)
            if not guild:
                return

            channel = guild.get_channel(channel_id)
            if not channel:
                return

            min_started_at = min(p["started_at"] for p in panels)
            start_time = datetime.fromtimestamp(min_started_at, tz=timezone.utc)

            async with self._channel_semaphore:
                async for message in channel.history(limit=50, after=start_time):
                    message_ts = message.created_at.timestamp()

                    if message.author.bot:
                        continue

                    for panel in panels:
                        started_at = panel["started_at"]
                        if message_ts <= started_at:
                            continue

                        emojis_list = panel["emojis"]
                        if not emojis_list:
                            continue

                        if panel["member_whitelist"]:
                            whitelist_users: Set[int] = panel["whitelist_users"]
                            if whitelist_users and message.author.id not in whitelist_users:
                                continue

                        if panel["image_only_mode"]:
                            has_images = bool(message.attachments) or (message.embeds and any(
                                embed.type == discord.EmbedType.image for embed in message.embeds))
                            if not has_images:
                                continue

                        for em in emojis_list:
                            try:
                                already_reacted = False
                                for reaction in message.reactions:
                                    if str(reaction.emoji) == em and reaction.me:
                                        already_reacted = True
                                        break
                                if already_reacted:
                                    continue

                                await self._reaction_queue.put((message, em))
                            except Exception:
                                continue
        except Exception as e:
            print(f"Error processing channel {channel_id} in guild {guild_id}: {e}")


class MemberWhitelistSelectionView(discord.ui.View):
    def __init__(self, bot, guild_id: int, user_id: int, panels):
        super().__init__(timeout=300)
        self.bot = bot
        self.guild_id = guild_id
        self.user_id = user_id
        self.panels = panels

        for panel_id, name, emoji in panels:
            button = discord.ui.Button(
                label=f"{panel_id}. {name}",
                style=discord.ButtonStyle.primary,
                custom_id=f"select_whitelist_panel_{panel_id}"
            )
            button.callback = lambda interaction, pid=panel_id: self.select_panel(interaction, pid)
            self.add_item(button)

    async def select_panel(self, interaction: discord.Interaction, panel_id: int):
        selected_panel = None
        for panel_id_val, name, emoji in self.panels:
            if panel_id_val == panel_id:
                selected_panel = (panel_id_val, name, emoji)
                break

        if not selected_panel:
            embed = discord.Embed(
                title="Error: Panel Not Found",
                description="The selected panel could not be found.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        cog = self.bot.get_cog('AutoReact')
        if cog:
            db = await cog.get_db_connection()
            await db.execute('''
                UPDATE autoreact_panels 
                SET member_whitelist = 1
                WHERE guild_id = ? AND panel_id = ?
            ''', (self.guild_id, panel_id))

            await db.execute('''
                INSERT OR REPLACE INTO autoreact_whitelist 
                (guild_id, panel_id, user_id)
                VALUES (?, ?, ?)
            ''', (self.guild_id, panel_id, self.user_id))

            await db.commit()

        success_embed = discord.Embed(
            title="Member Whitelist Set Successfully",
            description=f"AutoReact panel **{selected_panel[1]}** will now only react to messages from <@{self.user_id}>!",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=success_embed, ephemeral=True)


class ImageOnlyModeSelectionView(discord.ui.View):
    def __init__(self, bot, guild_id: int, panels):
        super().__init__(timeout=300)
        self.bot = bot
        self.guild_id = guild_id
        self.panels = panels

        for panel_id, name, emoji in panels:
            button = discord.ui.Button(
                label=f"{panel_id}. {name}",
                style=discord.ButtonStyle.primary,
                custom_id=f"select_image_panel_{panel_id}"
            )
            button.callback = lambda interaction, pid=panel_id: self.select.panel(interaction, pid)
            self.add_item(button)

    async def select_panel(self, interaction: discord.Interaction, panel_id: int):
        selected_panel = None
        for panel_id_val, name, emoji in self.panels:
            if panel_id_val == panel_id:
                selected_panel = (panel_id_val, name, emoji)
                break

        if not selected_panel:
            embed = discord.Embed(
                title="Error: Panel Not Found",
                description="The selected panel could not be found.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        cog = self.bot.get_cog('AutoReact')
        if cog:
            db = await cog.get_db_connection()
            await db.execute('''
                UPDATE autoreact_panels 
                SET image_only_mode = 1
                WHERE guild_id = ? AND panel_id = ?
            ''', (self.guild_id, panel_id))

            await db.commit()

        success_embed = discord.Embed(
            title="Image-Only Mode Set Successfully",
            description=f"AutoReact panel **{selected_panel[1]}** will now only react to messages that contain images!",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=success_embed, ephemeral=True)

async def setup(bot):
    await bot.add_cog(AutoReact(bot))
