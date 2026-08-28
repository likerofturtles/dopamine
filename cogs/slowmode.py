from __future__ import annotations

import asyncio
from datetime import datetime, time
from typing import List, Dict, Tuple, Optional

import discord
import pytz
from beacon import PrivateLayoutView, beacon_commands, preconditions
from discord import app_commands
from discord.app_commands import Choice
from discord.ext import commands, tasks

from utils.data_handlers import export_table
from utils.data_protocol import DataDeleteResult, DataExportChunk, DataFeatureMeta, DataMonitorResult

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


class DestructiveConfirmationView(PrivateLayoutView):
    def __init__(self, title_text: str, body_text: str, color: discord.Color | None = None):
        super().__init__(timeout=30)
        self.value: bool | None = None
        self.title_text = title_text
        self.body_text = body_text
        self.color = color
        self.message: discord.Message | None = None
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container(accent_color=self.color)
        container.add_item(discord.ui.TextDisplay(f"### {self.title_text}"))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(self.body_text))

        is_disabled = self.value is not None
        action_row = discord.ui.ActionRow()
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.gray, disabled=is_disabled)
        confirm = discord.ui.Button(label="Delete Permanently", style=discord.ButtonStyle.red, disabled=is_disabled)

        cancel.callback = self.cancel_callback
        confirm.callback = self.confirm_callback

        action_row.add_item(cancel)
        action_row.add_item(confirm)
        container.add_item(discord.ui.Separator())
        container.add_item(action_row)

        self.add_item(container)

    async def update_view(self, interaction: discord.Interaction, title: str, color: discord.Color):
        self.title_text = title
        self.body_text = f"~~{self.body_text}~~"
        self.color = color
        self.build_layout()

        if interaction.response.is_done():
            await interaction.edit_original_response(view=self)
        else:
            await interaction.response.edit_message(view=self)
        self.stop()

    async def cancel_callback(self, interaction: discord.Interaction):
        self.value = False
        await self.update_view(interaction, "Action Canceled", discord.Color(0xdf5046))

    async def confirm_callback(self, interaction: discord.Interaction):
        self.value = True
        await self.update_view(interaction, "Action Confirmed", discord.Color.green())

    async def on_timeout(self, interaction: discord.Interaction):
        if self.value is None and self.message:
            self.value = False
            await self.update_view(interaction, "Timed Out", discord.Color(0xdf5046))


