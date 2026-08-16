import re
import time
from datetime import timedelta
from typing import Dict, Optional, List, Any, Union

import aiosqlite
import discord
from beacon import PrivateLayoutView, beacon_commands
from discord import app_commands
from discord.ext import commands, tasks
from natsort import natsorted, ns

from utils.data_handlers import export_table
from utils.data_protocol import DataDeleteResult, DataExportChunk, DataFeatureMeta, DataMonitorResult
from utils.discord_health import is_access_error, report_access_failure, resolve_guild_channel, channel_can_send
from utils.log import LoggingManager

DELETE_OPTIONS = {
        "Off": 0,
        "Past 1 Day": 1,
        "Past 3 Days": 3,
        "Past 7 Days": 7
    }

def parse_duration(duration_str: str) -> Optional[int]:
    if not duration_str or duration_str.lower() in ["permanent", "perm", "0", "infinite"]:
        return 0

    match = re.match(r"(\d+)\s*(m|h|d|w|mo|min|minute|hour|day|week|month)s?", duration_str.lower())
    if not match:
        return None

    amount = int(match.group(1))
    unit = match.group(2)

    multipliers = {
        'm': 60, 'min': 60, 'minute': 60,
        'h': 3600, 'hour': 3600,
        'd': 86400, 'day': 86400,
        'w': 604800, 'week': 604800,
        'mo': 2592000, 'month': 2592000
    }

    seconds = amount * multipliers.get(unit, 0)
    if seconds > 0 and (seconds < 900 or seconds > 31536000):
        return None

    return seconds


def format_duration_str(seconds: int) -> str:
    if seconds == 0:
        return "Permanent"

    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} Minute{'s' if minutes != 1 else ''}"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} Hour{'s' if hours != 1 else ''}"
    days = hours // 24
    if days < 7:
        return f"{days} Day{'s' if days != 1 else ''}"
    weeks = days // 7
    if weeks < 4:
        return f"{weeks} Week{'s' if weeks != 1 else ''}"
    months = days // 30
    return f"{months} Month{'s' if months != 1 else ''}"


def format_punishment_label(action: Optional[str], duration: Optional[Union[timedelta, int]] = None) -> str:
    if not action:
        return "No punishment (No threshold reached)"

    dur_seconds = None
    if duration is not None:
        dur_seconds = int(duration.total_seconds()) if isinstance(duration, timedelta) else int(duration)

    if action == "ban" and not dur_seconds:
        return "banned permanently"
    if action == "ban" and dur_seconds:
        return f"banned for {format_duration_str(dur_seconds)}"
    if dur_seconds:
        return f"{action} for {format_duration_str(dur_seconds)}"
    if action == "warning":
        return "warned"
    return f"{action}ed from the server."


class ConfirmationView(PrivateLayoutView):
    def __init__(self, user, cog, title_text: str, body_text: str, color: discord.Color = None):
        super().__init__(user, timeout=30)
        self.value = None
        self.cog = cog
        self.title_text = title_text
        self.body_text = body_text
        self.color = color
        self.message: discord.Message = None
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container(accent_color=self.color)
        container.add_item(discord.ui.TextDisplay(f"### {self.title_text}"))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(self.body_text))

        if self.value is None:
            action_row = discord.ui.ActionRow()
            cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.red)
            confirm = discord.ui.Button(label="Confirm", style=discord.ButtonStyle.green)

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
            await self.update_view(interaction, "Timed Out", discord.Color(0xdf5046))
            self.stop()

class DestructiveConfirmationView(PrivateLayoutView):
    def __init__(self, user, cog, title_text: str, body_text: str, color: discord.Color = None):
        super().__init__(user, timeout=30)
        self.value = None
        self.cog = cog
        self.title_text = title_text
        self.body_text = body_text
        self.color = color
        self.message: discord.Message = None
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container(accent_color=self.color)
        container.add_item(discord.ui.TextDisplay(f"### {self.title_text}"))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(self.body_text))

        if self.value is None:
            action_row = discord.ui.ActionRow()
            cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
            confirm = discord.ui.Button(label="Confirm", style=discord.ButtonStyle.danger)

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
            await self.update_view(interaction, "Timed Out", discord.Color(0xdf5046))
            self.stop()


class CaseDeleteConfirmationView(PrivateLayoutView):
    def __init__(self, user, cog, guild: discord.Guild, case: dict, term: str, body_text: str, title: str):
        super().__init__(user, timeout=60)
        self.value = None
        self.cog = cog
        self.guild = guild
        self.case = case
        self.term = term
        self.body_text = body_text
        self.title = title
        self.message: discord.Message = None
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container(accent_color=discord.Color(0xdf5046))
        container.add_item(discord.ui.TextDisplay(f"### {self.title}"))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(self.body_text))

        if self.value is None:
            term_cap = self.term.title()
            action_row = discord.ui.ActionRow()
            cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
            points_only = discord.ui.Button(
                label=f"Adjust {term_cap}s Only",
                style=discord.ButtonStyle.danger,
            )
            reverse = discord.ui.Button(
                label=f"Adjust {term_cap}s and Reverse Punishment",
                style=discord.ButtonStyle.danger,
            )

            cancel.callback = self.cancel_callback
            points_only.callback = self.points_only_callback
            reverse.callback = self.reverse_callback

            action_row.add_item(cancel)
            action_row.add_item(points_only)
            action_row.add_item(reverse)
            container.add_item(discord.ui.Separator())
            container.add_item(action_row)

        self.add_item(container)

    async def update_view(self, interaction: discord.Interaction, title: str, color: discord.Color):
        self.body_text = f"~~{self.body_text}~~"
        self.build_layout()

        if interaction.response.is_done():
            await interaction.edit_original_response(view=self)
        else:
            await interaction.response.edit_message(view=self)
        self.stop()

    async def cancel_callback(self, interaction: discord.Interaction):
        self.value = False
        await self.update_view(interaction, "Action Canceled", discord.Color(0xdf5046))

    async def points_only_callback(self, interaction: discord.Interaction):
        self.value = "points_only"
        await interaction.response.defer()
        await self.cog.execute_case_delete(interaction, self.guild, self.case, reverse=False)
        await self.update_view(interaction, "Case Deleted", discord.Color.green())

    async def reverse_callback(self, interaction: discord.Interaction):
        self.value = "reverse"
        await interaction.response.defer()
        await self.cog.execute_case_delete(interaction, self.guild, self.case, reverse=True)
        await self.update_view(interaction, "Case Deleted", discord.Color.green())

    async def on_timeout(self):
        if self.value is None and self.message:
            try:
                self.body_text = f"~~{self.body_text}~~"
                self.build_layout()
                await self.message.edit(view=self)
            except Exception:
                pass
            self.stop()


class UndoActionView(discord.ui.View):
    def __init__(self, cog, case_number: int, guild_id: int, expires_at: int):
        super().__init__(timeout=10)
        self.cog = cog
        self.case_number = case_number
        self.guild_id = guild_id
        self.expires_at = expires_at
        self.message: discord.Message = None

    @discord.ui.button(label="Undo", style=discord.ButtonStyle.secondary, custom_id="undo_action")
    async def undo_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.moderate_members:
            return await interaction.response.send_message("You lack permissions to undo this action.", ephemeral=True)

        now = int(time.time())
        if now > self.expires_at:
            return await interaction.response.send_message("This undo button has expired.", ephemeral=True)

        case = await self.cog.get_infraction(self.guild_id, self.case_number)
        if not case:
            return await interaction.response.send_message("Case not found.", ephemeral=True)
        
        await interaction.response.defer()
        await self.cog.execute_case_delete(interaction, interaction.guild, case, reverse=True)

        for item in self.children:
            item.disabled = True
        await interaction.edit_original_response(content=f"This action has been undone by {interaction.user.mention}.", embed=None, view=self)
        self.stop()

    async def on_timeout(self):
        await self.message.edit(view=None)
        self.stop()


class ActionModal(discord.ui.Modal):
    def __init__(self, cog, guild_id, is_create=True, existing_action_id=None):
        title = "Create New Action" if is_create else "Edit Action Points"
        super().__init__(title=title)
        self.cog = cog
        self.guild_id = guild_id
        self.is_create = is_create
        self.existing_action_id = existing_action_id

        settings = self.cog.settings_cache.get(guild_id, {})
        is_simple = settings.get("simple_mode", 0) == 1
        term = "Warnings" if is_simple else "Points"

        if self.is_create:
            self.action_type = discord.ui.TextInput(
                label="Action (warning, timeout, kick, ban)",
                placeholder="timeout",
                min_length=3, max_length=10
            )
            self.duration = discord.ui.TextInput(
                label="Duration (e.g., 15m, 1h, 3 days)",
                placeholder="Leave empty for warning/kick/perm ban",
                required=False
            )
            self.add_item(self.action_type)
            self.add_item(self.duration)

        self.points = discord.ui.TextInput(
            label=f"Amount of {term} Required",
            placeholder="Enter a number (1-1000)",
            min_length=1, max_length=4
        )
        self.add_item(self.points)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            points_val = int(self.points.value)
            if not (1 <= points_val <= 1000): raise ValueError
        except ValueError:
            return await interaction.response.send_message("Points must be an integer between 1 and 1000.",
                                                           ephemeral=True)

        if self.is_create:
            act_type = self.action_type.value.lower().strip()
            if act_type not in ["warning", "warn", "timeout", "mute", "kick", "ban"]:
                return await interaction.response.send_message("Invalid action type.", ephemeral=True)

            if act_type == "warn": act_type = "warning"
            if act_type == "mute": act_type = "timeout"

            dur_seconds = 0
            if act_type in ["timeout", "ban"]:
                if self.duration.value:
                    dur_seconds = parse_duration(self.duration.value)
                    if dur_seconds is None:
                        return await interaction.response.send_message("Invalid duration format or range.",
                                                                       ephemeral=True)

            existing = self.cog.action_cache.get(self.guild_id, [])
            conflict = next((a for a in existing if a['points'] == points_val), None)

            if conflict:
                view = ConfirmationView(
                    interaction.user, self.cog,
                    "Point Conflict",
                    f"An action already exists at **{points_val}** points. Do you want to add this action anyway, triggering BOTH?"
                )
                await interaction.response.send_message(view=view)
                view.message = await interaction.original_response()
                await view.wait()
                if not view.value:
                    return

            async with self.cog.acquire_db() as db:
                await db.execute(
                    "INSERT INTO actions (guild_id, action_type, duration, points) VALUES (?, ?, ?, ?)",
                    (self.guild_id, act_type, dur_seconds, points_val)
                )
                await db.commit()

        else:
            async with self.cog.acquire_db() as db:
                await db.execute(
                    "UPDATE actions SET points = ? WHERE id = ? AND guild_id = ?",
                    (points_val, self.existing_action_id, self.guild_id)
                )
                await db.commit()

        await self.cog.refresh_action_cache(self.guild_id)

        view = CustomisationPage(interaction.user, self.cog)
        if interaction.response.is_done():
            await interaction.edit_original_response(view=view, content=None, embed=None)
        else:
            await interaction.response.edit_message(view=view, content=None, embed=None)


class SettingValueModal(discord.ui.Modal):
    def __init__(self, cog, setting_key):
        title_map = {"decay_interval": "Decay Frequency", "rejoin_points": "Rejoin Points"}
        super().__init__(title=f"Edit {title_map.get(setting_key, 'Setting')}")
        self.cog = cog
        self.setting_key = setting_key

        if setting_key == "decay_interval":
            self.value_input = discord.ui.TextInput(
                label="Frequency (e.g. 14 days, 2 weeks)",
                placeholder="0 to disable. Min 1 day / 24 hours.",
            )
        else:
            self.value_input = discord.ui.TextInput(
                label="Points Amount",
                placeholder="Type 'preserve' or a number (0-50)"
            )
        self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction):
        val = self.value_input.value.lower().strip()
        final_val = 0

        if self.setting_key == "decay_interval":
            if val == "0":
                final_val = 0
            else:
                seconds = parse_duration(val)
                if not seconds:
                    return await interaction.response.send_message("Invalid duration.", ephemeral=True)
                days = seconds // 86400
                if days < 1 or days > 100:
                    return await interaction.response.send_message(
                        "Decay must be between 1 and 100 days (or 0 to disable decay feature).", ephemeral=True)
                final_val = days
        elif self.setting_key == "rejoin_points":
            if val == "preserve":
                final_val = -1
            else:
                try:
                    final_val = int(val)
                    if not (0 <= final_val <= 50): raise ValueError
                except ValueError:
                    return await interaction.response.send_message("Invalid number (0-50) or 'preserve'.",
                                                                   ephemeral=True)

        guild_id = interaction.guild.id
        async with self.cog.acquire_db() as db:
            await db.execute(
                f"UPDATE settings SET {self.setting_key} = ? WHERE guild_id = ?",
                (final_val, guild_id)
            )
            await db.commit()

        if guild_id in self.cog.settings_cache:
            self.cog.settings_cache[guild_id][self.setting_key] = final_val
        else:
            await self.cog.populate_caches()

        view = SettingsPage(interaction.user, self.cog)
        await interaction.response.edit_message(view=view)


