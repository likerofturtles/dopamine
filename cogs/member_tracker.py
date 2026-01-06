import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiosqlite
import asyncio
from typing import Optional
from contextlib import asynccontextmanager
from config import MCTDB_PATH
from utils.checks import slash_mod_check
import re


class MemberTrackerEditModal(discord.ui.Modal, title="Edit Member Tracker Settings", ):
    member_goal = discord.ui.TextInput(
        label="Member Goal",
        placeholder="Set a member count goal... (leave blank to keep unchanged)",
        required=False,
        max_length=10
    )
    format_template = discord.ui.TextInput(
        label="Format (Documentation: /membertracker info) ",
        style=discord.TextStyle.paragraph,
        placeholder="Enter a format here... (leave blank to keep unchanged)",
        required=False,
        max_length=1000
    )
    embed_color = discord.ui.TextInput(
        label="Embed Color",
        placeholder="Enter a HEX value... (leave blank to keep unchanged)",
        required=False,
        max_length=9
    )

    def __init__(self, cog: "MemberCountTracker"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        updates = []

        if not await self.cog.check_vote_access(interaction.user.id):
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="Vote to Use This Feature!",
                    description=f"This command requires voting! To access this feature, please vote for Dopamine [__here__](https://top.gg/bot/{self.bot.user.id}).",
                    color=0xffaa00
                ),
                ephemeral=True
            )
            return

        async with self.cog.lock:
            async with self.cog.get_db_connection() as db:
                cursor = await db.execute(
                    "SELECT is_active FROM member_tracker WHERE guild_id = ?",
                    (interaction.guild.id,)
                )
                row = await cursor.fetchone()
                if not row or not row[0]:
                    await interaction.response.send_message(
                        embed=discord.Embed(
                            title="Tracker Not Enabled",
                            description="Enable the tracker with `/membertracker enable` before editing settings.",
                            color=discord.Color.red()
                        ),
                        ephemeral=True
                    )
                    return

                if self.member_goal.value:
                    try:
                        goal_val = int(self.member_goal.value)
                        if goal_val <= 0:
                            raise ValueError
                    except ValueError:
                        await interaction.response.send_message(
                            embed=discord.Embed(
                                title="Invalid Member Goal",
                                description="Enter a positive integer or leave the field blank.",
                                color=discord.Color.red()
                            ),
                            ephemeral=True
                        )
                        return
                    await db.execute(
                        "UPDATE member_tracker SET member_goal = ? WHERE guild_id = ?",
                        (goal_val, interaction.guild.id)
                    )
                    updates.append(f"Member goal set to **{goal_val}**")

                if self.format_template.value:
                    template = self.format_template.value.strip()
                    if not any(token in template for token in ("{member_count}", "{remaining_until_goal}")):
                        await interaction.response.send_message(
                            embed=discord.Embed(
                                title="Invalid Format",
                                description="Format must include `{member_count}` or `{remaining_until_goal}`.",
                                color=discord.Color.red()
                            ),
                            ephemeral=True
                        )
                        return
                    await db.execute(
                        "UPDATE member_tracker SET custom_format = ? WHERE guild_id = ?",
                        (template, interaction.guild.id)
                    )
                    updates.append("Custom format updated")

                if self.embed_color.value:
                    hex_value = self.embed_color.value.strip().lstrip("#")
                    if not re.fullmatch(r"[0-9a-fA-F]{6}", hex_value):
                        await interaction.response.send_message(
                            embed=discord.Embed(
                                title="Invalid Color",
                                description="Use a 6-digit hex color like `FFA500`.",
                                color=discord.Color.red()
                            ),
                            ephemeral=True
                        )
                        return
                    await db.execute(
                        "UPDATE member_tracker SET color = ? WHERE guild_id = ?",
                        (int(hex_value, 16), interaction.guild.id)
                    )
                    updates.append(f"Embed color set to `#{hex_value.upper()}`")

                await db.commit()

        if not updates:
            updates.append("No changes were submitted; settings remain the same.")

        await interaction.response.send_message(
            embed=discord.Embed(
                title="Member Tracker Updated",
                description="\n".join(updates),
                color=discord.Color.green()
            ),
            ephemeral=True
        )


class MemberCountTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.lock = asyncio.Lock()
        self._db_pool = []
        self._pool_lock = asyncio.Lock()
        self._pool_semaphore: Optional[asyncio.Semaphore] = None
        self._max_pool_size = 5
        self._active_guild_counts = {}

    async def cog_load(self):
        """Called when the cog is loaded"""
        await self.init_mctracker_db()
        await self.load_active_guild_counts()
        if not self.member_count_monitor.is_running():
            self.member_count_monitor.start()
        if not self._db_keepalive.is_running():
            self._db_keepalive.start()

    async def cog_unload(self):
        """Close database connection when cog is unloaded"""
        if self._db_keepalive.is_running():
            self._db_keepalive.cancel()
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
            created_conns = []
            for _ in range(self._max_pool_size):
                for attempt in range(max_retries):
                    try:
                        conn = await aiosqlite.connect(MCTDB_PATH, timeout=5.0)
                        await conn.execute("PRAGMA busy_timeout=5000")
                        await conn.execute("PRAGMA journal_mode=WAL")
                        await conn.execute("PRAGMA wal_autocheckpoint=1000")
                        await conn.execute("PRAGMA synchronous=NORMAL")
                        await conn.execute("PRAGMA cache_size=-32000")
                        await conn.execute("PRAGMA foreign_keys=ON")
                        await conn.commit()
                        created_conns.append(conn)
                        break
                    except Exception:
                        if attempt < max_retries - 1:
                            await asyncio.sleep(0.1 * (2 ** attempt))
                            continue
                        else:
                            raise

            self._db_pool = created_conns
            self._pool_semaphore = asyncio.Semaphore(len(self._db_pool))

    @asynccontextmanager
    async def get_db_connection(self):
        """Acquire a pooled database connection."""
        if not self._db_pool:
            await self._init_db_pool()

        await self._pool_semaphore.acquire()
        conn = self._db_pool.pop()
        try:
            yield conn
        finally:
            self._db_pool.append(conn)
            self._pool_semaphore.release()

    @tasks.loop(seconds=60)
    async def _db_keepalive(self):
        try:
            async with self.get_db_connection() as db:
                cur = await db.execute("SELECT 1")
                await cur.fetchone()
                await cur.close()
        except Exception:
            pass

    async def init_mctracker_db(self):
        """Initialize the member count tracker database"""
        async with self.get_db_connection() as db:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS member_tracker (
                    guild_id INTEGER PRIMARY KEY,
                    channel_id INTEGER,
                    is_active INTEGER DEFAULT 0,
                    member_goal INTEGER,
                    custom_format TEXT,
                    last_member_count INTEGER,
                    color INTEGER
                )
            ''')

            try:
                await db.execute('ALTER TABLE member_tracker ADD COLUMN color INTEGER')
            except Exception:
                pass

            await db.execute('''
                CREATE INDEX IF NOT EXISTS idx_member_tracker_active 
                ON member_tracker(is_active, guild_id)
            ''')

            await db.commit()

    async def load_active_guild_counts(self):
        """Load active guild_id -> last_member_count pairs into the in-memory cache."""
        async with self.get_db_connection() as db:
            cursor = await db.execute('''
                SELECT guild_id, COALESCE(last_member_count, 0)
                FROM member_tracker
                WHERE is_active = 1
            ''')
            rows = await cursor.fetchall()
            await cursor.close()
        self._active_guild_counts = {guild_id: last_count for guild_id, last_count in rows}

    async def check_vote_access(self, user_id: int) -> bool:
        """Check if user has voted"""
        voter_cog = self.bot.get_cog('TopGGVoter')
        if not voter_cog:
            return True
        return await voter_cog.check_vote_access(user_id)

    membertracker_group = app_commands.Group(name="membertracker", description="Member count tracker commands")

    @membertracker_group.command(name="enable", description="Enable the member count tracker for this server")
    @app_commands.describe(channel="The channel where member count updates will be sent")
    @app_commands.check(slash_mod_check)
    async def enable_member_tracker(
            self,
            interaction: discord.Interaction,
            channel: discord.TextChannel
    ):
        """Enable the member count tracker for this server"""
        try:
            if not await self.check_vote_access(interaction.user.id):
                embed = discord.Embed(
                    title="Vote to Use This Feature!",
                    description=f"This command requires voting! To access this feature, please vote for Dopamine [__here__](https://top.gg/bot/{self.bot.user.id}).",
                    color=0xffaa00
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            async with self.lock:
                async with self.get_db_connection() as db:
                    await db.execute('''
                        INSERT OR REPLACE INTO member_tracker 
                        (guild_id, channel_id, is_active, last_member_count, color)
                        VALUES (?, ?, 1, ?, ?)
                    ''', (interaction.guild.id, channel.id, interaction.guild.member_count, 0x337fd5))
                    await db.commit()

            self._active_guild_counts[interaction.guild.id] = interaction.guild.member_count

            embed = discord.Embed(
                title="Member Count Tracker Enabled",
                description=f"Member count tracker is now active in {channel.mention}!\n\nDopamine will monitor your server's member count and send updates when the count changes.",
                color=discord.Color.green()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            embed = discord.Embed(
                title="Error",
                description=f"An error occurred: {str(e)}",
                color=discord.Color.red()
            )
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)

    @membertracker_group.command(name="disable", description="Disable the member count tracker for this server")
    @app_commands.check(slash_mod_check)
    async def disable_member_tracker(self, interaction: discord.Interaction):
        """Disable the member count tracker for this server"""
        try:
            if not await self.check_vote_access(interaction.user.id):
                embed = discord.Embed(
                    title="Vote to Use This Feature!",
                    description=f"This command requires voting! To access this feature, please vote for Dopamine [__here__](https://top.gg/bot/{self.bot.user.id}).",
                    color=0xffaa00
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            async with self.lock:
                async with self.get_db_connection() as db:
                    await db.execute('''
                        DELETE FROM member_tracker 
                        WHERE guild_id = ?
                    ''', (interaction.guild.id,))
                    await db.commit()

            embed = discord.Embed(
                title="Member Count Tracker Disabled",
                description="Member count tracker has been disabled and all related data has been removed.",
                color=discord.Color.green()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            embed = discord.Embed(
                title="Error",
                description=f"An error occurred: {str(e)}",
                color=discord.Color.red()
            )
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)

    @membertracker_group.command(name="info", description="View member count tracker information for this server")
    @app_commands.check(slash_mod_check)
    async def member_tracker_info(self, interaction: discord.Interaction):
        """View member count tracker information for this server"""
        try:
            if not await self.check_vote_access(interaction.user.id):
                embed = discord.Embed(
                    title="Vote to Use This Feature!",
                    description=f"This command requires voting! To access this feature, please vote for Dopamine [__here__](https://top.gg/bot/{self.bot.user.id}).",
                    color=0xffaa00
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            async with self.get_db_connection() as db:
                cursor = await db.execute('''
                    SELECT channel_id, is_active, member_goal, custom_format, color
                    FROM member_tracker
                    WHERE guild_id = ?
                ''', (interaction.guild.id,))
                result = await cursor.fetchone()

            if not result:
                embed = discord.Embed(
                    title="Member Count Tracker Info",
                    description="No member count tracker is currently active for this server.\n\nUse `/membertracker enable` to start tracking!",
                    color=discord.Color.blue()
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            channel_id, is_active, member_goal, custom_format, color = result

            if channel_id:
                channel = self.bot.get_channel(channel_id)
                channel_mention = channel.mention if channel else "Unknown Channel"
            else:
                channel_mention = "Not set"

            status_emoji = "🟢" if is_active else "🔴"
            status_text = "Active" if is_active else "Inactive"

            embed = discord.Embed(
                title="Member Count Tracker Information",
                description=f"**Status:** {status_emoji} {status_text}\n**Channel:** {channel_mention}",
                color=color if color else discord.Color(0x337fd5)
            )

            if member_goal:
                embed.add_field(name="Member Goal", value=f"**{member_goal}** members", inline=False)

            if custom_format:
                embed.add_field(name="Custom Format", value=f"```\n{custom_format}\n```", inline=False)
            else:
                embed.add_field(name="Format", value="Default", inline=False)

            if color:
                embed.add_field(name="Color", value=f"`#{hex(color)[2:].upper()}`", inline=False)

            embed.add_field(name="---------------------------------------------------", value="**➤ DOCUMENTATION**")

            embed.add_field(
                name="Available Variables",
                value=(
                    "• `{member_count}` - Current member count of your server\n"
                    "• `{remaining_until_goal}` - Members remaining to reach the goal\n"
                    "• `{member_goal}` - The member goal you've set\n"
                    "• `{servername}` - Name of your server\n\n "
                ),
                inline=False
            )
            embed.add_field(
                name="Example Formats",
                value=(
                    "• `🎉 {member_count} members! Only {remaining_until_goal} more to go!`\n"
                    "• `{servername} reached {member_count}! Goal: {member_goal}`\n\n"
                ),
                inline=False
            )
            embed.add_field(
                name="Notes",
                value=(
                    "• You can customize it however you want, you don't have to use these examples!\n"
                    "• `{remaining_until_goal}` will only work if a goal is set."
                ),
                inline=False
            )

            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            embed = discord.Embed(
                title="Error",
                description=f"An error occurred: {str(e)}",
                color=discord.Color.red()
            )
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)

    @membertracker_group.command(name="edit", description="Edit member tracker settings")
    @app_commands.check(slash_mod_check)
    async def edit_member_tracker(self, interaction: discord.Interaction):
        await interaction.response.send_modal(MemberTrackerEditModal(self))

    @membertracker_group.command(name="reset", description="Reset member tracker settings for this server to defaults")
    @app_commands.check(slash_mod_check)
    async def reset_member_tracker(self, interaction: discord.Interaction):
        """Delete all rows for this server across tables that have a guild_id column."""
        try:
            if not await self.check_vote_access(interaction.user.id):
                embed = discord.Embed(
                    title="Vote to Use This Feature!",
                    description=f"This command requires voting! To access this feature, please vote for Dopamine [__here__](https://top.gg/bot/{self.bot.user.id}).",
                    color=0xffaa00
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            async with self.get_db_connection() as db:
                cursor = await db.execute("""
                        SELECT name
                        FROM sqlite_master
                        WHERE type='table' AND name NOT LIKE 'sqlite_%'
                    """)
                tables = [row[0] for row in await cursor.fetchall()]

                tables_with_guild_id = []
                for table in tables:
                    try:
                        cursor = await db.execute(f"PRAGMA table_info({table})")
                        cols = await cursor.fetchall()
                        if any(col[1].lower() == "guild_id" for col in cols):
                            tables_with_guild_id.append(table)
                    except Exception:
                        continue

                deleted_from = []
                async with self.lock:
                    for table in tables_with_guild_id:
                        try:
                            await db.execute(f"DELETE FROM {table} WHERE guild_id = ?", (interaction.guild.id,))
                            deleted_from.append(table)
                        except Exception:
                            continue

                    await db.commit()

            self._active_guild_counts.pop(interaction.guild.id, None)

            if deleted_from:
                cleared_list = ", ".join(f"`{t}`" for t in deleted_from)
                desc = (
                    "All configurations for this server have been reset to defaults.\n\n"
                    f"Cleared rows from: {cleared_list}"
                )
                color = discord.Color.green()
            else:
                desc = (
                    "No server-specific data was found to reset.\n"
                    "If the tracker was enabled previously, use `/membertracker enable` to start fresh."
                )
                color = discord.Color.blue()

            embed = discord.Embed(
                title="Member Tracker Reset",
                description=desc,
                color=color
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

        except Exception as e:
            embed = discord.Embed(
                title="Error",
                description=f"An error occurred: {str(e)}",
                color=discord.Color.red()
            )
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)

    @tasks.loop(minutes=5)
    async def member_count_monitor(self):
        """Background task to monitor member count changes"""
        try:
            cache_snapshot = list(self._active_guild_counts.items())

            bulk_last_count_updates = []
            guilds_to_deactivate = []
            send_tasks = []

            for guild_id, last_member_count in cache_snapshot:
                try:
                    guild = self.bot.get_guild(guild_id)
                    if not guild:
                        continue

                    current_count = guild.member_count

                    bulk_last_count_updates.append((current_count, guild_id))

                    if current_count <= last_member_count:
                        continue

                    async with self.get_db_connection() as db:
                        cursor = await db.execute('''
                            SELECT channel_id, member_goal, custom_format, color
                            FROM member_tracker
                            WHERE guild_id = ? AND is_active = 1
                        ''', (guild_id,))
                        row = await cursor.fetchone()
                        await cursor.close()

                    if not row:
                        continue

                    channel_id, member_goal, custom_format, color = row

                    channel = guild.get_channel(channel_id)
                    if not channel:
                        continue

                    remaining = None
                    if member_goal:
                        remaining = max(0, member_goal - current_count)

                    if custom_format:
                        format_text = custom_format
                        format_text = format_text.replace('{member_count}', str(current_count))
                        format_text = format_text.replace('{servername}', guild.name)

                        if '{remaining_until_goal}' in format_text:
                            if remaining is not None:
                                format_text = format_text.replace('{remaining_until_goal}', str(remaining))
                            else:
                                format_text = format_text.replace('{remaining_until_goal}', 'N/A')

                        if '{member_goal}' in format_text:
                            format_text = format_text.replace(
                                '{member_goal}',
                                str(member_goal) if member_goal else 'N/A'
                            )
                    else:
                        format_text = f"{guild.name} now has **{current_count}** members!"

                        if member_goal and remaining and remaining > 0:
                            format_text += (
                                f"\n> **{remaining}** more members until we reach our goal of **{member_goal}**!"
                            )

                    embed_color = color if color else 0x337fd5
                    embed = discord.Embed(
                        description=format_text,
                        color=embed_color
                    )
                    send_tasks.append(channel.send(embed=embed))

                    if member_goal and current_count >= member_goal and remaining == 0:
                        celebrate_embed = discord.Embed(
                            description=f"{guild.name} has reached the goal of **{member_goal}** members! 🎉",
                            color=discord.Color.gold()
                        )
                        send_tasks.append(channel.send(embed=celebrate_embed))
                        guilds_to_deactivate.append(guild_id)

                except discord.Forbidden:
                    continue
                except Exception as e:
                    print(f"Error monitoring guild {guild_id}: {e}")
                    continue

            if send_tasks:
                await asyncio.gather(*send_tasks, return_exceptions=True)

            if bulk_last_count_updates or guilds_to_deactivate:
                async with self.lock:
                    async with self.get_db_connection() as db:

                        if bulk_last_count_updates:
                            await db.executemany('''
                                UPDATE member_tracker
                                SET last_member_count = ?
                                WHERE guild_id = ?
                            ''', bulk_last_count_updates)

                        if guilds_to_deactivate:
                            await db.executemany('''
                                UPDATE member_tracker
                                SET is_active = 0
                                WHERE guild_id = ?
                            ''', [(gid,) for gid in guilds_to_deactivate])

                        await db.commit()

                for last_count, guild_id in bulk_last_count_updates:
                    self._active_guild_counts[guild_id] = last_count

                for guild_id in guilds_to_deactivate:
                    self._active_guild_counts.pop(guild_id, None)

        except Exception as e:
            print(f"Error in member_count_monitor task: {e}")

    @member_count_monitor.before_loop
    async def before_member_count_monitor(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(MemberCountTracker(bot))