class ScheduledSlowmode(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._schedule_cache: Dict[int, List[Tuple[int, int, int]]] = {}
        self.lock = asyncio.Lock()

    async def cog_load(self):
        await self.bot.db.wait_ready()
        await self.populate_caches()
        if not self.slowmode_monitor.is_running():
            self.slowmode_monitor.start()

    async def cog_unload(self):
        if self.slowmode_monitor.is_running():
            self.slowmode_monitor.cancel()

    async def populate_caches(self):
        self._schedule_cache.clear()
        rows = await self.bot.db.execute(
            'SELECT channel_id, start_min_utc, end_min_utc, delay_seconds FROM slowmode_schedules'
        )
        for row in rows:
            cid = row["channel_id"]
            if cid not in self._schedule_cache:
                self._schedule_cache[cid] = []
            self._schedule_cache[cid].append(
                (row["start_min_utc"], row["end_min_utc"], row["delay_seconds"])
            )

    async def check_vote_access(self, user_id: int) -> bool:
        voter_cog = self.bot.get_cog('TopGGVoter')
        if not voter_cog:
            return True
        return await voter_cog.check_vote_access(user_id)

    def parse_time_str(self, time_str: str) -> time | None:
        time_str = time_str.strip().upper()
        formats = ["%H:%M", "%I:%M %p", "%I:%M%p"]
        for fmt in formats:
            try:
                return datetime.strptime(time_str, fmt).time()
            except ValueError:
                continue
        return None

    def get_utc_minutes(self, user_time: time, tz_name: str) -> int:
        tz = pytz.timezone(tz_name)
        now_user_tz = datetime.now(tz)
        target_dt = tz.localize(datetime.combine(now_user_tz.date(), user_time))
        target_utc = target_dt.astimezone(pytz.UTC)
        return target_utc.hour * 60 + target_utc.minute

    def get_schedule_minutes_set(self, start: int, end: int) -> set:
        if start < end:
            return set(range(start, end))
        else:
            return set(range(start, 1440)) | set(range(0, end))

    def format_frequency(self, seconds: int) -> str:
        if seconds < 0: return "0 seconds"
        hours, remainder = divmod(seconds, 3600)
        minutes, remaining_seconds = divmod(remainder, 60)
        parts = []
        if hours > 0: parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
        if minutes > 0: parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
        if remaining_seconds > 0: parts.append(f"{remaining_seconds} second{'s' if remaining_seconds != 1 else ''}")
        if not parts:
            return "0 seconds"
        elif len(parts) == 1:
            return parts[0]
        else:
            return f"{', '.join(parts[:-1])} and {parts[-1]}"

    async def timezone_autocomplete(self, interaction: discord.Interaction, current: str) -> list[
        Choice[str | int | float]]:
        return [app_commands.Choice(name=tz, value=tz) for tz in COMMON_TIMEZONES if current.lower() in tz.lower()][:25]

    async def interval_autocomplete(self, interaction: discord.Interaction, current: str) -> list[
        Choice[str | int | float]]:
        return [app_commands.Choice(name=name, value=sec) for name, sec in SLOWMODE_INTERVALS.items() if
                current.lower() in name.lower()][:25]

    async def interval_autocomplete_with_disable(self, interaction: discord.Interaction, current: str) -> List[
        app_commands.Choice[int]]:
        choices = [app_commands.Choice(name="Disable", value=0)]
        choices.extend([app_commands.Choice(name=name, value=sec) for name, sec in SLOWMODE_INTERVALS.items() if
                        current.lower() in name.lower()])
        return choices[:25]

    slowmode_group = app_commands.Group(name="slowmode", description="Manage scheduled slowmode")
    schedule_group = beacon_commands.Group(name="schedule", description="Configure slowmode schedules",
                                           parent=slowmode_group, permissions_preset="manager")

    @slowmode_group.command(name="configure", description="Directly configure slowmode for a channel.")
    @app_commands.describe(channel="The channel to configure slowmode for",
                           interval="The slowmode delay interval (or Disable)")
    @app_commands.autocomplete(interval=interval_autocomplete_with_disable)
    @preconditions.has_permissions(manage_channels=True)
    async def configure_slowmode(self, interaction: discord.Interaction, channel: discord.TextChannel, interval: int):
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
            await interaction.response.send_message(embed=discord.Embed(title="Permission Denied",
                                                                        description="I do not have permission to set slowmode in that channel.",
                                                                        color=discord.Color.red()), ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(
                embed=discord.Embed(title="An Error Occurred", description=f"An unexpected error occurred: {e}",
                                    color=discord.Color.red()), ephemeral=True)

    @schedule_group.command(name="start", description="Schedule a slowmode for this channel")
    @app_commands.describe(channel="The channel to apply slowmode to", interval="The slowmode delay interval",
                           timezone="Your timezone region", start_time="Start time (e.g., 14:00 or 02:00 PM)",
                           end_time="End time (e.g., 18:00 or 06:00 PM)")
    @app_commands.autocomplete(timezone=timezone_autocomplete, interval=interval_autocomplete)
    @preconditions.has_permissions(manage_channels=True)
    async def schedule_start(self, interaction: discord.Interaction, channel: discord.TextChannel, interval: int,
                             timezone: str, start_time: str, end_time: str):
        if not await self.check_vote_access(interaction.user.id):
            return await interaction.response.send_message(embed=discord.Embed(title="Vote to Use This Feature!",
                                                                               description=f"This command requires voting! To access this feature, please vote for Dopamine [__here__](https://top.gg/bot/{self.bot.user.id}).",
                                                                               color=0xffaa00), ephemeral=True)

        if timezone not in pytz.all_timezones:
            return await interaction.response.send_message(embed=discord.Embed(title="Invalid Timezone",
                                                                               description="Please select a valid timezone from the list.",
                                                                               color=discord.Color.red()),
                                                           ephemeral=True)

        t_start, t_end = self.parse_time_str(start_time), self.parse_time_str(end_time)
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
            if not new_range.isdisjoint(self.get_schedule_minutes_set(ex_start, ex_end)):
                return await interaction.response.send_message(embed=discord.Embed(title="Schedule Conflict",
                                                                                   description=f"This time overlaps with an existing schedule in {channel.mention}.",
                                                                                   color=discord.Color.red()),
                                                               ephemeral=True)

        async with self.lock:
            await self.bot.db.execute('''
                INSERT INTO slowmode_schedules (guild_id, channel_id, delay_seconds, start_min_utc, end_min_utc)
                VALUES (?, ?, ?, ?, ?)
            ''', (interaction.guild.id, channel.id, interval, utc_start, utc_end))

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
    @preconditions.has_permissions(manage_channels=True)
    async def schedule_delete(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if channel.id not in self._schedule_cache or not self._schedule_cache[channel.id]:
            return await interaction.response.send_message(embed=discord.Embed(title="No Schedules",
                                                                               description=f"No active schedules found for {channel.mention}.",
                                                                               color=discord.Color.red()),
                                                           ephemeral=True)

        body_content = f"Are you sure you want to delete **ALL** slowmode schedules for {channel.mention}?\nThis will also disable any currently active scheduled slowmode."
        view = DestructiveConfirmationView("Pending Confirmation", body_content)
        await interaction.response.send_message(view=view, ephemeral=True)
        view.message = await interaction.original_response()
        await view.wait()

        if view.value is True:
            async with self.lock:
                await self.bot.db.execute(
                    "DELETE FROM slowmode_schedules WHERE channel_id = ?", (channel.id,)
                )

                if channel.id in self._schedule_cache:
                    del self._schedule_cache[channel.id]

            try:
                await channel.edit(slowmode_delay=0)
            except Exception:
                pass

    @tasks.loop(seconds=60)
    async def slowmode_monitor(self):
        await self.bot.db.wait_ready()
        now_utc = datetime.now(pytz.UTC)
        current_minutes = now_utc.hour * 60 + now_utc.minute

        for channel_id, schedules in list(self._schedule_cache.items()):
            target_delay = 0
            for start, end, delay in schedules:
                if (start < end and start <= current_minutes < end) or (
                        start > end and (current_minutes >= start or current_minutes < end)):
                    target_delay = delay
                    break

            channel = None
            try:
                channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
                if channel and channel.slowmode_delay != target_delay:
                    await channel.edit(slowmode_delay=target_delay)
            except (discord.Forbidden, discord.NotFound):
                from utils.discord_health import report_access_failure
                ch = self.bot.get_channel(channel_id)
                gid = ch.guild.id if ch else None
                if gid is None:
                    for g in self.bot.guilds:
                        if g.get_channel(channel_id):
                            gid = g.id
                            break
                if gid:
                    await report_access_failure(self.bot, gid, "slowmode", str(channel_id))
                continue
            except Exception as e:
                from utils.discord_health import is_access_error, report_access_failure
                if is_access_error(e) and isinstance(channel, discord.abc.GuildChannel):
                    await report_access_failure(self.bot, channel.guild.id, "slowmode", str(channel_id))

    @slowmode_monitor.before_loop
    async def before_monitor(self):
        await self.bot.wait_until_ready()

    def data_features(self) -> list[DataFeatureMeta]:
        return [DataFeatureMeta(
            feature_id="slowmode",
            name="Scheduled Slowmode",
            guild_export=True,
            guild_delete=True,
        )]

    async def data_export_user(self, user_id: int, *, guild_ids: list[int] | None) -> DataExportChunk:
        return DataExportChunk(feature_id="slowmode")

    async def data_export_guild(self, guild_id: int) -> DataExportChunk:
        chunk = DataExportChunk(feature_id="slowmode")
        rows = await export_table(
            self.bot.db, "SELECT * FROM slowmode_schedules WHERE guild_id = ?", (guild_id,)
        )
        chunk.guild_data[guild_id] = {"slowmode_schedules": rows}
        return chunk

    async def data_delete_user(self, user_id: int, *, guild_ids: list[int] | None,
                               feature_id: str | None) -> DataDeleteResult:
        return DataDeleteResult(feature_id="slowmode")

    async def data_delete_guild(self, guild_id: int, feature_id: str | None) -> DataDeleteResult:
        if feature_id and feature_id != "slowmode":
            return DataDeleteResult(feature_id="slowmode")

        async with self.bot.db.acquire_db() as db:
            rows = await db.execute(
                "SELECT DISTINCT channel_id FROM slowmode_schedules WHERE guild_id = ?", (guild_id,)
            )
            channel_ids = [row["channel_id"] for row in rows]
            rows_affected = await db.execute(
                "DELETE FROM slowmode_schedules WHERE guild_id = ?", (guild_id,)
            )
            await db.commit()

        for cid in channel_ids:
            self._schedule_cache.pop(cid, None)

        return DataDeleteResult(feature_id="slowmode", deleted=True, rows_affected=rows_affected)

    async def _channel_manageable(self, guild: discord.Guild, channel_id: int) -> bool:
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return False
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild.id:
            return False
        perms = channel.permissions_for(guild.me)
        return perms.view_channel and perms.manage_channels

    async def data_monitor_guild(self, guild: discord.Guild) -> DataMonitorResult:
        result = DataMonitorResult(feature_id="slowmode")
        rows = await self.bot.db.execute(
            "SELECT DISTINCT channel_id FROM slowmode_schedules WHERE guild_id = ?", (guild.id,)
        )
        channel_ids = [row["channel_id"] for row in rows]

        unmanageable_ids = [cid for cid in channel_ids if not await self._channel_manageable(guild, cid)]

        if unmanageable_ids:
            async with self.bot.db.acquire_db() as db:
                for channel_id in unmanageable_ids:
                    await db.execute(
                        "DELETE FROM slowmode_schedules WHERE guild_id = ? AND channel_id = ?",
                        (guild.id, channel_id),
                    )
                    self._schedule_cache.pop(channel_id, None)
                    result.actions.append(f"removed_schedules:{channel_id}")
                await db.commit()

        return result


async def setup(bot):
    await bot.add_cog(ScheduledSlowmode(bot))