class MessageReportDashboard(PrivateLayoutView):
    def __init__(self, user, cog):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.build_layout()

    def build_layout(self):
        self.clear_items()

        guild_id = self.user.guild.id
        settings = self.cog.settings_cache.get(guild_id, {})
        enabled = settings.get("msg_report_enabled", 0) == 1
        channel_id = settings.get("msg_report_channel")
        roles_raw = settings.get("msg_report_roles")

        channel_str = f"<#{channel_id}>" if channel_id else "Not Set"
        roles_str = ", ".join([f"<@&{r}>" for r in roles_raw.split(",") if r]) if roles_raw else "Not Set"

        container = discord.ui.Container()
        container.add_item((discord.ui.TextDisplay("## Message Report Dashboard")))

        toggle_btn = discord.ui.Button(
            label=f"{'Disable' if enabled else 'Enable'}",
            style=discord.ButtonStyle.secondary if enabled else discord.ButtonStyle.primary
        )
        toggle_btn.callback = self.toggle_reporting

        container.add_item(discord.ui.Section(
            discord.ui.TextDisplay(
                "Message reports allow users to report any message directly to the moderators. Use this dashboard to configure it."),
            accessory=toggle_btn
        ))

        if enabled:
            container.add_item(discord.ui.Separator())

            channel_btn = discord.ui.Button(label="Edit Channel", style=discord.ButtonStyle.primary)
            channel_btn.callback = self.edit_channel
            container.add_item(discord.ui.Section(
                discord.ui.TextDisplay(f"* Channel where reported messages will be sent: {channel_str}"),
                accessory=channel_btn
            ))

            role_btn = discord.ui.Button(label="Edit Roles", style=discord.ButtonStyle.primary)
            role_btn.callback = self.edit_roles
            container.add_item(discord.ui.Section(
                discord.ui.TextDisplay(f"* Roles that will be pinged upon a report: {roles_str}"),
                accessory=role_btn
            ))

            container.add_item(discord.ui.Separator())

            test_btn = discord.ui.Button(label="Send Test Message", style=discord.ButtonStyle.primary)
            test_btn.callback = self.send_test_message
            container.add_item(discord.ui.Section(
                discord.ui.TextDisplay("* Click the button to send a test message to the chosen channel."),
                accessory=test_btn
            ))

        container.add_item(discord.ui.Separator())
        return_btn = discord.ui.Button(emoji=self.cog.bot.back_emoji, label="Back", style=discord.ButtonStyle.secondary)
        return_btn.callback = self.return_home

        container.add_item(discord.ui.ActionRow(return_btn))
        self.add_item(container)

    async def toggle_reporting(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        settings = self.cog.settings_cache.get(guild_id, {})
        current_state = settings.get("msg_report_enabled", 0) == 1
        new_state = 0 if current_state else 1

        channel_id = settings.get("msg_report_channel")
        roles_raw = settings.get("msg_report_roles")

        async with self.cog.acquire_db() as db:
            await db.execute("UPDATE settings SET msg_report_enabled = ? WHERE guild_id = ?", (new_state, guild_id))
            await db.commit()
        self.cog.settings_cache[guild_id]["msg_report_enabled"] = new_state

        if new_state == 1:
            if not channel_id:
                return await interaction.response.edit_message(view=ChannelSelect(self.user, self.cog, firsttime=1))
            elif not roles_raw:
                return await interaction.response.edit_message(view=RoleSelect(self.user, self.cog, firsttime=1))

        self.build_layout()
        await interaction.response.edit_message(view=self)

    async def edit_channel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=ChannelSelect(self.user, self.cog))

    async def edit_roles(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=RoleSelect(self.user, self.cog))

    async def send_test_message(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        settings = self.cog.settings_cache.get(guild_id, {})
        channel_id = settings.get("msg_report_channel")

        if not channel_id:
            return await interaction.response.send_message("Channel is not set up.", ephemeral=True)

        channel = interaction.guild.get_channel(channel_id)
        if channel:
            await channel.send("This is a test message from the Dopamine Message Reporting system.")
            await interaction.response.send_message("Test message sent!", ephemeral=True)
        else:
            await interaction.response.send_message("Could not find the configured channel.", ephemeral=True)

    async def return_home(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=ModerationDashboard(self.user, self.cog))


class ChannelSelect(PrivateLayoutView):
    def __init__(self, user, cog, firsttime: int = 0):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.firsttime = firsttime
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()

        select = discord.ui.ChannelSelect(
            placeholder="Select a channel...",
            min_values=1,
            max_values=1,
            channel_types=[discord.ChannelType.text]
        )
        select.callback = self.select_channel

        header_text = "## Step 1: Select the channel where you want reports to appear:" if self.firsttime else "## Select the channel where you want reports to appear:"
        container.add_item(discord.ui.TextDisplay(header_text))
        container.add_item(discord.ui.ActionRow(select))

        self.add_item(container)

    async def select_channel(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        channel_id = interaction.data['values'][0]

        async with self.cog.acquire_db() as db:
            await db.execute("UPDATE settings SET msg_report_channel = ? WHERE guild_id = ?", (channel_id, guild_id))
            await db.commit()

        self.cog.settings_cache[guild_id]["msg_report_channel"] = int(channel_id)

        if self.firsttime == 1:
            settings = self.cog.settings_cache.get(guild_id, {})
            if not settings.get("msg_report_roles"):
                return await interaction.response.edit_message(view=RoleSelect(self.user, self.cog, firsttime=1))

        await interaction.response.edit_message(view=MessageReportDashboard(self.user, self.cog))


class RoleSelect(PrivateLayoutView):
    def __init__(self, user, cog, firsttime: int = 0):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.firsttime = firsttime
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()

        select = discord.ui.RoleSelect(placeholder="Select role(s)...", min_values=1, max_values=25)
        select.callback = self.select_role

        header_text = "## Step 2: Select the roles that Dopamine should ping when a message is reported:" if self.firsttime else "## Select the roles that Dopamine should ping when a message is reported:"
        container.add_item(discord.ui.TextDisplay(header_text))

        skip_button = discord.ui.Button(label="Skip (Don't ping anyone / Set it up later)",
                                        style=discord.ButtonStyle.secondary)
        skip_button.callback = self.skip_roles

        container.add_item(discord.ui.ActionRow(select))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(skip_button))

        self.add_item(container)

    async def select_role(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        roles = ",".join(interaction.data['values'])

        async with self.cog.acquire_db() as db:
            await db.execute("UPDATE settings SET msg_report_roles = ? WHERE guild_id = ?", (roles, guild_id))
            await db.commit()

        self.cog.settings_cache[guild_id]["msg_report_roles"] = roles
        await interaction.response.edit_message(view=MessageReportDashboard(self.user, self.cog))

    async def skip_roles(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=MessageReportDashboard(self.user, self.cog))


class ReportActionModal(discord.ui.Modal):
    def __init__(self, cog, target: discord.Member, is_simple: bool):
        super().__init__(title=f"Punish {target.name[:35]}")
        self.cog = cog
        self.target = target

        term = "Warnings" if is_simple else "Points"

        self.amount = discord.ui.TextInput(
            label=f"Number of {term} to Add",
            placeholder="Enter a number...",
            min_length=1, max_length=4
        )
        self.reason = discord.ui.TextInput(
            label="Reason",
            style=discord.TextStyle.long,
            placeholder="Why are they being punished?",
            required=True
        )

        self.delete_msgs = discord.ui.TextInput(
            label="Delete User's Messages <14 days old? (yes/no)",
            placeholder="Type 'Yes' for Yes or 'No' for No",
            min_length=1, max_length=3,
            required=True,
            default="No"
        )

        self.add_item(self.amount)
        self.add_item(self.reason)
        self.add_item(self.delete_msgs)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amt = int(self.amount.value)
        except ValueError:
            return await interaction.response.send_message("Invalid amount. Must be a number.", ephemeral=True)

        del_msgs = self.delete_msgs.value.lower().startswith('y')

        await self.cog._add_infraction(interaction, self.target, amt, self.reason.value, del_msgs)


class ReportActionView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    def get_ids_from_footer(self, footer_text: str):
        try:
            parts = footer_text.split(" | ")
            auth_id = int(parts[0].replace("Author: ", ""))
            rep_id = int(parts[1].replace("Reporter: ", ""))
            return auth_id, rep_id
        except (IndexError, ValueError):
            return None, None

    @discord.ui.button(label="Punish Author", style=discord.ButtonStyle.danger, custom_id="report_warn_author")
    async def warn_author(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.moderate_members:
            return await interaction.response.send_message("You lack permissions.", ephemeral=True)

        auth_id, _ = self.get_ids_from_footer(interaction.message.embeds[0].footer.text)
        if not auth_id:
            return await interaction.response.send_message("Could not retrieve user data from this report.",
                                                           ephemeral=True)

        target = interaction.guild.get_member(auth_id) or await interaction.guild.fetch_member(auth_id)

        settings = self.cog.settings_cache.get(interaction.guild.id, {})
        is_simple = settings.get("simple_mode", 0) == 1

        await interaction.response.send_modal(ReportActionModal(self.cog, target, is_simple))

    @discord.ui.button(label="Punish Reporter", style=discord.ButtonStyle.danger, custom_id="report_warn_reporter")
    async def warn_reporter(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.moderate_members:
            return await interaction.response.send_message("You lack permissions.", ephemeral=True)

        _, rep_id = self.get_ids_from_footer(interaction.message.embeds[0].footer.text)
        if not rep_id:
            return await interaction.response.send_message("Could not retrieve user data from this report.",
                                                           ephemeral=True)

        target = interaction.guild.get_member(rep_id) or await interaction.guild.fetch_member(rep_id)

        settings = self.cog.settings_cache.get(interaction.guild.id, {})
        is_simple = settings.get("simple_mode", 0) == 1

        await interaction.response.send_modal(ReportActionModal(self.cog, target, is_simple))

    @discord.ui.button(label="Dismiss", style=discord.ButtonStyle.secondary, custom_id="report_dismiss")
    async def dismiss_report(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.moderate_members:
            return await interaction.response.send_message("You lack permissions.", ephemeral=True)

        for item in self.children:
            item.disabled = True

        await interaction.response.edit_message(content=f"**Report dismissed by {interaction.user.mention}**",
                                                view=self)

class ModerationDashboard(PrivateLayoutView):
    def __init__(self, user, cog):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("## Dopamine Moderation Dashboard"))
        container.add_item(discord.ui.Separator())

        settings = self.cog.settings_cache.get(self.user.guild.id, {})
        is_simple = settings.get("simple_mode", 0) == 1
        term = "Warnings" if is_simple else "Points"

        container.add_item(discord.ui.TextDisplay(
            f"Dopamine replaces traditional mute/kick/ban commands with a **escalation system**. "
            f"Moderators assign {term.lower()}, and the bot handles the math and the punishment automatically.\n\n"
            f"**Default Punishment Logic:**\n"
            f"* 1 {term}: Warning\n"
            f"* 2 {term}: 1h Timeout\n"
            f"* 3-4 {term}: Incremental Bans (12h to 1 week)\n"
            f"* 5 {term}: Permanent Ban\n> Dopamine's moderation system is completely customisable. Create new actions with any type of punishment and duration, delete old ones, or update the {term.lower()} amounts for an existing one.\n\n"
            "**Core Features:**\n"
            f"* **Cases:** Dopamine stores every single moderation punishment as its own case. View, sort, and search through all cases with `/case all`, view a list of all users with infractions using `/case users`, or view an individual user's case history using `/case history`."
            f"* **Decay:** {term} drop by 1 every set frequency (default: two weeks) if no new infractions occur.\n"
            f"* **Rejoin Policy:** Users unbanned via the bot start a set amount to prevent immediate repeat offenses by keeping them on thin ice."
        ))
        container.add_item(discord.ui.Separator())

        values_btn = discord.ui.Button(label=f"Customise {term} System", style=discord.ButtonStyle.primary)
        values_btn.callback = self.go_to_customisation

        report_btn = discord.ui.Button(label="Message Reporting", style=discord.ButtonStyle.primary)
        report_btn.callback = self.go_to_message_reports

        settings_btn = discord.ui.Button(label="Settings", style=discord.ButtonStyle.secondary)
        settings_btn.callback = self.go_to_settings

        row = discord.ui.ActionRow()
        row.add_item(values_btn)
        row.add_item(report_btn)
        row.add_item(settings_btn)
        container.add_item(row)
        self.add_item(container)

    async def go_to_customisation(self, interaction: discord.Interaction):
        view = CustomisationPage(self.user, self.cog)
        await interaction.response.edit_message(view=view)

    async def go_to_settings(self, interaction: discord.Interaction):
        view = SettingsPage(self.user, self.cog)
        await interaction.response.edit_message(view=view)

    async def go_to_message_reports(self, interaction: discord.Interaction):
        view = MessageReportDashboard(self.user, self.cog)
        await interaction.response.edit_message(view=view)


class SettingsPage(PrivateLayoutView):
    def __init__(self, user, cog):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        guild_id = self.user.guild.id
        settings = self.cog.settings_cache.get(guild_id, {"punishment_dm": 1, "punishment_log": 1, "simple_mode": 0,
                                                          "decay_interval": 14, "rejoin_points": 4, "decay_log_enabled": 0})

        dm_on = settings.get("punishment_dm", 1) == 1
        log_on = settings.get("punishment_log", 1) == 1
        simple_on = settings.get("simple_mode", 0) == 1
        decay_val = settings.get("decay_interval", 14)
        rejoin_val = settings.get("rejoin_points", 4)
        decay_log_on = settings.get("decay_log_enabled", 0) == 1
        rejoin_str = "Preserve" if rejoin_val == -1 else str(rejoin_val)

        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("## Moderation Settings"))
        container.add_item(discord.ui.Separator())

        dm_btn = discord.ui.Button(label=f"{'Disable' if dm_on else 'Enable'} DMs",
                                   style=discord.ButtonStyle.secondary if dm_on else discord.ButtonStyle.primary)
        dm_btn.callback = self.make_toggle_callback("punishment_dm", not dm_on)

        log_btn = discord.ui.Button(label=f"{'Disable' if log_on else 'Enable'} Mod Logs",
                                    style=discord.ButtonStyle.secondary if log_on else discord.ButtonStyle.primary)
        log_btn.callback = self.make_toggle_callback("punishment_log", not log_on)

        simple_btn = discord.ui.Button(label=f"{'Disable' if simple_on else 'Enable'} Simple Mode",
                                       style=discord.ButtonStyle.secondary if simple_on else discord.ButtonStyle.primary)
        simple_btn.callback = self.toggle_simple_mode(not simple_on)

        decay_btn = discord.ui.Button(label=f"Edit Decay Frequency", style=discord.ButtonStyle.secondary)
        decay_btn.callback = self.open_modal_callback("decay_interval")

        rejoin_btn = discord.ui.Button(label=f"Edit Rejoin Points", style=discord.ButtonStyle.secondary)
        rejoin_btn.callback = self.open_modal_callback("rejoin_points")

        decay_log_btn = discord.ui.Button(label=f"{'Disable' if decay_log_on else 'Enable'} Decay Logs",
                                          style=discord.ButtonStyle.secondary if decay_log_on else discord.ButtonStyle.primary)
        decay_log_btn.callback = self.make_toggle_callback("decay_log_enabled", not decay_log_on)

        medals_on = settings.get("show_medals", 1) == 1
        medals_btn = discord.ui.Button(
            label=f"{'Disable' if medals_on else 'Enable'} Medals",
            style=discord.ButtonStyle.secondary if medals_on else discord.ButtonStyle.primary
        )
        medals_btn.callback = self.make_toggle_callback("show_medals", not medals_on)

        container.add_item(discord.ui.Section(discord.ui.TextDisplay(
            f"* **Decay Frequency:** Edit the frequency at which one {'warning' if simple_on else 'point'} is decayed from a user. Current: **{'Disabled' if decay_val == 0 else f'{decay_val} Days'}**."),
                                              accessory=decay_btn))

        container.add_item(
            discord.ui.Section(discord.ui.TextDisplay(
                """* **Simple Mode:** The default configuration for Dopamine's moderation system that aims to mirror the experience of traditional moderation bots to make on-boarding easier. This setting is enabled by default.\n  * **Terminology:** Replaces "point" with "warning" and replaces `/point` command with `/warn` ("amount" option becomes optional and defaults to 1)\n  * The following simple five-strike preset is applied (you can still completely customise actions and replace this):\n    * 1 warning: Verbal warning, no punishment\n    * 2 warnings: 60-minute timeout\n    * 3 warnings: 12-hour ban\n    * 4 warnings: 7-day ban\n    * 5 warnings: Permanent ban"""),
                accessory=simple_btn))

        container.add_item(
            discord.ui.Section(discord.ui.TextDisplay(
                "* **Mod Logs:** Logs Moderation actions in the logging channel (if a channel is set using `/logging set`)."),
                accessory=log_btn))

        container.add_item(
            discord.ui.Section(discord.ui.TextDisplay(
                "* **Decay Logs:** Logs decay summaries in the logging channel (if a channel is set and enabled)."),
                accessory=decay_log_btn))

        container.add_item(discord.ui.Section(discord.ui.TextDisplay(
            f"* **Rejoin Points:** Edit the number of points that a user is given upon joining after being banned. Set it to `preserve` to preserve their points. Current: **{rejoin_str}**"),
            accessory=rejoin_btn))

        container.add_item(
            discord.ui.Section(discord.ui.TextDisplay("* **Punishment DMs:** Sends a DM to the user who is punished."),
                               accessory=dm_btn))

        container.add_item(discord.ui.Section(
            discord.ui.TextDisplay("* **Medals:** Show or hide medals next to names of users in Active Infractions list (`/case users` command)."),
            accessory=medals_btn
        ))

        container.add_item(discord.ui.Separator())
        return_btn = discord.ui.Button(emoji=self.cog.bot.back_emoji, label="Back", style=discord.ButtonStyle.secondary)
        return_btn.callback = self.return_home

        container.add_item(discord.ui.ActionRow(return_btn))
        self.add_item(container)

    def make_toggle_callback(self, key, new_val):
        async def callback(interaction: discord.Interaction):
            async with self.cog.acquire_db() as db:
                await db.execute(f"UPDATE settings SET {key} = ? WHERE guild_id = ?",
                                 (1 if new_val else 0, interaction.guild.id))
                await db.commit()
            self.cog.settings_cache[interaction.guild.id][key] = 1 if new_val else 0
            await interaction.response.edit_message(view=SettingsPage(self.user, self.cog))

        return callback

    def toggle_simple_mode(self, new_val):
        async def callback(interaction: discord.Interaction):
            if new_val:
                async with self.cog.acquire_db() as db:
                    await db.execute("DELETE FROM actions WHERE guild_id = ?", (interaction.guild.id,))
                    preset = [
                        ("warning", 0, 1),
                        ("timeout", 3600, 2),
                        ("ban", 43200, 3),
                        ("ban", 604800, 4),
                        ("ban", 0, 5)
                    ]
                    await db.executemany(
                        "INSERT INTO actions (guild_id, action_type, duration, points) VALUES (?, ?, ?, ?)",
                        [(interaction.guild.id, a, d, p) for a, d, p in preset])
                    await db.execute("UPDATE settings SET simple_mode = 1 WHERE guild_id = ?", (interaction.guild.id,))
                    await db.commit()
            else:
                async with self.cog.acquire_db() as db:
                    await db.execute("UPDATE settings SET simple_mode = 0 WHERE guild_id = ?", (interaction.guild.id,))
                    await db.commit()

            self.cog.settings_cache[interaction.guild.id]["simple_mode"] = 1 if new_val else 0
            await self.cog.refresh_action_cache(interaction.guild.id)
            await interaction.response.edit_message(view=SettingsPage(self.user, self.cog))

        return callback

    def open_modal_callback(self, key):
        async def callback(interaction: discord.Interaction):
            await interaction.response.send_modal(SettingValueModal(self.cog, key))

        return callback

    async def return_home(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=ModerationDashboard(self.user, self.cog))


class CustomisationPage(PrivateLayoutView):
    def __init__(self, user, cog, page=1, delete_mode=False):
        super().__init__(user, timeout=None)
        self.page = page
        self.cog = cog
        self.items_per_page = 5
        self.delete_mode = delete_mode
        self.build_layout()

    def build_layout(self):
        self.clear_items()

        guild_id = self.user.guild.id
        settings = self.cog.settings_cache.get(guild_id, {})
        is_simple = settings.get("simple_mode", 0) == 1
        term = "Warning" if is_simple else "Point"

        all_actions = self.cog.action_cache.get(guild_id, [])
        all_actions.sort(key=lambda x: x['points'])

        total_items = len(all_actions)
        total_pages = (total_items + self.items_per_page - 1) // self.items_per_page if total_items > 0 else 1

        start_idx = (self.page - 1) * self.items_per_page
        end_idx = start_idx + self.items_per_page
        current_actions = all_actions[start_idx:end_idx]

        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay(f"## Customise {term} System"))
        container.add_item(discord.ui.TextDisplay(
            f"List of actions triggered at specific {term.lower()} thresholds."))
        container.add_item(discord.ui.Separator())

        if not all_actions:
            container.add_item(discord.ui.TextDisplay("*No actions configured.*"))
        else:
            for i, action in enumerate(current_actions, start_idx + 1):
                act_name = action['action_type'].title()
                dur_str = format_duration_str(action['duration'])

                if act_name == "Ban":
                    display = "Permanent Ban" if action['duration'] == 0 else f"{dur_str} Ban"
                elif act_name == "Timeout":
                    display = f"{dur_str} Timeout"
                else:
                    display = act_name

                btn_label = "Delete" if self.delete_mode else "Edit"
                btn_style = discord.ButtonStyle.danger if self.delete_mode else discord.ButtonStyle.secondary

                btn = discord.ui.Button(label=btn_label, style=btn_style)
                btn.callback = self.make_action_callback(action, total_items)

                display_text = f"{i}. **{display}**: `{action['points']}` {term.lower()}{'s' if action['points'] != 1 else ''}"
                container.add_item(discord.ui.Section(discord.ui.TextDisplay(display_text), accessory=btn))

            container.add_item(discord.ui.Separator())

            nav_row = discord.ui.ActionRow()

            left_btn = discord.ui.Button(emoji="◀️", style=discord.ButtonStyle.primary, disabled=(self.page <= 1))
            left_btn.callback = self.prev_page
            nav_row.add_item(left_btn)

            go_btn = discord.ui.Button(label=f"Page {self.page} of {total_pages}", style=discord.ButtonStyle.secondary,
                                       disabled=(total_pages == 1))
            go_btn.callback = self.go_to_page_callback
            nav_row.add_item(go_btn)

            right_btn = discord.ui.Button(emoji="▶️", style=discord.ButtonStyle.primary,
                                          disabled=(self.page >= total_pages))
            right_btn.callback = self.next_page
            nav_row.add_item(right_btn)

            container.add_item(nav_row)

            container.add_item(discord.ui.Separator())

        control_row = discord.ui.ActionRow()
        create_btn = discord.ui.Button(label="Create New Action", style=discord.ButtonStyle.primary,
                                       disabled=len(all_actions) >= 20)
        create_btn.callback = self.create_action

        toggle_delete_btn = discord.ui.Button(
            label=f"{'Disable' if self.delete_mode else 'Enable'} Delete Mode",
            style=discord.ButtonStyle.danger if self.delete_mode else discord.ButtonStyle.secondary
        )
        toggle_delete_btn.callback = self.toggle_delete

        control_row.add_item(create_btn)
        control_row.add_item(toggle_delete_btn)
        container.add_item(control_row)

        container.add_item(discord.ui.Separator())
        return_btn = discord.ui.Button(emoji=self.cog.bot.back_emoji, label="Back", style=discord.ButtonStyle.secondary)
        return_btn.callback = self.return_home
        container.add_item(discord.ui.ActionRow(return_btn))

        self.add_item(container)

    def make_action_callback(self, action, total_actions):
        async def callback(interaction: discord.Interaction):
            if self.delete_mode:
                if total_actions <= 1:
                    return await interaction.response.send_message("You must keep at least one action.", ephemeral=True)

                async with self.cog.acquire_db() as db:
                    await db.execute("DELETE FROM actions WHERE id = ?", (action['id'],))
                    await db.commit()
                await self.cog.refresh_action_cache(interaction.guild.id)
                all_actions = self.cog.action_cache.get(interaction.guild.id, [])
                max_pages = (len(all_actions) + self.items_per_page - 1) // self.items_per_page
                self.page = min(self.page, max_pages) if max_pages > 0 else 1

                self.build_layout()
                await interaction.response.edit_message(view=self)
            else:
                await interaction.response.send_modal(
                    ActionModal(self.cog, interaction.guild.id, is_create=False, existing_action_id=action['id']))

        return callback

    async def prev_page(self, interaction: discord.Interaction):
        self.page -= 1
        self.build_layout()
        await interaction.response.edit_message(view=self)

    async def next_page(self, interaction: discord.Interaction):
        self.page += 1
        self.build_layout()
        await interaction.response.edit_message(view=self)

    async def go_to_page_callback(self, interaction: discord.Interaction):
        all_actions = self.cog.action_cache.get(interaction.guild.id, [])
        total_pages = (len(all_actions) + self.items_per_page - 1) // self.items_per_page
        await interaction.response.send_modal(GoToPageModal(self, total_pages))

    async def create_action(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ActionModal(self.cog, interaction.guild.id, is_create=True))

    async def toggle_delete(self, interaction: discord.Interaction):
        self.delete_mode = not self.delete_mode
        self.build_layout()
        await interaction.response.edit_message(view=self)

    async def return_home(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=ModerationDashboard(self.user, self.cog))


class GoToPageModal(discord.ui.Modal):
    def __init__(self, parent_view, total_pages: int):
        super().__init__(title="Jump to Page")
        self.parent_view = parent_view
        self.total_pages = total_pages
        self.page_input = discord.ui.TextInput(
            label=f"Page Number (1-{total_pages})",
            placeholder="Enter a page number...",
            min_length=1, max_length=5, required=True
        )
        self.add_item(self.page_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            page_num = int(self.page_input.value)
            if 1 <= page_num <= self.total_pages:
                self.parent_view.page = page_num
                self.parent_view.build_layout()
                await interaction.response.edit_message(view=self.parent_view)
            else:
                await interaction.response.send_message(f"Enter a number between 1 and {self.total_pages}.",
                                                        ephemeral=True)
        except ValueError:
            await interaction.response.send_message("Invalid input.", ephemeral=True)


class CaseDetailPage(PrivateLayoutView):
    def __init__(self, user, cog, guild: discord.Guild, case: dict, term: str, parent=None):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.guild = guild
        self.case = case
        self.term = term
        self.parent = parent
        self.message: Optional[discord.Message] = None
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay(f"## Case #{self.case['case_number']}"))
        container.add_item(discord.ui.TextDisplay(
            "Review this infraction below. Use the actions to manage the case or browse related history."
        ))
        container.add_item(discord.ui.Separator())

        punishment = format_punishment_label(
            self.case["punishment_type"], self.case["punishment_duration"]
        )
        reason = self.case["reason"] or "No reason provided."
        details = (
            f"* **User:** <@{self.case['user_id']}> (`{self.case['user_id']}`)\n"
            f"* **Moderator:** <@{self.case['moderator_id']}> (`{self.case['moderator_id']}`)\n"
            f"* **Date:** <t:{self.case['created_at']}:F> (<t:{self.case['created_at']}:R>)\n"
            f"* **Amount:** +{self.case['amount']} {self.term}(s)\n"
            f"* **Total after case:** {self.case['points_after']} {self.term}(s)\n"
            f"* **Punishment:** {punishment}\n"
            f"* **Reason:** {reason}"
        )
        container.add_item(discord.ui.TextDisplay(details))
        container.add_item(discord.ui.Separator())

        action_row = discord.ui.ActionRow()
        history_btn = discord.ui.Button(label="User History", style=discord.ButtonStyle.primary)
        history_btn.callback = self.history_callback
        delete_btn = discord.ui.Button(label="Delete Case", style=discord.ButtonStyle.danger)
        delete_btn.callback = self.delete_callback
        action_row.add_item(history_btn)
        action_row.add_item(delete_btn)
        container.add_item(action_row)

        if self.parent is not None:
            container.add_item(discord.ui.Separator())
            row = discord.ui.ActionRow()
            back_btn = discord.ui.Button(emoji=self.cog.bot.back_emoji, label="Back", style=discord.ButtonStyle.secondary)
            back_btn.callback = self.back_callback
            row.add_item(back_btn)
            container.add_item(row)


        self.add_item(container)

    async def history_callback(self, interaction: discord.Interaction):
        if isinstance(self.parent, AllActiveInfractionsPage):
            self.parent.pause_live()
        cases = await self.cog.get_user_infractions(self.guild.id, self.case["user_id"])
        view = CaseUserHistoryPage(
            self.user, self.cog, self.guild, self.case["user_id"], cases, self.term, parent=self
        )
        await interaction.response.edit_message(view=view)
        view.message = self.message

    async def delete_callback(self, interaction: discord.Interaction):
        user_display = await self.cog.resolve_user_display(self.guild, self.case["user_id"])
        mod_display = await self.cog.resolve_user_display(self.guild, self.case["moderator_id"])
        punishment = format_punishment_label(
            self.case["punishment_type"], self.case["punishment_duration"]
        )
        title = f"Delete Case | Case #{self.case['case_number']}"
        body = (
            f"* **User:** {user_display}\n"
            f"* **Moderator:** {mod_display}\n"
            f"* **Date:** <t:{self.case['created_at']}:F> (<t:{self.case['created_at']}:R>)\n"
            f"* **Amount:** +{self.case['amount']} {self.term}(s)\n"
            f"* **Total after case:** {self.case['points_after']} {self.term}(s)\n"
            f"* **Punishment:** {punishment}\n"
            f"* **Reason:** {self.case['reason'] or 'No reason provided.'}\n\n"
            f"Choose how to handle this deletion:"
        )
        view = CaseDeleteConfirmationView(
            interaction.user, self.cog, self.guild, self.case, self.term, body, title
        )
        await interaction.response.send_message(view=view, ephemeral=True)
        view.message = await interaction.original_response()

    async def back_callback(self, interaction: discord.Interaction):
        if self.parent is not None:
            await interaction.response.edit_message(view=self.parent)
            if isinstance(self.parent, AllActiveInfractionsPage):
                self.parent.resume_live()
        else:
            await interaction.response.defer()


class CaseUserHistoryPage(PrivateLayoutView):
    def __init__(self, user, cog, guild: discord.Guild, target_user_id: int, cases: list,
                 term: str, page: int = 1, parent=None):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.guild = guild
        self.target_user_id = target_user_id
        self.cases = cases
        self.term = term
        self.page = page
        self.per_page = 5
        self.parent = parent
        self.message: Optional[discord.Message] = None
        self.total_pages = max(1, (len(self.cases) - 1) // self.per_page + 1) if self.cases else 1
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        term_cap = self.term.title()
        total_pts = None
        key = f"{self.guild.id}:{self.target_user_id}"
        if key in self.cog.user_cache:
            total_pts = self.cog.user_cache[key]["points"]

        header = f"## {term_cap} History — <@{self.target_user_id}>"
        if total_pts is not None:
            header += f"\n-# Current total: **{total_pts}** {self.term}(s) • **{len(self.cases)}** case(s)"
        container.add_item(discord.ui.TextDisplay(header))
        container.add_item(discord.ui.TextDisplay(
            "Browse this user's infraction history. Select a case to view full details."
        ))
        container.add_item(discord.ui.Separator())

        start = (self.page - 1) * self.per_page
        current = self.cases[start:start + self.per_page]

        if not current:
            container.add_item(discord.ui.TextDisplay("*No cases found for this user.*"))
        else:
            for case in current:
                reason = case["reason"] or "No reason provided."
                if len(reason) > 80:
                    reason = reason[:77] + "..."
                punishment = format_punishment_label(case["punishment_type"], case["punishment_duration"])
                title = f"### Case #{case['case_number']} • +{case['amount']} {self.term}(s)"
                desc = (
                    f"**Punishment:** {punishment}\n"
                    f"**Reason:** {reason}\n"
                    f"**Moderator:** <@{case['moderator_id']}> • <t:{case['created_at']}:R>"
                )
                view_btn = discord.ui.Button(label="View", style=discord.ButtonStyle.primary)
                view_btn.callback = self.create_view_callback(case)
                container.add_item(discord.ui.Section(discord.ui.TextDisplay(f"{title}\n{desc}"), accessory=view_btn))

        container.add_item(discord.ui.Separator())

        nav_row = discord.ui.ActionRow()
        left_btn = discord.ui.Button(emoji="◀️", style=discord.ButtonStyle.primary, disabled=self.page <= 1)
        left_btn.callback = self.prev_callback
        go_btn = discord.ui.Button(label=f"Page {self.page} of {self.total_pages}", style=discord.ButtonStyle.secondary,
                                   disabled=self.total_pages <= 1)
        go_btn.callback = self.goto_callback
        right_btn = discord.ui.Button(emoji="▶️", style=discord.ButtonStyle.primary,
                                      disabled=self.page >= self.total_pages)
        right_btn.callback = self.next_callback
        nav_row.add_item(left_btn)
        nav_row.add_item(go_btn)
        nav_row.add_item(right_btn)
        container.add_item(nav_row)

        if self.parent is not None:
            container.add_item(discord.ui.Separator())
            back_btn = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary)
            back_btn.callback = self.back_callback
            container.add_item(discord.ui.ActionRow(back_btn))

        self.add_item(container)

    def create_view_callback(self, case):
        async def callback(interaction: discord.Interaction):
            if isinstance(self.parent, AllActiveInfractionsPage):
                self.parent.pause_live()
            view = CaseDetailPage(self.user, self.cog, self.guild, case, self.term, parent=self)
            await interaction.response.edit_message(view=view)
            view.message = self.message
        return callback

    async def prev_callback(self, interaction: discord.Interaction):
        self.page -= 1
        self.build_layout()
        await interaction.response.edit_message(view=self)

    async def next_callback(self, interaction: discord.Interaction):
        self.page += 1
        self.build_layout()
        await interaction.response.edit_message(view=self)

    async def goto_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(GoToPageModal(self, self.total_pages))

    async def back_callback(self, interaction: discord.Interaction):
        if self.parent is not None:
            await interaction.response.edit_message(view=self.parent)
            if isinstance(self.parent, AllActiveInfractionsPage):
                self.parent.resume_live()
        else:
            await interaction.response.defer()


class CaseUserSearchModal(discord.ui.Modal):
    def __init__(self, parent_view):
        super().__init__(title="Search by User ID")
        self.parent_view = parent_view
        self.query_input = discord.ui.TextInput(
            label="User ID",
            placeholder="Enter a Discord user ID...",
            min_length=1,
            max_length=20,
            required=True,
        )
        self.add_item(self.query_input)

    async def on_submit(self, interaction: discord.Interaction):
        query = self.query_input.value.strip()
        if not query.isdigit():
            return await interaction.response.send_message("Please enter a valid numeric user ID.", ephemeral=True)

        user_id = int(query)
        self.parent_view.search_query = query
        self.parent_view.container_header = f"Search results for `{query}`"
        self.parent_view.page = 1
        await self.parent_view.refresh_data()
        self.parent_view.build_layout()
        await interaction.response.edit_message(view=self.parent_view)


class AllActiveInfractionsPage(PrivateLayoutView):
    SORT_MOST = "most_points"
    SORT_LEAST = "least_points"
    SORT_RECENT = "recent_punishment"
    SORT_OLDEST = "oldest_punishment"
    SORT_ALPHA = "alpha"
    SORT_REVALPHA = "revalpha"

    def __init__(self, user, cog, guild: discord.Guild, term: str, page: int = 1,
                 current_sort: str = SORT_MOST, live: bool = False, search_query: str = None,
                 container_header: str = None):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.guild = guild
        self.guild_id = guild.id
        self.term = term
        self.page = page
        self.per_page = 5
        self.current_sort = current_sort
        self.live = live
        self.search_query = search_query
        self.container_header = container_header
        self.entries: List[dict] = []
        self.filtered_entries: List[dict] = []
        self.display_names: Dict[int, str] = {}
        self.total_pages = 1
        self.message: Optional[discord.Message] = None

    async def initialize(self):
        await self.refresh_data()
        self.build_layout()

    async def refresh_data(self):
        self.entries = await self.cog.get_guild_active_users(self.guild_id)
        await self._resolve_display_names(self.entries)
        self.apply_sort()
        self.apply_filter()
        self.total_pages = max(1, (len(self.filtered_entries) - 1) // self.per_page + 1) if self.filtered_entries else 1
        self.page = min(self.page, self.total_pages)

    async def _resolve_display_names(self, entries: List[dict]):
        for entry in entries:
            uid = entry["user_id"]
            if uid in self.display_names:
                continue
            member = self.guild.get_member(uid)
            if member:
                self.display_names[uid] = member.display_name
            else:
                try:
                    user = await self.cog.bot.fetch_user(uid)
                    self.display_names[uid] = user.display_name
                except discord.NotFound:
                    self.display_names[uid] = f"Unknown User"

    def apply_filter(self):
        if self.search_query:
            q = self.search_query.lower()
            self.filtered_entries = [
                e for e in self.entries
                if q in str(e["user_id"]) or q in self.display_names.get(e["user_id"], "").lower()
            ]
        else:
            self.filtered_entries = list(self.entries)

    def apply_sort(self):
        if self.current_sort == self.SORT_MOST:
            self.entries.sort(key=lambda x: x["points"], reverse=True)
        elif self.current_sort == self.SORT_LEAST:
            self.entries.sort(key=lambda x: x["points"])
        elif self.current_sort == self.SORT_RECENT:
            self.entries.sort(key=lambda x: x["last_punishment"] or 0, reverse=True)
        elif self.current_sort == self.SORT_OLDEST:
            self.entries.sort(key=lambda x: x["last_punishment"] or 0)
        elif self.current_sort == self.SORT_ALPHA:
            self.entries = natsorted(
                self.entries,
                key=lambda x: self.display_names.get(x["user_id"], str(x["user_id"])),
                alg=ns.IGNORECASE,
            )
        elif self.current_sort == self.SORT_REVALPHA:
            self.entries = natsorted(
                self.entries,
                key=lambda x: self.display_names.get(x["user_id"], str(x["user_id"])),
                alg=ns.IGNORECASE,
                reverse=True,
            )

    def _rank_emoji(self, rank: int, show_medals: bool) -> str:
        if show_medals:
            if rank == 1:
                return "🥇"
            if rank == 2:
                return "🥈"
            if rank == 3:
                return "🥉"
        return f"**#{rank}**"

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()

        count_text = f"{len(self.filtered_entries)} Active Member{'s' if len(self.filtered_entries) != 1 else ''}"
        if self.live:
            count_text += " • Live"

        if not self.container_header:
            header = f"## Active Infractions — {count_text}"
        else:
            header = f"## {self.container_header}"

        live_btn = discord.ui.Button(
            label="Disable Live Mode" if self.live else "Enable Live Mode",
            style=discord.ButtonStyle.secondary if self.live else discord.ButtonStyle.success,
        )
        live_btn.callback = self.live_callback
        lineone = discord.ui.TextDisplay(header)
        linetwo = discord.ui.TextDisplay(f"Members with active {self.term}(s) in this server. Sort, search, or enable **Live Mode** for real-time updates.")
        container.add_item(discord.ui.Section(lineone, linetwo, accessory=live_btn))
        container.add_item(discord.ui.Separator())

        start = (self.page - 1) * self.per_page
        current = self.filtered_entries[start:start + self.per_page]

        settings = self.cog.settings_cache.get(self.guild.id, {})
        show_medals = settings.get("show_medals", 1) == 1

        if not current:
            container.add_item(discord.ui.TextDisplay("*No active infractions found.*"))
        else:
            for idx, entry in enumerate(current, start + 1):
                uid = entry["user_id"]
                name = self.display_names.get(uid, "Unknown User")

                rank_label = self._rank_emoji(idx, show_medals) if self.current_sort == self.SORT_MOST and not self.search_query else f"**#{idx}**"
                last_p = (
                    f"<t:{entry['last_punishment']}:R>"
                    if entry["last_punishment"]
                    else "never"
                )
                title = f"### {rank_label} {name}"
                desc = (
                    f"**User:** <@{uid}> (`{uid}`)\n"
                    f"**Active {self.term.title()}(s):** {entry['points']}\n"
                    f"**Last punishment:** {last_p}"
                )
                cases_btn = discord.ui.Button(label="View Cases", style=discord.ButtonStyle.primary)
                cases_btn.callback = self.create_cases_callback(uid)
                container.add_item(discord.ui.Section(discord.ui.TextDisplay(f"{title}\n{desc}"), accessory=cases_btn))

        container.add_item(discord.ui.Separator())

        nav_row = discord.ui.ActionRow()
        left_btn = discord.ui.Button(emoji="◀️", style=discord.ButtonStyle.primary, disabled=self.page <= 1)
        left_btn.callback = self.prev_callback
        go_btn = discord.ui.Button(label=f"Page {self.page} of {self.total_pages}", style=discord.ButtonStyle.secondary,
                                   disabled=self.total_pages <= 1)
        go_btn.callback = self.goto_callback
        right_btn = discord.ui.Button(emoji="▶️", style=discord.ButtonStyle.primary,
                                      disabled=self.page >= self.total_pages)
        right_btn.callback = self.next_callback
        nav_row.add_item(left_btn)
        nav_row.add_item(go_btn)
        nav_row.add_item(right_btn)
        container.add_item(nav_row)
        container.add_item(discord.ui.Separator())

        control_row = discord.ui.ActionRow()
        search_btn = discord.ui.Button(label="Search by User ID", style=discord.ButtonStyle.primary)
        search_btn.callback = self.search_callback
        clear_btn = discord.ui.Button(label="Clear Search", style=discord.ButtonStyle.secondary,
                                      disabled=not self.search_query)
        clear_btn.callback = self.clear_search_callback

        control_row.add_item(search_btn)
        control_row.add_item(clear_btn)
        container.add_item(control_row)

        settings = self.cog.settings_cache.get(self.guild.id, {})
        is_simple = settings.get("simple_mode", 0) == 1
        term = "Warnings" if is_simple else "Points"

        sort_options = [
            discord.SelectOption(label=f"Most {term}", value=self.SORT_MOST),
            discord.SelectOption(label=f"Least {term}", value=self.SORT_LEAST),
            discord.SelectOption(label="Newest Punishment", value=self.SORT_RECENT),
            discord.SelectOption(label="Oldest Punishment", value=self.SORT_OLDEST),
            discord.SelectOption(label="Alphabetical (A–Z)", value=self.SORT_ALPHA),
            discord.SelectOption(label="Alphabetical (Z–A)", value=self.SORT_REVALPHA),
        ]
        for option in sort_options:
            if option.value == self.current_sort:
                option.default = True

        sort_dropdown = discord.ui.Select(placeholder="Sort by...", options=sort_options)
        sort_dropdown.callback = self.sort_callback
        container.add_item(discord.ui.ActionRow(sort_dropdown))

        self.add_item(container)

    def pause_live(self):
        if self.message:
            self.cog.unregister_live_case_view(self.message.id)

    def resume_live(self):
        if self.live and self.message:
            self.cog.register_live_case_view(self.message.id, self)

    def create_cases_callback(self, user_id: int):
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer()
            self.pause_live()
            cases = await self.cog.get_user_infractions(self.guild_id, user_id)
            view = CaseUserHistoryPage(
                self.user, self.cog, self.guild, user_id, cases, self.term, parent=self
            )
            await interaction.edit_original_response(view=view)
            view.message = self.message
        return callback

    async def prev_callback(self, interaction: discord.Interaction):
        self.page -= 1
        self.build_layout()
        await interaction.response.edit_message(view=self)

    async def next_callback(self, interaction: discord.Interaction):
        self.page += 1
        self.build_layout()
        await interaction.response.edit_message(view=self)

    async def goto_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(GoToPageModal(self, self.total_pages))

    async def search_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(CaseUserSearchModal(self))
    async def clear_search_callback(self, interaction: discord.Interaction):
        self.search_query = None
        self.container_header = None
        self.page = 1
        await self.refresh_data()
        self.build_layout()
        await interaction.response.edit_message(view=self)

    async def live_callback(self, interaction: discord.Interaction):
        self.live = not self.live
        self.build_layout()
        await interaction.response.edit_message(view=self)
        if self.live and self.message:
            self.cog.register_live_case_view(self.message.id, self)
        elif self.message:
            self.cog.unregister_live_case_view(self.message.id)

    async def sort_callback(self, interaction: discord.Interaction):
        self.current_sort = interaction.data["values"][0]
        self.page = 1
        self.apply_sort()
        self.apply_filter()
        self.total_pages = max(1, (len(self.filtered_entries) - 1) // self.per_page + 1) if self.filtered_entries else 1
        self.build_layout()
        await interaction.response.edit_message(view=self)


class CaseIDSearchModal(discord.ui.Modal):
    def __init__(self, parent_view):
        super().__init__(title="Search by Case ID")
        self.parent_view = parent_view
        self.case_id_input = discord.ui.TextInput(
            label="Case ID",
            placeholder="Enter the numeric Case ID (e.g., 123)...",
            min_length=1,
            max_length=10,
            required=True,
        )
        self.add_item(self.case_id_input)

    async def on_submit(self, interaction: discord.Interaction):
        query = self.case_id_input.value.strip()

        if not query.isdigit():
            return await interaction.response.send_message("Please enter a valid numeric Case ID.", ephemeral=True)

        self.parent_view.search_query = query
        self.parent_view.container_header = f"Search results for Case #{query}"
        self.parent_view.page = 1

        await self.parent_view.refresh_data()
        self.parent_view.build_layout()

        await interaction.response.edit_message(view=self.parent_view)


class AllCasesPage(PrivateLayoutView):
    SORT_NEWEST = "newest"
    SORT_OLDEST = "oldest"
    SORT_HIGHEST = "highest"
    SORT_LOWEST = "lowest"
    SORT_CASE_ASC = "case_asc"
    SORT_CASE_DESC = "case_desc"

    def __init__(self, user, cog, guild: discord.Guild, term: str, page: int = 1,
                 current_sort: str = SORT_NEWEST, search_query: str = None,
                 container_header: str = None, live: bool = False):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.guild = guild
        self.guild_id = guild.id
        self.term = term
        self.page = page
        self.per_page = 5
        self.current_sort = current_sort
        self.search_query = search_query
        self.container_header = container_header
        self.live = live
        self.entries: List[dict] = []
        self.filtered_entries: List[dict] = []
        self.total_pages = 1
        self.message: Optional[discord.Message] = None
        self.build_layout()

    async def initialize(self):
        await self.refresh_data()
        self.build_layout()

    def pause_live(self):
        if self.message:
            self.cog.unregister_live_case_view(self.message.id)

    def resume_live(self):
        if self.live and self.message:
            self.cog.register_live_case_view(self.message.id, self)

    async def refresh_data(self):
        self.entries = await self.cog.get_all_infractions(self.guild_id)
        self.apply_filter()
        self.apply_sort()
        self.total_pages = max(1, (len(self.filtered_entries) - 1) // self.per_page + 1) if self.filtered_entries else 1
        self.page = min(self.page, self.total_pages)

    def apply_filter(self):
        if self.search_query:
            q = self.search_query.lower()
            self.filtered_entries = [
                e for e in self.entries
                if q in str(e["case_number"]) or q in str(e["user_id"]) or (e["reason"] and q in e["reason"].lower())
            ]
        else:
            self.filtered_entries = list(self.entries)

    def apply_sort(self):
        if self.current_sort == self.SORT_NEWEST:
            self.entries.sort(key=lambda x: x["created_at"], reverse=True)
        elif self.current_sort == self.SORT_OLDEST:
            self.entries.sort(key=lambda x: x["created_at"])
        elif self.current_sort == self.SORT_HIGHEST:
            self.entries.sort(key=lambda x: x["amount"], reverse=True)
        elif self.current_sort == self.SORT_LOWEST:
            self.entries.sort(key=lambda x: x["amount"])
        elif self.current_sort == self.SORT_CASE_ASC:
            self.entries.sort(key=lambda x: x["case_number"])
        elif self.current_sort == self.SORT_CASE_DESC:
            self.entries.sort(key=lambda x: x["case_number"], reverse=True)

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()

        count_text = f"{len(self.filtered_entries)} Total Cases"
        if self.live:
            count_text += " • Live"

        if not self.container_header:
            header = f"## Case Log — {count_text}"
        else:
            header = f"## {self.container_header}"

        live_btn = discord.ui.Button(
            label="Disable Live Mode" if self.live else "Enable Live Mode",
            style=discord.ButtonStyle.secondary if self.live else discord.ButtonStyle.success,
        )
        live_btn.callback = self.live_callback

        container.add_item(discord.ui.TextDisplay(header))
        container.add_item(discord.ui.Section(
            discord.ui.TextDisplay(f"Browsing all recorded infractions. Use search to find specific Case IDs or User IDs."),
            accessory=live_btn
        ))
        container.add_item(discord.ui.Separator())

        start = (self.page - 1) * self.per_page
        current = self.filtered_entries[start:start + self.per_page]

        if not current:
            container.add_item(discord.ui.TextDisplay("*No cases found matching your criteria.*"))
        else:
            for idx, case in enumerate(current, start + 1):
                user_mention = f"<@{case['user_id']}>"

                last_p = f"<t:{case['created_at']}:R>"
                title = f"### Case #{case['case_number']} • +{case['amount']} {self.term}(s)"
                desc = (
                    f"**User:** {user_mention} (`{case['user_id']}`)\n"
                    f"**Moderator:** <@{case['moderator_id']}>\n"
                    f"**Date:** {last_p}\n"
                    f"**Reason:** {case['reason'] or 'No reason provided.'}"
                )

                view_btn = discord.ui.Button(label="View Details", style=discord.ButtonStyle.primary)
                view_btn.callback = self.create_details_callback(case)
                container.add_item(discord.ui.Section(discord.ui.TextDisplay(f"{title}\n{desc}"), accessory=view_btn))

        container.add_item(discord.ui.Separator())

        nav_row = discord.ui.ActionRow()
        left_btn = discord.ui.Button(emoji="◀️", style=discord.ButtonStyle.primary, disabled=(self.page <= 1))
        left_btn.callback = self.prev_page
        go_btn = discord.ui.Button(label=f"Page {self.page} of {self.total_pages}", style=discord.ButtonStyle.secondary,
                                   disabled=(self.total_pages <= 1))
        go_btn.callback = self.go_to_page_callback
        right_btn = discord.ui.Button(emoji="▶️", style=discord.ButtonStyle.primary,
                                      disabled=(self.page >= self.total_pages))
        right_btn.callback = self.next_page
        nav_row.add_item(left_btn)
        nav_row.add_item(go_btn)
        nav_row.add_item(right_btn)
        container.add_item(nav_row)
        container.add_item(discord.ui.Separator())
        control_row = discord.ui.ActionRow()
        search_case_id_btn = discord.ui.Button(label="Search by Case ID", style=discord.ButtonStyle.primary)
        search_case_id_btn.callback = self.search_case_id_callback
        search_user_id_btn = discord.ui.Button(label="Search by User ID", style=discord.ButtonStyle.primary)
        search_user_id_btn.callback = self.search_user_id_callback
        clear_btn = discord.ui.Button(label="Clear Search", style=discord.ButtonStyle.secondary,
                                      disabled=(not self.search_query))
        clear_btn.callback = self.clear_search_callback
        control_row.add_item(search_case_id_btn)
        control_row.add_item(search_user_id_btn)
        control_row.add_item(clear_btn)
        container.add_item(control_row)

        settings = self.cog.settings_cache.get(self.guild.id, {})
        is_simple = settings.get("simple_mode", 0) == 1
        term = "Warnings" if is_simple else "Points"

        sort_options = [
            discord.SelectOption(label="Newest First", value=self.SORT_NEWEST),
            discord.SelectOption(label="Oldest First", value=self.SORT_OLDEST),
            discord.SelectOption(label=f"Most {term}", value=self.SORT_HIGHEST),
            discord.SelectOption(label=f"Least {term}", value=self.SORT_LOWEST),
            discord.SelectOption(label="Case ID (Ascending)", value=self.SORT_CASE_ASC),
            discord.SelectOption(label="Case ID (Descending)", value=self.SORT_CASE_DESC),
        ]
        for opt in sort_options:
            if opt.value == self.current_sort:
                opt.default = True

        sort_dropdown = discord.ui.Select(placeholder="Sort by...", options=sort_options)
        sort_dropdown.callback = self.sort_callback
        container.add_item(discord.ui.ActionRow(sort_dropdown))

        self.add_item(container)

    async def live_callback(self, interaction: discord.Interaction):
        self.live = not self.live
        self.build_layout()
        await interaction.response.edit_message(view=self)
        if self.live and self.message:
            self.cog.register_live_case_view(self.message.id, self)
        elif self.message:
            self.cog.unregister_live_case_view(self.message.id)

    def create_details_callback(self, case):
        async def callback(interaction: discord.Interaction):
            self.pause_live()
            view = CaseDetailPage(self.user, self.cog, self.guild, case, self.term)
            await interaction.response.edit_message(view=view)
            view.message = self.message

        return callback

    async def prev_page(self, interaction: discord.Interaction):
        self.page -= 1
        self.build_layout()
        await interaction.response.edit_message(view=self)

    async def next_page(self, interaction: discord.Interaction):
        self.page += 1
        self.build_layout()
        await interaction.response.edit_message(view=self)

    async def go_to_page_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(GoToPageModal(self, self.total_pages))

    async def search_case_id_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(CaseIDSearchModal(self))

    async def search_user_id_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(CaseUserSearchModal(self))

    async def clear_search_callback(self, interaction: discord.Interaction):
        self.search_query = None
        self.container_header = None
        self.page = 1
        await self.refresh_data()
        self.build_layout()
        await interaction.response.edit_message(view=self)

    async def sort_callback(self, interaction: discord.Interaction):
        self.current_sort = interaction.data["values"][0]
        self.page = 1
        self.apply_sort()
        self.apply_filter()
        self.total_pages = max(1, (len(self.filtered_entries) - 1) // self.per_page + 1) if self.filtered_entries else 1
        self.build_layout()
        await interaction.response.edit_message(view=self)

class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.user_cache: Dict[str, Dict[str, Any]] = {}
        self.action_cache: Dict[int, List[Dict[str, Any]]] = {}
        self.settings_cache: Dict[int, Dict[str, Any]] = {}
        self.live_case_views: Dict[int, AllActiveInfractionsPage] = {}

        self.ctx_menu = app_commands.ContextMenu(
            name='Report Message to Server Mods',
            callback=self.report_message_menu
        )
        self.bot.tree.add_command(self.ctx_menu)
        self.manager = LoggingManager(bot.db)

    async def cog_load(self):
        await self.bot.db.wait_ready()
        await self.populate_caches()
        self.bot.add_view(ReportActionView(cog=self))
        self.unban_loop.start()
        self.decay_loop.start()
        self.live_case_refresh_loop.start()

    async def cog_unload(self):
        self.unban_loop.stop()
        self.decay_loop.stop()
        self.live_case_refresh_loop.stop()

    def _row_to_infraction(self, row) -> dict:
        return {
            "id": row[0],
            "guild_id": row[1],
            "case_number": row[2],
            "user_id": row[3],
            "moderator_id": row[4],
            "amount": row[5],
            "reason": row[6],
            "punishment_type": row[7],
            "punishment_duration": row[8],
            "points_after": row[9],
            "created_at": row[10],
        }

    async def next_case_number(self, guild_id: int, db: aiosqlite.Connection) -> int:
        async with db.execute(
                "SELECT COALESCE(MAX(case_number), 0) + 1 FROM infractions WHERE guild_id = ?",
                (guild_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0]

    async def record_infraction(
            self, guild_id: int, user_id: int, moderator_id: int, amount: int, reason: Optional[str],
            punishment_type: Optional[str], punishment_duration: int, points_after: int, created_at: int
    ) -> int:
        async with self.acquire_db() as db:
            case_number = await self.next_case_number(guild_id, db)
            await db.execute(
                '''INSERT INTO infractions
                   (guild_id, case_number, user_id, moderator_id, amount, reason,
                    punishment_type, punishment_duration, points_after, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (guild_id, case_number, user_id, moderator_id, amount, reason,
                 punishment_type, punishment_duration, points_after, created_at)
            )
            await db.commit()
            return case_number

    async def get_user_infractions(self, guild_id: int, user_id: int) -> List[dict]:
        async with self.acquire_db() as db:
            async with db.execute(
                    '''SELECT id, guild_id, case_number, user_id, moderator_id, amount, reason,
                              punishment_type, punishment_duration, points_after, created_at
                       FROM infractions
                       WHERE guild_id = ? AND user_id = ?
                       ORDER BY created_at DESC''',
                    (guild_id, user_id)
            ) as cursor:
                return [self._row_to_infraction(row) async for row in cursor]

    async def get_all_infractions(self, guild_id: int) -> List[dict]:
        async with self.acquire_db() as db:
            async with db.execute(
                    "SELECT id, guild_id, case_number, user_id, moderator_id, amount, reason, "
                    "punishment_type, punishment_duration, points_after, created_at "
                    "FROM infractions WHERE guild_id = ? ORDER BY created_at DESC",
                    (guild_id,)
            ) as cursor:
                return [self._row_to_infraction(row) async for row in cursor]

    async def get_infraction(self, guild_id: int, case_number: int) -> Optional[dict]:
        async with self.acquire_db() as db:
            async with db.execute(
                    '''SELECT id, guild_id, case_number, user_id, moderator_id, amount, reason,
                              punishment_type, punishment_duration, points_after, created_at
                       FROM infractions
                       WHERE guild_id = ? AND case_number = ?''',
                    (guild_id, case_number)
            ) as cursor:
                row = await cursor.fetchone()
                return self._row_to_infraction(row) if row else None

    async def get_guild_active_users(self, guild_id: int) -> List[dict]:
        entries = []
        seen = set()
        for key, data in self.user_cache.items():
            g_id_str, u_id_str = key.split(":")
            if int(g_id_str) != guild_id or data["points"] <= 0:
                continue
            user_id = int(u_id_str)
            seen.add(user_id)
            entries.append({
                "user_id": user_id,
                "points": data["points"],
                "last_punishment": data["last_punishment"],
                "last_decay": data["last_decay"],
            })

        async with self.acquire_db() as db:
            async with db.execute(
                    "SELECT user_id, points, last_punishment, last_decay FROM moderation_users WHERE guild_id = ? AND points > 0",
                    (guild_id,)
            ) as cursor:
                async for row in cursor:
                    if row[0] in seen:
                        continue
                    entries.append({
                        "user_id": row[0],
                        "points": row[1],
                        "last_punishment": row[2],
                        "last_decay": row[3],
                    })
        return entries

    def register_live_case_view(self, message_id: int, view: AllActiveInfractionsPage):
        self.live_case_views[message_id] = view

    def unregister_live_case_view(self, message_id: int):
        self.live_case_views.pop(message_id, None)

    async def refresh_live_case_views(self, guild_id: int):
        for msg_id, view in list(self.live_case_views.items()):
            if view.guild_id != guild_id or not view.live:
                continue
            try:
                await view.refresh_data()
                view.build_layout()
                if view.message:
                    await view.message.edit(view=view)
            except discord.NotFound:
                self.unregister_live_case_view(msg_id)
            except Exception as e:
                print(f"Error refreshing live case view {msg_id}: {e}")

    @tasks.loop(seconds=10)
    async def live_case_refresh_loop(self):
        for msg_id, view in list(self.live_case_views.items()):
            if not view.live:
                self.unregister_live_case_view(msg_id)
                continue
            try:
                await view.refresh_data()
                view.build_layout()
                if view.message:
                    await view.message.edit(view=view)
            except discord.NotFound:
                self.unregister_live_case_view(msg_id)
            except Exception as e:
                print(f"Error in live case refresh loop for {msg_id}: {e}")

    @live_case_refresh_loop.before_loop
    async def before_live_case_refresh_loop(self):
        await self.bot.wait_until_ready()

    async def delete_infraction(self, guild_id: int, case_number: int) -> bool:
        async with self.acquire_db() as db:
            cursor = await db.execute(
                "DELETE FROM infractions WHERE guild_id = ? AND case_number = ?",
                (guild_id, case_number)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def sync_last_punishment_from_cases(self, guild_id: int, user_id: int):
        async with self.acquire_db() as db:
            async with db.execute(
                    "SELECT MAX(created_at) FROM infractions WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id)
            ) as cursor:
                row = await cursor.fetchone()
                last_ts = row[0] if row and row[0] else None

        key = f"{guild_id}:{user_id}"
        data = await self.get_user_data(guild_id, user_id)
        data["last_punishment"] = last_ts
        if last_ts is None:
            data["last_decay"] = None
        self.user_cache[key] = data

        async with self.acquire_db() as db:
            await db.execute(
                '''UPDATE moderation_users SET last_punishment = ?, last_decay = ?
                   WHERE guild_id = ? AND user_id = ?''',
                (last_ts, data["last_decay"], guild_id, user_id)
            )
            await db.commit()

    async def execute_case_delete(self, interaction: discord.Interaction, guild: discord.Guild,
                                  case: dict, reverse: bool):
        guild_id = guild.id
        user_id = case["user_id"]
        data = await self.get_user_data(guild_id, user_id)
        new_points = max(0, data["points"] - case["amount"])
        old_points = max(0, data["points"])
        await self.delete_infraction(guild_id, case["case_number"])
        await self.update_user_points(guild_id, user_id, new_points)
        await self.sync_last_punishment_from_cases(guild_id, user_id)

        settings = self.settings_cache.get(guild_id, {})
        is_simple = settings.get("simple_mode", 0) == 1
        term = "warning" if is_simple else "point"
        reversal_notes = []

        if reverse:
            new_action, _ = self.get_punishment_data(new_points, guild_id)

            if case["punishment_type"] == "timeout":
                member = guild.get_member(user_id)
                if member and member.is_timed_out():
                    if new_action != "timeout":
                        try:
                            await member.timeout(None, reason=f"Case #{case['case_number']} deleted by {interaction.user.display_name}")
                            reversal_notes.append("Timeout removed")
                        except discord.Forbidden:
                            reversal_notes.append("Failed to remove timeout (missing permissions)")
                elif member is None:
                    reversal_notes.append("Timeout reversal skipped (user not in server)")

            elif case["punishment_type"] == "ban" and new_action != "ban":
                try:
                    await guild.unban(
                        discord.Object(id=user_id),
                        reason=f"Case #{case['case_number']} deleted by {interaction.user.display_name}"
                    )
                    async with self.acquire_db() as db:
                        await db.execute(
                            "DELETE FROM ban_schedule WHERE guild_id = ? AND user_id = ?",
                            (guild_id, user_id)
                        )
                        await db.commit()
                    reversal_notes.append("User unbanned")
                except discord.NotFound:
                    reversal_notes.append("User was not banned")
                except discord.Forbidden:
                    reversal_notes.append("Failed to unban (missing permissions)")

        reversal_text = ""
        if reverse:
            if reversal_notes:
                reversal_text = f"\n* **Reversal:** {', '.join(reversal_notes)}"
            else:
                reversal_text = "\n* **Reversal:** No Discord punishment changes were needed."

        embed = discord.Embed(
            title="Moderation Action Undone",
            description=(
                f"* Case **#{case['case_number']}** deleted.\n"
                f"* **{term.title()}s** adjusted: **{old_points}** → **{new_points}**"
                f"{reversal_text}"
            ),
            color=discord.Color.green()
        )
        embed.set_footer(text=f"by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        await interaction.followup.send(embed=embed)

        if settings.get("punishment_log", 1):
            log_ch = await self.get_log_channel(guild)
            if log_ch:
                log_embed = discord.Embed(
                    title="Moderation Action Undone",
                    description=(
                        f"* Case **#{case['case_number']}** deleted for <@{user_id}>."
                        f"* **{term.title()}s** adjusted: **{old_points}** → **{new_points}**"
                        f"{reversal_text}\n"
                        f"* **Original reason:** {case['reason'] or 'No reason provided.'}"
                    ),
                    color=discord.Colour.red()
                )
                log_embed.set_footer(text=f"by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
                await log_ch.send(embed=log_embed)

    async def resolve_user_display(self, guild: discord.Guild, user_id: int) -> str:
        member = guild.get_member(user_id)
        if member:
            return f"{member.mention} (`{user_id}`)"

        try:
            member = await guild.fetch_member(user_id)
            return f"{member.mention} (`{user_id}`)"
        except discord.NotFound:
            pass
        except discord.HTTPException:
            pass

        try:
            user = await self.bot.fetch_user(user_id)
            return f"**{user.display_name}** (`{user_id}`)"
        except (discord.NotFound, discord.HTTPException):
            return f"Unknown User (`{user_id}`)"

    async def populate_caches(self):
        self.user_cache.clear()
        self.action_cache.clear()
        self.settings_cache.clear()

        async with self.acquire_db() as db:
            async with db.execute("SELECT guild_id, user_id, points, last_punishment, last_decay, total_decayed FROM moderation_users") as cursor:
                async for row in cursor:
                    self.user_cache[f"{row[0]}:{row[1]}"] = {
                        "points": row[2],
                        "last_punishment": row[3],
                        "last_decay": row[4],
                        "total_decayed": row[5]
                    }

            async with db.execute("SELECT * FROM actions") as cursor:
                async for row in cursor:
                    guild_id = row[1]
                    action = {
                        "id": row[0],
                        "guild_id": row[1],
                        "action_type": row[2],
                        "duration": row[3],
                        "points": row[4]
                    }
                    if guild_id not in self.action_cache:
                        self.action_cache[guild_id] = []
                    self.action_cache[guild_id].append(action)

            async with db.execute(
                    "SELECT guild_id, punishment_dm, punishment_log, decay_interval, rejoin_points, simple_mode, msg_report_enabled, msg_report_channel, msg_report_roles, decay_log_enabled, show_medals FROM settings") as cursor:
                async for row in cursor:
                    self.settings_cache[row[0]] = {
                        "punishment_dm": row[1],
                        "punishment_log": row[2],
                        "decay_interval": row[3],
                        "rejoin_points": row[4],
                        "simple_mode": row[5],
                        "msg_report_enabled": row[6],
                        "msg_report_channel": row[7],
                        "msg_report_roles": row[8],
                        "decay_log_enabled": row[9],
                        "show_medals": row[10]
                    }

    async def guild_setup(self, interaction: discord.Interaction):
        if interaction.guild.id not in self.settings_cache:
            async with self.acquire_db() as db:
                await db.execute("INSERT OR IGNORE INTO settings (guild_id) VALUES (?)", (interaction.guild.id,))
                await db.commit()

            self.settings_cache[interaction.guild.id] = {
                "punishment_dm": 1, "punishment_log": 1, "simple_mode": 1,
                "decay_interval": 14, "rejoin_points": 4,
                "msg_report_enabled": 0, "msg_report_channel": None, "msg_report_roles": None,
                "decay_log_enabled": 0, "show_medals": 1
            }
            await self.apply_default_actions(interaction.guild.id)
        return True

    async def apply_default_actions(self, guild_id: int):
        default_actions = [
            ("warning", 0, 1),
            ("timeout", 3600, 2),
            ("ban", 43200, 3),
            ("ban", 604800, 4),
            ("ban", 0, 5)
        ]

        async with self.acquire_db() as db:
            async with db.execute("SELECT 1 FROM actions WHERE guild_id = ? LIMIT 1", (guild_id,)) as cursor:
                if not await cursor.fetchone():
                    await db.executemany(
                        "INSERT INTO actions (guild_id, action_type, duration, points) VALUES (?, ?, ?, ?)",
                        [(guild_id, a, d, p) for a, d, p in default_actions]
                    )
                    await db.commit()
                    await self.refresh_action_cache(guild_id)


    async def refresh_action_cache(self, guild_id: int):
        if guild_id in self.action_cache:
            self.action_cache[guild_id] = []

        async with self.acquire_db() as db:
            async with db.execute("SELECT * FROM actions WHERE guild_id = ?", (guild_id,)) as cursor:
                async for row in cursor:
                    action = {
                        "id": row[0],
                        "guild_id": row[1],
                        "action_type": row[2],
                        "duration": row[3],
                        "points": row[4]
                    }
                    if guild_id not in self.action_cache:
                        self.action_cache[guild_id] = []
                    self.action_cache[guild_id].append(action)

    async def get_user_data(self, guild_id: int, user_id: int) -> dict:
        key = f"{guild_id}:{user_id}"
        if key not in self.user_cache:
            data = {"points": 0, "last_punishment": None, "last_decay": None, "total_decayed": 0}
            self.user_cache[key] = data
            async with self.acquire_db() as db:
                await db.execute(
                    "INSERT OR IGNORE INTO moderation_users (guild_id, user_id, points, total_decayed) VALUES (?, ?, ?, ?)",
                    (guild_id, user_id, 0, 0)
                )
                await db.commit()
        return self.user_cache[key]

    async def update_user_points(self, guild_id: int, user_id: int, points: int, punishment_ts: Optional[int] = None, total_decayed: Optional[int] = None):
        key = f"{guild_id}:{user_id}"
        data = await self.get_user_data(guild_id, user_id)
        data["points"] = points
        if punishment_ts:
            data["last_punishment"] = punishment_ts
            data["last_decay"] = None
        if total_decayed is not None:
            data["total_decayed"] = total_decayed

        self.user_cache[key] = data

        async with self.acquire_db() as db:
            await db.execute('''
                             UPDATE moderation_users
                             SET points          = ?,
                                 last_punishment = ?,
                                 last_decay      = ?,
                                 total_decayed   = ?
                             WHERE guild_id = ?
                               AND user_id = ?
                             ''', (points, data["last_punishment"], data["last_decay"], data.get("total_decayed", 0), guild_id, user_id))
            await db.commit()

        await self.refresh_live_case_views(guild_id)

    def get_punishment_data(self, points: int, guild_id: int):
        actions = self.action_cache.get(guild_id, [])
        if not actions:
            return None, None

        actions.sort(key=lambda x: x['points'])

        triggered_action = None
        for action in actions:
            if points >= action['points']:
                triggered_action = action
            else:
                break

        if triggered_action:
            dur = None
            if triggered_action['duration'] > 0:
                dur = timedelta(seconds=triggered_action['duration'])
            return triggered_action['action_type'], dur

        return "warning", None

    async def get_log_channel(self, guild: discord.Guild):
        channel_id = await self.manager.log_get(guild.id)
        if not channel_id:
            return None
        _, channel = await resolve_guild_channel(
            self.bot, guild.id, channel_id, feature_id="logging"
        )
        if channel and channel_can_send(channel, guild):
            return channel
        return None

    async def apply_punishment(self, interaction: discord.Interaction, member: discord.Member, amount: int,
                               reason: str, case_number: int, old_amount: int, delete_days: int = 0):
        settings = self.settings_cache.get(interaction.guild.id, {})
        is_simple = settings.get("simple_mode", 0) == 1
        term = "warning" if is_simple else "point"
        errors = []

        action, duration = self.get_punishment_data(amount, interaction.guild.id)
        if not action: return []

        reason_text = f"{term.title()}s: {amount} | {reason or 'No reason provided.'}"

        action_text = action
        if action == "timeout":
            action_text = "timed out"
        elif action == "ban":
            action_text = "banned"
        elif action == "kick":
            action_text = "kicked"
        elif action == "warning":
            action_text = "warned"

        duration_str = format_duration_str(int(duration.total_seconds())) if duration else None

        def build_embed(interaction, action_text, duration_str):
            display_action = action_text.capitalize()
            if "ban" in action_text.lower() and duration_str is None:
                display_action = "Permanently banned"

            if duration_str:
                first_line = f"{display_action} for {duration_str}."
            else:
                first_line = f"{display_action}."

            dm_preposition = "from" if "ban" in action_text.lower() or "kick" in action_text.lower() else "in"

            if duration_str:
                dm_first_line = f"You have been {display_action} {dm_preposition} {interaction.guild.name} for {duration_str}."
            else:
                dm_first_line = f"You have been {display_action} {dm_preposition} {interaction.guild.name}."

            title = f"{action.capitalize()} | Case #{case_number}"
            description = (
                f"* **Old Amount:** {old_amount} {term}(s)\n"
                f"* **New Amount:** {amount} {term}(s)\n"
                f"* **Resulting Action:** {first_line}\n"
                f"* **Reason:** {reason or 'No reason provided.'}"
            )

            dm_title = f"{dm_first_line}"
            dm_description = (
                f"* **Old Amount:** {old_amount} {term}(s)\n"
                f"* **New Amount:** {amount} {term}(s)\n"
                f"* **Reason:** {reason or 'No reason provided.'}"
            )
            is_ban = "ban" in action_text.lower() or "kick" in action_text.lower()
            main_color = discord.Color.red() if is_ban else discord.Color.orange()

            embed = discord.Embed(title=title, description=description, color=main_color)
            embed.set_author(name=f"{member} ({member.id})", icon_url=member.display_avatar.url)
            embed.set_footer(text=f"by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)

            dm_embed = discord.Embed(title=dm_title, description=dm_description, color=main_color)
            dm_embed.set_footer(text=f"by {interaction.user.display_name}",
                                icon_url=interaction.user.display_avatar.url)

            return embed, dm_embed

        log_embed, dm_embed = build_embed(interaction, action_text, duration_str)

        if settings.get("punishment_dm", 1):
            try:
                await member.send(embed=dm_embed)
            except discord.Forbidden:
                errors.append("DM not sent because user's DMs are closed")
            except:
                errors.append("DM not sent")

        try:
            if action == "timeout" and duration:
                await member.timeout(discord.utils.utcnow() + duration, reason=reason_text)
            elif action == "kick":
                await member.kick(reason=reason_text)
            elif action == "ban":
                await interaction.guild.ban(member, reason=reason_text, delete_message_days=delete_days)
                if duration:
                    unban_ts = int((discord.utils.utcnow() + duration).timestamp())
                    async with self.acquire_db() as db:
                        await db.execute(
                            "INSERT OR REPLACE INTO ban_schedule (guild_id, user_id, unban_at) VALUES (?, ?, ?)",
                            (interaction.guild.id, member.id, unban_ts)
                        )
                        await db.commit()
        except discord.Forbidden:
            errors.append("Punishment failed (Missing Permissions)")
        except Exception as e:
            errors.append(f"Punishment error: {str(e)}")

        if settings.get("punishment_log", 1):
            log_ch = await self.get_log_channel(interaction.guild)
            if log_ch:
                await log_ch.send(embed=log_embed)

        return errors

    @tasks.loop(seconds=60)
    async def unban_loop(self):
        await self.bot.db.wait_ready()
        now = int(discord.utils.utcnow().timestamp())
        rows = await self.bot.db.execute(
                "SELECT guild_id, user_id FROM ban_schedule WHERE unban_at <= ?",
                (now,)
        )
        for row in rows:
            guild_id = row["guild_id"]
            user_id = row["user_id"]
            guild = self.bot.get_guild(guild_id) or await self.bot.fetch_guild(guild_id)
            if guild:
                try:
                    await guild.unban(discord.Object(id=user_id), reason="Temporary ban expired")

                    settings = self.settings_cache.get(guild_id, {})
                    rejoin_pts = settings.get("rejoin_points", 4)
                    if rejoin_pts != -1:
                        await self.update_user_points(guild_id, user_id, rejoin_pts)

                except discord.NotFound:
                    pass
                except Exception as e:
                    print(f"Error unbanning {user_id} in {guild_id}: {e}")

            await self.bot.db.execute(
                "DELETE FROM ban_schedule WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id)
            )

    @tasks.loop(hours=6)
    async def decay_loop(self):
        await self.bot.db.wait_ready()
        now = int(discord.utils.utcnow().timestamp())

        guild_decays = {}

        for key, data in list(self.user_cache.items()):
            guild_id_str, user_id_str = key.split(":")
            guild_id, user_id = int(guild_id_str), int(user_id_str)

            points = data["points"]
            last_p = data["last_punishment"]
            last_d = data["last_decay"]

            if points <= 0 or not last_p:
                continue

            settings = self.settings_cache.get(guild_id, {})
            days = settings.get("decay_interval", 14)

            if days == 0: continue

            interval_seconds = days * 86400

            reference_ts = last_d if (last_d and last_d > last_p) else last_p

            elapsed = now - reference_ts
            periods = elapsed // interval_seconds

            if periods > 0:
                new_points = max(0, points - periods)
                new_decay_ts = reference_ts + (periods * interval_seconds)
                decayed_amount = points - new_points
                new_total_decayed = data.get('total_decayed', 0) + decayed_amount

                data["points"] = new_points
                data["last_decay"] = new_decay_ts if new_points > 0 else None
                data["total_decayed"] = new_total_decayed

                await self.bot.db.execute('''
                                 UPDATE moderation_users
                                 SET points        = ?,
                                     last_decay    = ?,
                                     total_decayed = ?
                                 WHERE guild_id = ?
                                   AND user_id = ?
                                 ''', (new_points, data["last_decay"], new_total_decayed, guild_id, user_id))

                if guild_id not in guild_decays:
                    guild_decays[guild_id] = []
                guild_decays[guild_id].append((user_id, decayed_amount))

        for guild_id, decays in guild_decays.items():
            settings = self.settings_cache.get(guild_id, {})
            if settings.get("decay_log_enabled", 0) == 1:
                log_ch = await self.get_log_channel(self.bot.get_guild(guild_id))
                if log_ch:
                    is_simple = settings.get("simple_mode", 0) == 1
                    term = "warning" if is_simple else "point"
                    embed_title = f"Moderation - {term.title()}s Decay Summary"
                    embed_desc = f"Total users decayed: **{len(decays)}**\n\n"
                    
                    for user_id, amount in decays:
                        user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                        embed_desc += f"* {user.mention} (`{user.id}`): **-{amount}** {term}(s)\n"
                    
                    embed = discord.Embed(tite=embed_title, description=embed_desc, color=discord.Color.blue())
                    try:
                        await log_ch.send(embed=embed)
                    except Exception as e:
                        if is_access_error(e):
                            await report_access_failure(self.bot, guild_id, "logging")

        guild_ids = set()
        for key in self.user_cache:
            g_id_str, _ = key.split(":")
            guild_ids.add(int(g_id_str))
        for guild_id in guild_ids:
            await self.refresh_live_case_views(guild_id)



    async def delete_days_autocomplete(self, interaction: discord.Interaction, current: str) -> List[
        app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=opt, value=opt)
            for opt in DELETE_OPTIONS.keys()
            if opt.lower().startswith(current.lower())
        ]

    async def is_user_pending(self, guild_id: int, user_id: int) -> bool:
        async with self.acquire_db() as db:
            async with db.execute(
                    "SELECT 1 FROM pending_punishments WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id)
            ) as cursor:
                row = await cursor.fetchone()
                return row is not None

    mod_group = beacon_commands.Group(name="moderation", description="Moderation system settings", permissions_preset="moderator")

    @mod_group.command(name="dashboard", description="Open the moderation dashboard.")
    async def moderation_dashboard(self, interaction: discord.Interaction):
        await self.guild_setup(interaction)
        if interaction.guild.id not in self.settings_cache:
            async with self.acquire_db() as db:
                await db.execute("INSERT OR IGNORE INTO settings (guild_id) VALUES (?)", (interaction.guild.id,))
                await db.commit()
            self.settings_cache[interaction.guild.id] = {"punishment_dm": 1, "punishment_log": 1, "simple_mode": 0,
                                                         "decay_interval": 14, "rejoin_points": 4}
        await self.apply_default_actions(interaction.guild.id)
        await interaction.response.send_message(view=ModerationDashboard(interaction.user, self))

    @beacon_commands.command(name="point", description="Add points to a user.", permissions_preset="moderator")
    @app_commands.describe(delete_messages="Wipe message history (Only works if the resulting punishment is a BAN)")
    @app_commands.autocomplete(delete_messages=delete_days_autocomplete)
    async def point(self, interaction: discord.Interaction, member: discord.Member, amount: int,
                    reason: Optional[str] = None, delete_messages: Optional[str] = "Off"):
        if amount <= 0:
            return await interaction.response.send_message("The amount can't be negative!", epehemral=True)
        await self.guild_setup(interaction)
        settings = self.settings_cache.get(interaction.guild.id, {})
        if settings.get("simple_mode", 0) == 1:
            return await interaction.response.send_message("Simple Mode is enabled. Use `/warn` instead.",
                                                           ephemeral=True)
        days_to_delete = DELETE_OPTIONS.get(delete_messages, 0)
        await self._add_infraction(interaction, member, amount, reason, days_to_delete)

    @beacon_commands.command(name="warn", description="Issue a warning (Add 1 or more warnings to user).", permissions_preset="moderator")
    @app_commands.describe(delete_messages="Wipe message history (Only works if the resulting punishment is a BAN)")
    @app_commands.autocomplete(delete_messages=delete_days_autocomplete)
    async def warn(self, interaction: discord.Interaction, member: discord.Member, amount: int = 1, reason: Optional[str] = None,
                   delete_messages: Optional[str] = "Off"):
        if amount <= 0:
            return await interaction.response.send_message("The amount can't be negative!", epehemral=True)
        await self.guild_setup(interaction)
        settings = self.settings_cache.get(interaction.guild.id, {})
        if settings.get("simple_mode", 0) == 0:
            return await interaction.response.send_message("Simple Mode is disabled. Use `/point` instead.",
                                                           ephemeral=True)
        days_to_delete = DELETE_OPTIONS.get(delete_messages, 0)
        await self._add_infraction(interaction, member, amount, reason, days_to_delete)

    async def verify_punishment_permissions(self, interaction: discord.Interaction, target: discord.Member) -> Optional[str]:
        if target.id == interaction.user.id:
            return "You can't punish yourself!"
        if target.id == self.bot.user.id:
            return "You can't use ME to punish ME! 🙃"
        if target.bot:
            return "You can't punish bots!"

        if interaction.user != interaction.guild.owner:
            if target == interaction.guild.owner:
                return "You cannot punish the server owner!"
            if target.top_role >= interaction.user.top_role:
                return "You cannot punish this user because their role is higher than or equal to yours!"

        bot_member = interaction.guild.me
        if target == interaction.guild.owner:
            return "I cannot punish the server owner!"
        if target.top_role >= bot_member.top_role:
            return "I cannot punish this user because my own role is lower than or equal to theirs!"

        perms = bot_member.guild_permissions
        if not perms.moderate_members:
            return "I lack the `Moderate Members` permission to execute timeouts!"
        if not perms.ban_members:
            return "I lack the `Ban Members` permission to execute bans!"
        if not perms.kick_members:
            return "I lack the `Kick Members` permission to execute kicks!"

        return None

    async def _add_infraction(self, interaction: discord.Interaction, member: discord.Member, amount: int, reason: str,
                              delete_days: int = False, new: bool = False):

        permission_error = await self.verify_punishment_permissions(interaction, member)
        if permission_error:
            if interaction.response.is_done():
                await interaction.followup.send(permission_error, ephemeral=True)
            else:
                await interaction.response.send_message(permission_error, ephemeral=True)
            return

        await interaction.response.defer()

        async with self.acquire_db() as db:
            async with db.execute(
                    "SELECT id FROM pending_punishments WHERE guild_id = ? AND user_id = ?",
                    (interaction.guild.id, member.id)) as cursor:
                pending_entry = await cursor.fetchone()

        if pending_entry:
            pending_id = pending_entry[0]
            try:
                await member.timeout(None, reason="Pending punishment resolved by formal infraction.")
            except discord.Forbidden:
                self.bot.logger.warning(f"Failed to remove timeout for {member.id} (Permissions).")
            except Exception as e:
                self.bot.logger.error(f"Error removing timeout for {member.id}: {e}")

            await db.execute("DELETE FROM pending_punishments WHERE id = ?", (pending_id,))
            await db.commit()

        all_errors = []

        data = await self.get_user_data(interaction.guild.id, member.id)
        old_points = max(0, data["points"])
        new_points = max(0, data["points"] + amount)
        now = int(time.time())

        await self.update_user_points(interaction.guild.id, member.id, new_points, punishment_ts=now)

        action, duration = self.get_punishment_data(new_points, interaction.guild.id)
        settings = self.settings_cache.get(interaction.guild.id, {})
        term = "warning" if settings.get("simple_mode", 0) == 1 else "point"

        punishment_duration = int(duration.total_seconds()) if duration else 0
        punishment_text = format_punishment_label(action, duration)

        case_number = await self.record_infraction(
            guild_id=interaction.guild.id,
            user_id=member.id,
            moderator_id=interaction.user.id,
            amount=amount,
            reason=reason,
            punishment_type=action,
            punishment_duration=punishment_duration,
            points_after=new_points,
            created_at=now,
        )

        punishment_errors = await self.apply_punishment(interaction, member, new_points, reason, case_number,
                                                        old_points, delete_days)
        all_errors.extend(punishment_errors)

        embed_desc = (
            f"{member.mention} now has {new_points} {term}(s) – {punishment_text}.\n"
            f"* **Reason:** {reason or 'No reason provided.'}"
        )

        if all_errors:
            error_str = ", ".join(all_errors)
            embed_desc += f"\n* **Errors:** {error_str}"

        embed = discord.Embed(
            description=embed_desc,
            color=discord.Color.red()
        )
        embed.set_author(name=f"{member.display_name} ({member.id})", icon_url=member.display_avatar.url)
        embed.set_footer(text=f"by {interaction.user.display_name} • Case #{case_number}",
                         icon_url=interaction.user.display_avatar.url)

        expires_at = now + 10
        undo_view = UndoActionView(self, case_number, interaction.guild.id, expires_at)

        if not new:
            await interaction.edit_original_response(embed=embed, view=undo_view)
            undo_view.message = await interaction.original_response()
        else:
            undo_view.message = await interaction.followup.send(embed=embed, view=undo_view)

    @beacon_commands.command(name="pardon", description="Remove points/warnings from a user.", permissions_preset="moderator")
    async def pardon(self, interaction: discord.Interaction, member: discord.User, amount: int,
                     reason: Optional[str] = None):
        await self.guild_setup(interaction)
        data = await self.get_user_data(interaction.guild.id, member.id)
        old_points = data["points"]
        new_points = max(0, old_points - amount)

        await self.update_user_points(interaction.guild.id, member.id, new_points)

        settings = self.settings_cache.get(interaction.guild.id, {})
        term = "Warnings" if settings.get("simple_mode", 0) == 1 else "Points"

        embed = discord.Embed(
            description=f"## {term} Updated\n\n{term} removed: **{amount}**\nOld: **{old_points}** | New: **{new_points}**\n\n{f"**Reason**: {reason}" if reason else "**Reason**: No reason provided."}",
            color=discord.Color(0x944ae8)
        )
        embed.set_author(name=f"{member.name} ({member.id})", icon_url=member.display_avatar.url)
        await interaction.response.send_message(embed=embed)

        if settings.get("punishment_log", 1):
            log_ch = await self.get_log_channel(interaction.guild)
            if log_ch:
                log_embed = discord.Embed(
                    description=(f"## {term} Updated\n\n{term} removed: **{amount}**\n\n"
                                 f"Old {term}**: {old_points}**\nNew {term}**: {new_points}**\n\n**Reason**: {reason}"),
                    color=discord.Color(0x944ae8)
                )
                log_embed.set_author(name=f"{member.name} ({member.id})", icon_url=member.display_avatar.url)
                log_embed.set_footer(text=f"by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
                await log_ch.send(embed=log_embed)

    @beacon_commands.command(name="unban", description="Unban a user.", permissions_preset="moderator")
    async def unban(self, interaction: discord.Interaction, user: discord.User, reason: Optional[str] = None):
        await self.guild_setup(interaction)
        try:
            await interaction.guild.unban(user, reason=f"Unbanned by {interaction.user.display_name}: {reason}")

            async with self.acquire_db() as db:
                await db.execute("DELETE FROM ban_schedule WHERE guild_id = ? AND user_id = ?",
                                 (interaction.guild.id, user.id))
                await db.commit()

            settings = self.settings_cache.get(interaction.guild.id, {})
            rejoin_pts = settings.get("rejoin_points", 4)
            if rejoin_pts != -1:
                await self.update_user_points(interaction.guild.id, user.id, rejoin_pts)

            await interaction.response.send_message(
                embed=discord.Embed(description=f"**{user.name}** has been unbanned.", color=discord.Color.green()))
        except discord.NotFound:
            return await interaction.response.send_message("User is not banned.", ephemeral=True)
        except discord.Forbidden:
            return await interaction.response.send_message("I lack permissions to unban.", ephemeral=True)

        if settings.get("punishment_log", 1):
            log_ch = await self.get_log_channel(interaction.guild)
            if log_ch:
                log_embed = discord.Embed(description=f"**{user.name}** has been unbanned.\n\n**Reason**: {reason}",
                                          color=discord.Color(0x944ae8))
                log_embed.set_author(name=f"{user.name} ({user.id})", icon_url=user.display_avatar.url)
                log_embed.set_footer(text=f"by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
                await log_ch.send(embed=log_embed)

    @beacon_commands.command(name="points", description="Show points info.", permissions_preset="moderator")
    async def points_lookup(self, interaction: discord.Interaction, user: discord.User):
        await self.guild_setup(interaction)
        settings = self.settings_cache.get(interaction.guild.id, {})
        if settings.get("simple_mode", 0) == 1:
            return await interaction.response.send_message("Simple Mode is enabled. Use `/warnings` instead.",
                                                           ephemeral=True)
        await self._show_info(interaction, user, "Points")

    @beacon_commands.command(name="warnings", description="Show warnings info.", permissions_preset="moderator")
    async def warnings_lookup(self, interaction: discord.Interaction, user: discord.User):
        await self.guild_setup(interaction)
        settings = self.settings_cache.get(interaction.guild.id, {})
        if settings.get("simple_mode", 0) == 0:
            return await interaction.response.send_message("Simple Mode is disabled. Use `/points` instead.",
                                                           ephemeral=True)
        await self._show_info(interaction, user, "Warnings")

    async def _show_info(self, interaction: discord.Interaction, user: discord.User, term: str):
        data = await self.get_user_data(interaction.guild.id, user.id)

        last_p = f"<t:{data['last_punishment']}:f>" if data['last_punishment'] else "never"
        last_d = f"<t:{data['last_decay']}:f>" if data['last_decay'] else "never"
        total_decayed = data.get('total_decayed', 0)

        embed = discord.Embed(
            description=f"## {term} info\n\n{term}: **{data['points']}**\nTotal {term} decayed: **{total_decayed}**\nLast punishment: **{last_p}**\nLast decay: **{last_d}**",
            color=discord.Color(0x944ae8)
        )
        embed.set_author(name=f"{user.name} ({user.id})", icon_url=user.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    case_group = beacon_commands.Group(
        name="case",
        description="View and manage infraction history",
        permissions_preset="moderator",
    )

    @case_group.command(name="history", description="View all past cases for a user.")
    async def case_history(self, interaction: discord.Interaction, user: discord.User):
        await interaction.response.defer()
        await self.guild_setup(interaction)
        settings = self.settings_cache.get(interaction.guild.id, {})
        is_simple = settings.get("simple_mode", 0) == 1
        term = "warning" if is_simple else "point"

        cases = await self.get_user_infractions(interaction.guild.id, user.id)
        if not cases:
            container = discord.ui.Container()
            container.add_item(discord.ui.TextDisplay(f"## No Cases Found"))
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay(f"No cases found for {user.mention} (`{user.id}`)."))
            view = PrivateLayoutView(interaction.user, timeout=None)
            view.clear_items()
            view.add_item(container)
            return await interaction.edit_original_response(view=view)

        view = CaseUserHistoryPage(
            interaction.user, self, interaction.guild, user.id, cases, term
        )
        await interaction.edit_original_response(view=view)
        view.message = await interaction.original_response()

    @case_group.command(name="view", description="View a specific moderation case by ID.")
    async def case_view(self, interaction: discord.Interaction, case_id: int):
        await self.guild_setup(interaction)
        case = await self.get_infraction(interaction.guild.id, case_id)
        if not case:
            return await interaction.response.send_message(
                f"Case **#{case_id}** not found in this server.", ephemeral=True
            )

        settings = self.settings_cache.get(interaction.guild.id, {})
        is_simple = settings.get("simple_mode", 0) == 1
        term = "warning" if is_simple else "point"

        view = CaseDetailPage(interaction.user, self, interaction.guild, case, term)
        await interaction.response.send_message(view=view)
        view.message = await interaction.original_response()

    @case_group.command(name="users", description="Shows a list of all users who have a moderation case.")
    async def case_all(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.guild_setup(interaction)
        settings = self.settings_cache.get(interaction.guild.id, {})
        is_simple = settings.get("simple_mode", 0) == 1
        term = "warning" if is_simple else "point"

        view = AllActiveInfractionsPage(interaction.user, self, interaction.guild, term)
        await view.initialize()
        await interaction.edit_original_response(view=view)
        view.message = await interaction.original_response()

    @case_group.command(name="all", description="Show a list of every moderation case recorded.")
    async def case_all_list(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.guild_setup(interaction)
        settings = self.settings_cache.get(interaction.guild.id, {})
        is_simple = settings.get("simple_mode", 0) == 1
        term = "warning" if is_simple else "point"

        view = AllCasesPage(interaction.user, self, interaction.guild, term)
        await view.initialize()
        await interaction.edit_original_response(view=view)
        view.message = await interaction.original_response()

    @case_group.command(name="delete", description="Delete a moderation case and adjust the user's warnings/points.")
    async def case_delete(self, interaction: discord.Interaction, case_id: int):
        await self.guild_setup(interaction)
        case = await self.get_infraction(interaction.guild.id, case_id)
        if not case:
            return await interaction.response.send_message(
                f"Case **#{case_id}** not found in this server.", ephemeral=True
            )

        settings = self.settings_cache.get(interaction.guild.id, {})
        is_simple = settings.get("simple_mode", 0) == 1
        term = "warning" if is_simple else "point"
        user_display = await self.resolve_user_display(interaction.guild, case["user_id"])
        mod_display = await self.resolve_user_display(interaction.guild, case["moderator_id"])
        punishment = format_punishment_label(case["punishment_type"], case["punishment_duration"])

        title = f"Delete Case | Case #{case['case_number']}"
        body = (
            f"* **User:** {user_display}\n"
            f"* **Moderator:** {mod_display}\n"
            f"* **Date:** <t:{case['created_at']}:F> (<t:{case['created_at']}:R>)\n"
            f"* **Amount:** +{case['amount']} {term}(s)\n"
            f"* **Total after case:** {case['points_after']} {term}(s)\n"
            f"* **Punishment:** {punishment}\n"
            f"* **Reason:** {case['reason'] or 'No reason provided.'}\n\n"
            f"Choose how to handle this deletion:"
        )

        view = CaseDeleteConfirmationView(
            interaction.user, self, interaction.guild, case, term, body, title
        )
        await interaction.response.send_message(view=view)
        view.message = await interaction.original_response()

    async def report_message_menu(self, interaction: discord.Interaction, message: discord.Message):
        await self.guild_setup(interaction)
        settings = self.settings_cache.get(interaction.guild.id, {})

        if settings.get("msg_report_enabled", 0) == 0:
            return await interaction.response.send_message("Message reporting is currently disabled in this server.",
                                                           ephemeral=True)

        channel_id = settings.get("msg_report_channel")
        if not channel_id:
            return await interaction.response.send_message(
                "The message reporting channel has not been fully set up by administrators yet.", ephemeral=True)

        report_channel = interaction.guild.get_channel(channel_id)

        embed = discord.Embed(
            title="Message Reported",
            description=f"**Message Content:**\n{message.content or '*No text content*'}",
            color=discord.Color.orange()
        )
        embed.set_author(name=f"{message.author.name} ({message.author.id})",
                         icon_url=message.author.display_avatar.url)
        embed.add_field(name="Reported by", value=f"{interaction.user.mention} ({interaction.user.id})", inline=True)
        embed.add_field(name="Channel", value=message.channel.mention, inline=True)
        embed.add_field(name="Jump to Message", value=f"[Click Here]({message.jump_url})", inline=False)

        embed.set_footer(text=f"Author: {message.author.id} | Reporter: {interaction.user.id}")

        if message.attachments:
            embed.add_field(name="Attachments", value="\n".join([a.url for a in message.attachments]), inline=False)

        content = ""
        roles_raw = settings.get("msg_report_roles")
        if roles_raw:
            content = " ".join([f"<@&{r}>" for r in roles_raw.split(",") if r])

        view = ReportActionView(cog=self)

        try:
            await report_channel.send(content=content, embed=embed, view=view)
            await interaction.response.send_message("Message reported to moderators successfully! Thank you for keeping the community safe! ❤️", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                "I lack permissions to send messages to the configured reporting channel.", ephemeral=True)

    async def get_pending_punishments(self, guild_id: int) -> list:
        async with self.acquire_db() as db:
            async with db.execute(
                    "SELECT id, user_id, moderator_id, reason, created_at, timeout_until FROM pending_punishments WHERE guild_id = ? ORDER BY created_at DESC",
                    (guild_id,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [{"id": row[0], "user_id": row[1], "moderator_id": row[2], "reason": row[3], "created_at": row[4], "timeout_until": row[5]} for row in rows]

    async def add_pending_punishment(self, guild_id: int, user_id: int, moderator_id: int, reason: str, created_at: int, timeout_until: int) -> int:
        async with self.acquire_db() as db:
            cursor = await db.execute(
                "INSERT INTO pending_punishments (guild_id, user_id, moderator_id, reason, created_at, timeout_until) VALUES (?, ?, ?, ?, ?, ?)",
                (guild_id, user_id, moderator_id, reason, created_at, timeout_until)
            )
            await db.commit()
            return cursor.lastrowid

    async def remove_pending_punishment(self, guild_id: int, pending_id: int):
        async with self.acquire_db() as db:
            await db.execute(
                "DELETE FROM pending_punishments WHERE guild_id = ? AND id = ?",
                (guild_id, pending_id)
            )
            await db.commit()

    @beacon_commands.command(name="pending", description="Put a user on a 7-day timeout and add to pending punishments list.", permissions_preset="moderator")
    @app_commands.describe(member="The user to put on pending punishment", reason="The reason for the pending punishment")
    async def pending_command(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        await self.guild_setup(interaction)
        if await self.is_user_pending(interaction.guild.id, member.id):
            return await interaction.response.send_message(
                f"{member.mention} is already in the pending list!",
                ephemeral=True
            )
        permission_error = await self.verify_punishment_permissions(interaction, member)
        if permission_error:
            if interaction.response.is_done():
                await interaction.followup.send(permission_error, ephemeral=True)
            else:
                await interaction.response.send_message(permission_error, ephemeral=True)
            return
        now = int(time.time())
        timeout_until = now + 7 * 86400

        await member.timeout(discord.utils.utcnow() + timedelta(days=7), reason=reason)
        
        pending_id = await self.add_pending_punishment(
            guild_id=interaction.guild.id,
            user_id=member.id,
            moderator_id=interaction.user.id,
            reason=reason,
            created_at=now,
            timeout_until=timeout_until
        )

        settings = self.settings_cache.get(interaction.guild.id, {})
        if settings.get("punishment_log", 1):
            log_ch = await self.get_log_channel(interaction.guild)
            if log_ch:
                is_simple = settings.get("simple_mode", 0) == 1
                term = "warning" if is_simple else "point"
                embed = discord.Embed(
                    title="New Pending Punishment",
                    description=f"* **User:** {member.mention} (`{member.id}`)\n* **Moderator:** {interaction.user.mention} (`{interaction.user.id}`)\n* **Reason:** {reason}\n* **Timed out user so that they doesn't break more rules while moderators take their time to take action, timeout ends <t:{timeout_until}:R>.",
                    color=discord.Color.orange()
                )
                embed.set_footer(text="Please take the pending action as soon as possible.")
                await log_ch.send(embed=embed)
        
        await interaction.response.send_message(f"🤐")
        msg = await interaction.original_response()
        await msg.add_reaction("🤐")

    @beacon_commands.command(name="pending-list", description="List all pending punishments.", permissions_preset="moderator")
    async def pending_list_command(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.guild_setup(interaction)
        pending = await self.get_pending_punishments(interaction.guild.id)
        
        view = PendingPunishmentsView(interaction.user, self, interaction.guild, pending)
        await view.build_layout()
        await interaction.edit_original_response(view=view)

    def data_features(self) -> list[DataFeatureMeta]:
        return [DataFeatureMeta(
            feature_id="moderation",
            name="Points",
            user_export=True,
            user_delete=False,
            guild_export=True,
            guild_delete=True,
            user_delete_note="Moderation points and infractions are preserved for server integrity.",
        )]

    async def data_export_user(self, user_id: int, *, guild_ids: list[int] | None) -> DataExportChunk:
        chunk = DataExportChunk(feature_id="moderation")
        async with self.acquire_db() as db:
            if guild_ids is None:
                users = await export_table(db, "SELECT * FROM moderation_users WHERE user_id = ?", (user_id,))
                infractions = await export_table(
                    db, "SELECT * FROM infractions WHERE user_id = ?", (user_id,))
            else:
                placeholders = ",".join("?" * len(guild_ids))
                params = (user_id, *guild_ids)
                users = await export_table(
                    db,
                    f"SELECT * FROM moderation_users WHERE user_id = ? AND guild_id IN ({placeholders})",
                    params,
                )
                infractions = await export_table(
                    db,
                    f"SELECT * FROM infractions WHERE user_id = ? AND guild_id IN ({placeholders})",
                    params,
                )
        for row in users:
            gid = row.pop("guild_id")
            chunk.guild_data.setdefault(gid, {})["points"] = row
        for row in infractions:
            gid = row.pop("guild_id")
            chunk.guild_data.setdefault(gid, {}).setdefault("infractions", []).append(row)
        return chunk

    async def data_export_guild(self, guild_id: int) -> DataExportChunk:
        chunk = DataExportChunk(feature_id="moderation")
        async with self.acquire_db() as db:
            users = await export_table(db, "SELECT * FROM moderation_users WHERE guild_id = ?", (guild_id,))
            actions = await export_table(db, "SELECT * FROM actions WHERE guild_id = ?", (guild_id,))
            ban_schedule = await export_table(
                db, "SELECT * FROM ban_schedule WHERE guild_id = ?", (guild_id,))
            settings = await export_table(db, "SELECT * FROM settings WHERE guild_id = ?", (guild_id,))
            infractions = await export_table(
                db, "SELECT * FROM infractions WHERE guild_id = ?", (guild_id,))
            pending = await export_table(
                db, "SELECT * FROM pending_punishments WHERE guild_id = ?", (guild_id,))
        chunk.guild_data[guild_id] = {
            "users": users,
            "actions": actions,
            "ban_schedule": ban_schedule,
            "settings": settings,
            "infractions": infractions,
            "pending_punishments": pending,
        }
        return chunk

    async def data_delete_user(self, user_id: int, *, guild_ids: list[int] | None, feature_id: str | None) -> DataDeleteResult:
        if feature_id and feature_id != "moderation":
            return DataDeleteResult(feature_id="moderation")
        return DataDeleteResult(
            feature_id="moderation",
            deleted=False,
            message="Moderation points and infractions cannot be deleted per user.",
        )

    async def data_delete_guild(self, guild_id: int, feature_id: str | None) -> DataDeleteResult:
        if feature_id and feature_id != "moderation":
            return DataDeleteResult(feature_id="moderation")
        rows_affected = 0
        async with self.acquire_db() as db:
            for table in ("pending_punishments", "infractions", "ban_schedule", "users", "actions", "settings"):
                cur = await db.execute(f"DELETE FROM {table} WHERE guild_id = ?", (guild_id,))
                rows_affected += cur.rowcount
            await db.commit()
        self.settings_cache.pop(guild_id, None)
        self.action_cache.pop(guild_id, None)
        keys_to_remove = [k for k in self.user_cache if k.startswith(f"{guild_id}:")]
        for key in keys_to_remove:
            self.user_cache.pop(key, None)
        return DataDeleteResult(feature_id="moderation", deleted=True, rows_affected=rows_affected)

    async def data_monitor_guild(self, guild: discord.Guild) -> DataMonitorResult:
        result = DataMonitorResult(feature_id="moderation")
        settings = self.settings_cache.get(guild.id)
        if not settings or settings.get("msg_report_enabled", 0) != 1:
            return result
        channel_id = settings.get("msg_report_channel")
        if not channel_id:
            return result
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                channel = None
        accessible = (
            channel is not None
            and isinstance(channel, discord.abc.GuildChannel)
            and channel.guild.id == guild.id
            and channel.permissions_for(guild.me).view_channel
            and channel.permissions_for(guild.me).send_messages
        )
        if not accessible:
            async with self.acquire_db() as db:
                await db.execute(
                    "UPDATE settings SET msg_report_enabled = 0 WHERE guild_id = ?", (guild.id,))
                await db.commit()
            settings["msg_report_enabled"] = 0
            result.actions.append("disabled_msg_report")
        return result


class PendingPunishmentsView(PrivateLayoutView):
    def __init__(self, user, cog, guild: discord.Guild, pending: list, page: int = 0):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.guild = guild
        self.pending = pending
        self.page = page
        self.per_page = 5
        self.total_pages = max(1, (len(self.pending) - 1) // self.per_page + 1)

    async def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("## Pending Punishments"))

        
        start = self.page * self.per_page
        end = start + self.per_page
        current_pending = self.pending[start:end]
        if not current_pending:
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay("*No pending users for punishment found*"))
        for p in current_pending:
            container.add_item(discord.ui.Separator())
            user = self.guild.get_member(p["user_id"]) or self.cog.bot.get_user(p["user_id"]) or await self.cog.bot.fetch_user(p["user_id"]) or "Unknown User"
            mod = self.guild.get_member(p["moderator_id"]) or self.cog.bot.get_user(p["moderator_id"]) or await self.cog.bot.fetch_user(p["moderator_id"]) or "Unknown Moderator"

            resolve_row = discord.ui.ActionRow()
            remove_btn = discord.ui.Button(label="Remove from Pending", style=discord.ButtonStyle.success)
            remove_btn.callback = self.make_remove_callback(p["id"])
            punish_btn = discord.ui.Button(label="Resolve with Punishment", style=discord.ButtonStyle.danger)
            punish_btn.callback = self.make_punish_callback(p["id"])
            resolve_row.add_item(remove_btn)
            resolve_row.add_item(punish_btn)
            
            container.add_item(discord.ui.TextDisplay(
                    f"### {'{:s} {:s}'.format(getattr(user, 'display_name', 'Unknown'), f'({user.id})' if hasattr(user, 'id') else '')}\n"
                    f"* **Moderator:** {getattr(mod, 'mention', 'Unknown')}\n"
                    f"* **Reason:** {p['reason']}\n"
                    f"* **Created:** <t:{p['created_at']}:f>\n"
                    f"* **Timeout until:** <t:{p['timeout_until']}:f>"))
            container.add_item(resolve_row)

        nav_row = discord.ui.ActionRow()
        prev_btn = discord.ui.Button(emoji="◀️", style=discord.ButtonStyle.primary, disabled=self.page == 0)
        prev_btn.callback = self.prev_page
        next_btn = discord.ui.Button(emoji="▶️", style=discord.ButtonStyle.primary, disabled=self.page >= self.total_pages - 1)
        next_btn.callback = self.next_page
        nav_row.add_item(prev_btn)
        nav_row.add_item(next_btn)
        container.add_item(discord.ui.TextDisplay(f"-# Page {self.page + 1} of {self.total_pages}"))
        container.add_item(discord.ui.Separator())

        container.add_item(nav_row)
        
        self.add_item(container)

    async def prev_page(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.page -= 1
        await self.build_layout()
        await interaction.edit_original_response(view=self)

    async def next_page(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.page += 1
        await self.build_layout()
        await interaction.edit_original_response(view=self)

    def make_remove_callback(self, pending_id: int):
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer()
            await self.cog.remove_pending_punishment(self.guild.id, pending_id)
            self.pending = [p for p in self.pending if p["id"] != pending_id]
            if not self.pending:
                await self.build_layout()
                await interaction.edit_original_response(view=self)

            else:
                self.total_pages = (len(self.pending) - 1) // self.per_page + 1
                if self.page >= self.total_pages:
                    self.page = self.total_pages - 1
                await self.build_layout()
                await interaction.edit_original_response(view=self)
        return callback

    def make_punish_callback(self, pending_id: int):
        async def callback(interaction: discord.Interaction):
            await interaction.response.send_modal(PunishPendingModal(self, pending_id, interaction.guild.id))
        return callback

class PunishPendingModal(discord.ui.Modal):
    def __init__(self, parent_view, pending_id: int, guild_id: int):
        super().__init__(title="Resolve with Punishment")
        self.parent_view = parent_view
        self.pending_id = pending_id
        settings = self.parent_view.cog.settings_cache.get(guild_id, {})
        is_simple = settings.get("simple_mode", 0) == 1
        term = "Warnings" if is_simple else "Points"
        pending = next((p for p in self.parent_view.pending if p["id"] == self.pending_id), None)
        self.amount = discord.ui.TextInput(
            placeholder="Enter a number",
            default="1",
            min_length=1,
            max_length=3
        )

        self.reason = discord.ui.TextInput(
            required=False,
            placeholder="Enter a Reason for this punishment",
            default=pending["reason"] if not pending['reason'] == "No reason provided" else "",
            max_length=256)
        self.delete_messages = discord.ui.Select(
            placeholder="Message Deletion (Only works if resulting action is a BAN)",
            options=[
                discord.SelectOption(label="Off", value="Off"),
                discord.SelectOption(label="Past 1 Day", value="Past 1 Day"),
                discord.SelectOption(label="Past 3 Days", value="Past 3 Days"),
                discord.SelectOption(label="Past 7 Days", value="Past 7 Days"),
            ]
        )
        self.delete_messages.default = "Off"
        self.add_item(discord.ui.Label(text=f"{term} to add", component=self.amount))
        self.add_item(discord.ui.Label(text="Reason", component=self.reason))
        self.add_item(discord.ui.Label(text="Message Removal Options", component=self.delete_messages))

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = int(self.amount.value)
            reason = str(self.reason.value)
            delete_messages = "delete" in self.delete_messages.values
            if amount <= 0:
                return await interaction.response.send_message("Amount must be positive.", ephemeral=True)
        except ValueError:
            return await interaction.response.send_message("Invalid amount.", ephemeral=True)

        pending = next((p for p in self.parent_view.pending if p["id"] == self.pending_id), None)
        if not pending:
            return await interaction.response.send_message("Pending punishment not found.", ephemeral=True)

        member = self.parent_view.guild.get_member(pending["user_id"]) or await self.parent_view.guild.fetch_member(pending["user_id"])
        if not member:
            return await interaction.response.send_message("User not found in the server.", ephemeral=True)

        permission_error = await self.parent_view.cog.verify_punishment_permissions(interaction, member)
        if permission_error:

            if interaction.response.is_done():
                await interaction.followup.send(permission_error, ephemeral=True)
            else:
                await interaction.response.send_message(permission_error, ephemeral=True)
            return

        await self.parent_view.cog.remove_pending_punishment(self.parent_view.guild.id, self.pending_id)
        reason = reason or "No reason provided"
        await self.parent_view.cog._add_infraction(interaction, member, amount, reason, delete_messages=delete_messages, new=True)
        pending = await self.parent_view.cog.get_pending_punishments(interaction.guild.id)
        view = PendingPunishmentsView(interaction.user, self.parent_view.cog, interaction.guild, pending)
        await view.build_layout()
        await interaction.edit_original_response(view=view)


async def setup(bot):
    await bot.add_cog(Moderation(bot))