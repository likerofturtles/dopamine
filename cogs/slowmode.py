import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiosqlite
import asyncio
from typing import Optional, List, Tuple
from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta
import pytz
import re

from config import SSDB_PATH
from utils.checks import slash_mod_check

SLOWMODE_INTERVALS = {
    "5 seconds": 5, "10 seconds": 10, "15 seconds": 15, "30 seconds": 30,
    "1 minute": 60, "2 minutes": 120, "5 minutes": 300, "10 minutes": 600,
    "15 minutes": 900, "30 minutes": 1800,
    "1 hour": 3600, "2 hours": 7200, "6 hours": 21600
}

COMMON_TIMEZONES = [
    "UTC", "US/Pacific", "US/Mountain", "US/Central", "US/Eastern",
    "Canada/Atlantic", "Europe/London", "Europe/Paris", "Europe/Berlin",
    "Europe/Moscow", "Asia/Dubai", "Asia/Kolkata", "Asia/Bangkok",
    "Asia/Singapore", "Asia/Tokyo", "Asia/Sydney", "Australia/Melbourne"
]


class ConfirmDeleteView(discord.ui.View):
    def __init__(self, interaction: discord.Interaction):
        super().__init__(timeout=60)
        self.interaction = interaction
        self.value = None

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.interaction.user.id:
            await interaction.response.send_message("This isn't your confirmation!", ephemeral=True)
            return
        self.value = False
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.interaction.user.id:
            await interaction.response.send_message("This isn't your confirmation!", ephemeral=True)
            return
        self.value = True
        self.stop()
        await interaction.response.defer()




class ScheduledSlowmode(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.lock = asyncio.Lock()

        self._db_pool = []
        self._pool_lock = asyncio.Lock()
        self._pool_semaphore: Optional[asyncio.Semaphore] = None
        self._max_pool_size = 5

        self._schedule_cache = {}

    async def cog_load(self):
        await self.init_db()
        await self.load_cache()
        if not self._db_keepalive.is_running():
            self._db_keepalive.start()
        if not self.slowmode_monitor.is_running():
            self.slowmode_monitor.start()

    async def cog_unload(self):
        if self._db_keepalive.is_running():
            self._db_keepalive.cancel()
        if self.slowmode_monitor.is_running():
            self.slowmode_monitor.cancel()
        for conn in self._db_pool:
            try:
                await conn.close()
            except Exception:
                pass
        self._db_pool.clear()

    async def _init_db_pool(self):
        async with self._pool_lock:
            if self._db_pool:
                return
            created_conns = []
            for _ in range(self._max_pool_size):
                try:
                    conn = await aiosqlite.connect(SSDB_PATH, timeout=5.0)
                    await conn.execute("PRAGMA busy_timeout=5000")
                    await conn.execute("PRAGMA journal_mode=WAL")
                    await conn.execute("PRAGMA synchronous=NORMAL")
                    await conn.commit()
                    created_conns.append(conn)
                except Exception:
                    pass
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
                await (await db.execute("SELECT 1")).close()
        except Exception:
            pass

    async def init_db(self):
        async with self.get_db_connection() as db:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS slowmode_schedules (
                    id INTEGER PRIMARY KEY,
                    guild_id INTEGER,
                    channel_id INTEGER,
                    delay_seconds INTEGER,
                    start_min_utc INTEGER,
                    end_min_utc INTEGER
                )
            ''')
            await db.execute('CREATE INDEX IF NOT EXISTS idx_slow_channel ON slowmode_schedules(channel_id)')
            await db.commit()

    async def load_cache(self):
        """Loads all schedules into memory for fast access."""
        self._schedule_cache.clear()
        async with self.get_db_connection() as db:
            cursor = await db.execute(
                'SELECT channel_id, start_min_utc, end_min_utc, delay_seconds FROM slowmode_schedules')
            rows = await cursor.fetchall()
            await cursor.close()

        for cid, start, end, delay in rows:
            if cid not in self._schedule_cache:
                self._schedule_cache[cid] = []
            self._schedule_cache[cid].append((start, end, delay))

    async def check_vote_access(self, user_id: int) -> bool:
        voter_cog = self.bot.get_cog('TopGGVoter')
        if not voter_cog: return True
        return await voter_cog.check_vote_access(user_id)

    def parse_time_str(self, time_str: str) -> Optional[time]:
        """Parses hh:mm or hh:mm AM/PM (case insensitive)."""
        time_str = time_str.strip().upper()
        formats = ["%H:%M", "%I:%M %p", "%I:%M%p"]
        for fmt in formats:
            try:
                return datetime.strptime(time_str, fmt).time()
            except ValueError:
                continue
        return None

    def get_utc_minutes(self, user_time: time, tz_name: str) -> int:
        """Converts user wall clock time + timezone -> Minutes from midnight UTC."""
        tz = pytz.timezone(tz_name)
        now_user_tz = datetime.now(tz)
        target_dt = tz.localize(datetime.combine(now_user_tz.date(), user_time))
        target_utc = target_dt.astimezone(pytz.UTC)
        return target_utc.hour * 60 + target_utc.minute

    def get_schedule_minutes_set(self, start: int, end: int) -> set:
        """Returns a set of minutes (0-1439) covered by the schedule."""
        if start < end:
            return set(range(start, end))
        else:
            return set(range(start, 1440)) | set(range(0, end))

    def format_frequency(self, seconds: int) -> str:
        """Convert seconds to human readable format showing hours, minutes, and seconds"""
        if seconds < 0:
            return "0 seconds"

        hours, remainder = divmod(seconds, 3600)
        minutes, remaining_seconds = divmod(remainder, 60)

        parts = []
        if hours > 0:
            parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
        if minutes > 0:
            parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
        if remaining_seconds > 0:
            parts.append(f"{remaining_seconds} second{'s' if remaining_seconds != 1 else ''}")

        if not parts:
            return "0 seconds"
        elif len(parts) == 1:
            return parts[0]
        else:
            return f"{', '.join(parts[:-1])} and {parts[-1]}"

    slowmode_group = app_commands.Group(name="slowmode", description="Manage scheduled slowmode")
    schedule_group = app_commands.Group(name="schedule", description="Configure slowmode schedules",
                                        parent=slowmode_group)

    async def timezone_autocomplete(self, interaction: discord.Interaction, current: str) -> List[
        app_commands.Choice[str]]:
        choices = [
            app_commands.Choice(name=tz, value=tz)
            for tz in COMMON_TIMEZONES if current.lower() in tz.lower()
        ]
        return choices[:25]

    async def interval_autocomplete(self, interaction: discord.Interaction, current: str) -> List[
        app_commands.Choice[int]]:
        return [
            app_commands.Choice(name=name, value=sec)
            for name, sec in SLOWMODE_INTERVALS.items() if current.lower() in name.lower()
        ][:25]

    async def interval_autocomplete_with_disable(self, interaction: discord.Interaction, current: str) -> List[
        app_commands.Choice[int]]:
        choices = [app_commands.Choice(name="Disable", value=0)]
        choices.extend([
            app_commands.Choice(name=name, value=sec)
            for name, sec in SLOWMODE_INTERVALS.items() if current.lower() in name.lower()
        ])
        return choices[:25]

    @slowmode_group.command(name="configure", description="Directly configure slowmode for a channel.")
    @app_commands.describe(
        channel="The channel to configure slowmode for",
        interval="The slowmode delay interval (or Disable)"
    )
    @app_commands.autocomplete(interval=interval_autocomplete_with_disable)
    @app_commands.check(slash_mod_check)
    async def configure_slowmode(
            self,
            interaction: discord.Interaction,
            channel: discord.TextChannel,
            interval: int
    ):
        try:
            await channel.edit(slowmode_delay=interval)
            formatted_interval = self.format_frequency(interval) if interval > 0 else "Disabled"
            embed = discord.Embed(
                title="Slowmode Configured",
                description=f"Slowmode for {channel.mention} has been set to **{formatted_interval}**.",
                color=discord.Color.green()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.Forbidden:
            embed = discord.Embed(
                title="Permission Denied",
                description="I do not have permission to set slowmode in that channel.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            embed = discord.Embed(
                title="An Error Occurred",
                description=f"An unexpected error occurred: {e}",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @schedule_group.command(name="start", description="Schedule a slowmode for this channel")
    @app_commands.describe(
        channel="The channel to apply slowmode to",
        interval="The slowmode delay interval",
        timezone="Your timezone region",
        start_time="Start time (e.g., 14:00 or 02:00 PM)",
        end_time="End time (e.g., 18:00 or 06:00 PM)"
    )
    @app_commands.autocomplete(timezone=timezone_autocomplete, interval=interval_autocomplete)
    @app_commands.check(slash_mod_check)
    async def schedule_start(
            self, interaction: discord.Interaction,
            channel: discord.TextChannel,
            interval: int,
            timezone: str,
            start_time: str,
            end_time: str
    ):
        if not await self.check_vote_access(interaction.user.id):
            return await interaction.response.send_message(
                embed=discord.Embed(title="Vote to Use This Feature!", description=f"This command requires voting! To access this feature, please vote for Dopamine [__here__](https://top.gg/bot/{self.bot.user.id}).",
                                    color=0xffaa00), ephemeral=True)

        if timezone not in pytz.all_timezones:
            return await interaction.response.send_message(embed=discord.Embed(title="Invalid Timezone",
                                                                               description="Please select a valid timezone from the list.",
                                                                               color=discord.Color.red()),
                                                           ephemeral=True)

        t_start = self.parse_time_str(start_time)
        t_end = self.parse_time_str(end_time)

        if not t_start or not t_end:
            return await interaction.response.send_message(embed=discord.Embed(title="Invalid Time Format",
                                                                               description="Use `HH:MM` (24h) or `HH:MM AM/PM` (12h).",
                                                                               color=discord.Color.red()),
                                                           ephemeral=True)

        if t_start == t_end:
            return await interaction.response.send_message(
                embed=discord.Embed(title="Invalid Duration", description="Start and End time cannot be the same.",
                                    color=discord.Color.red()), ephemeral=True)

        utc_start = self.get_utc_minutes(t_start, timezone)
        utc_end = self.get_utc_minutes(t_end, timezone)

        new_range = self.get_schedule_minutes_set(utc_start, utc_end)

        existing_schedules = self._schedule_cache.get(channel.id, [])
        for ex_start, ex_end, _ in existing_schedules:
            ex_range = self.get_schedule_minutes_set(ex_start, ex_end)
            if not new_range.isdisjoint(ex_range):
                return await interaction.response.send_message(embed=discord.Embed(title="Schedule Conflict",
                                                                                   description=f"This time overlaps with an existing schedule in {channel.mention}.",
                                                                                   color=discord.Color.red()),
                                                               ephemeral=True)

        async with self.lock:
            async with self.get_db_connection() as db:
                await db.execute('''
                    INSERT INTO slowmode_schedules (guild_id, channel_id, delay_seconds, start_min_utc, end_min_utc)
                    VALUES (?, ?, ?, ?, ?)
                ''', (interaction.guild.id, channel.id, interval, utc_start, utc_end))
                await db.commit()

        if channel.id not in self._schedule_cache:
            self._schedule_cache[channel.id] = []
        self._schedule_cache[channel.id].append((utc_start, utc_end, interval))

        formatted_interval = self.format_frequency(interval)
        embed = discord.Embed(title="Slowmode Scheduled", color=discord.Color.green())
        embed.description = f"**Channel:** {channel.mention}\n**Slowmode Setting:** {formatted_interval}\n**Time:** {start_time} to {end_time} ({timezone})"
        embed.set_footer(text="The slowmode will be automatically started at the scheduled time.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @schedule_group.command(name="delete", description="Delete all slowmode schedules for a channel")
    @app_commands.describe(channel="The channel to clear schedules for")
    @app_commands.check(slash_mod_check)
    async def schedule_delete(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if channel.id not in self._schedule_cache or not self._schedule_cache[channel.id]:
            return await interaction.response.send_message(embed=discord.Embed(title="No Schedules",
                                                                               description=f"No active schedules found for {channel.mention}.",
                                                                               color=discord.Color.red()),
                                                           ephemeral=True)

        embed = discord.Embed(
            title="Confirm Deletion",
            description=f"Are you sure you want to delete **ALL** slowmode schedules for {channel.mention}?\nThis will also disable any currently active scheduled slowmode.",
            color=discord.Color.red()
        )

        view = ConfirmDeleteView(interaction)

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        await view.wait()

        try:
            await interaction.edit_original_response(view=None, embed=discord.Embed(
                title="Successfully Deleted",
                description=f"Are you sure you want to delete **ALL** slowmode schedules for {channel.mention}?\nThis will also disable any currently active scheduled slowmode.",
                color=discord.Color.green()
            ))
        except:
            pass

        if view.value:
            async with self.lock:
                async with self.get_db_connection() as db:
                    await db.execute("DELETE FROM slowmode_schedules WHERE channel_id = ?", (channel.id,))
                    await db.commit()

            if channel.id in self._schedule_cache:
                del self._schedule_cache[channel.id]

            try:
                await channel.edit(slowmode_delay=0)
            except:
                pass

            success_embed = discord.Embed(
                title="Successfully Deleted",
                description=f"Are you sure you want to delete **ALL** slowmode schedules for {channel.mention}?\nThis will also disable any currently active scheduled slowmode.",
                color=discord.Color.green()
            )
            await interaction.edit_original_response(view=None, embed=success_embed)

        else:
            cancel_embed = discord.Embed(
                title="Timed Out",
                description=f"~~Are you sure you want to delete **ALL** slowmode schedules for {channel.mention}?\nThis will also disable any currently active scheduled slowmode.~~",
                color=discord.Color.red()
            )
            await interaction.edit_original_response(embed=cancel_embed)

    @tasks.loop(seconds=60)
    async def slowmode_monitor(self):
        """Checks every minute to apply/remove slowmodes."""
        now_utc = datetime.now(pytz.UTC)
        current_minutes = now_utc.hour * 60 + now_utc.minute

        for channel_id, schedules in list(self._schedule_cache.items()):
            target_delay = 0

            for start, end, delay in schedules:
                if start < end:
                    if start <= current_minutes < end:
                        target_delay = delay
                        break
                else:
                    if current_minutes >= start or current_minutes < end:
                        target_delay = delay
                        break

            try:
                channel = self.bot.get_channel(channel_id)
                if not channel:
                    continue

                if channel.slowmode_delay != target_delay:
                    await channel.edit(slowmode_delay=target_delay)

            except (discord.Forbidden, discord.NotFound):
                continue
            except Exception as e:
                print(f"Error in slowmode monitor for channel {channel_id}: {e}")

    @slowmode_monitor.before_loop
    async def before_monitor(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(ScheduledSlowmode(bot))
