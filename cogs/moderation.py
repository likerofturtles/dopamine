import asyncio
import aiosqlite
import discord
import time
import re
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timedelta
from typing import Dict, Optional, List, Any, Union
from contextlib import asynccontextmanager
from config import DB_PATH
from utils.checks import slash_mod_check
from utils.log import LoggingManager


def parse_duration(duration_str: str) -> Optional[int]:
    """Parses a string like '3 days', '1 week' into seconds. Returns None if invalid or 0 if permanent."""
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


class PrivateLayoutView(discord.ui.LayoutView):
    def __init__(self, user, cog, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.cog = cog

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                "This isn't for you!",
                ephemeral=True
            )
            return False
        return True


class ConfirmationView(PrivateLayoutView):
    def __init__(self, user, cog, title_text: str, body_text: str, color: discord.Color = None):
        super().__init__(user, cog, timeout=30)
        self.value = None
        self.title_text = title_text
        self.body_text = body_text
        self.color = color or discord.Color.blue()
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


class ActionModal(discord.ui.Modal):
    def __init__(self, cog, guild_id, is_create=True, existing_action_id=None):
        title = "Create New Action" if is_create else "Edit Action Points"
        super().__init__(title=title)
        self.cog = cog
        self.guild_id = guild_id
        self.is_create = is_create
        self.existing_action_id = existing_action_id

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
            label="Points/Warnings Required",
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
                await interaction.response.send_message(view=view, ephemeral=True)
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
                placeholder="0 to disable. Min 3 days.",
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
                if days < 3 or days > 100:
                    return await interaction.response.send_message(
                        "Decay must be between 3 and 100 days (or 0 to disable).", ephemeral=True)
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


class ModerationDashboard(PrivateLayoutView):
    def __init__(self, user, cog):
        super().__init__(user, cog, timeout=None)
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
            f"* 2-5 {term}: Incremental Timeouts (15m to 1h)\n"
            f"* 6-11 {term}: Incremental Bans (12h to 7d)\n"
            f"* 12 {term}: Permanent Ban\n> The system is completely customizable, and you can customize {term.lower()} amounts for each action or disable an action completely.\n\n"
            "**Core Features:**\n"
            f"* **Decay:** {term} drop by 1 every set frequency (default: two weeks) if no new infractions occur.\n"
            f"* **Rejoin Policy:** Users unbanned via the bot start a set amount to prevent immediate repeat offenses by keeping them on thin ice."
        ))
        container.add_item(discord.ui.Separator())

        values_btn = discord.ui.Button(label=f"Customise {term} System", style=discord.ButtonStyle.primary)
        values_btn.callback = self.go_to_customisation

        settings_btn = discord.ui.Button(label="Settings", style=discord.ButtonStyle.secondary)
        settings_btn.callback = self.go_to_settings

        row = discord.ui.ActionRow()
        row.add_item(values_btn)
        row.add_item(settings_btn)
        container.add_item(row)
        self.add_item(container)

    async def go_to_customisation(self, interaction: discord.Interaction):
        view = CustomisationPage(self.user, self.cog)
        await interaction.response.edit_message(view=view)

    async def go_to_settings(self, interaction: discord.Interaction):
        view = SettingsPage(self.user, self.cog)
        await interaction.response.edit_message(view=view)


class SettingsPage(PrivateLayoutView):
    def __init__(self, user, cog):
        super().__init__(user, cog, timeout=None)
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        guild_id = self.user.guild.id
        settings = self.cog.settings_cache.get(guild_id, {"punishment_dm": 1, "punishment_log": 1, "simple_mode": 0,
                                                          "decay_interval": 14, "rejoin_points": 4})

        dm_on = settings.get("punishment_dm", 1) == 1
        log_on = settings.get("punishment_log", 1) == 1
        simple_on = settings.get("simple_mode", 0) == 1
        decay_val = settings.get("decay_interval", 14)
        rejoin_val = settings.get("rejoin_points", 4)
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

        container.add_item(discord.ui.Section(discord.ui.TextDisplay(
            f"* **Decay Frequency:** Edit the frequency at which one {'warning' if simple_on else 'point'} is decayed from a user. Current: **{'Disabled' if decay_val == 0 else f'{decay_val} Days'}**."),
                                              accessory=decay_btn))

        container.add_item(
            discord.ui.Section(discord.ui.TextDisplay(
                """* **Simple Mode:**\n  * **Terminology:** Replaces "point" with "warning" and replaces `/point` command with `/warn` (single strike at a time only)\n  * The following simple five-strike preset is applied:\n    * 1 warning: Verbal warning, no punishment\n    * 2 warnings: 60-minute timeout/mute\n    * 3 warnings: 12-hour ban\n    * 4 warnings: 7-day ban\n    * 5 warnings: Permanent ban\n  * **Best For:** Users seeking a traditional moderation feel while retaining Dopamine’s decay and rejoin policies without the learning curve. (Note: Customization of actions and point/warning thresholds is still available in Simple Mode!)"""),
                accessory=simple_btn))

        container.add_item(
            discord.ui.Section(discord.ui.TextDisplay(
                "* **Mod Logs:** Logs Moderation actions in the logging channel (if a channel is set using `/logging set`)."),
                accessory=log_btn))

        container.add_item(discord.ui.Section(discord.ui.TextDisplay(
            f"* **Rejoin Points:** Edit the number of points that a user is given upon joining after being banned. Set it to `preserve` to preserve their points. Current: **{rejoin_str}**"),
            accessory=rejoin_btn))

        container.add_item(
            discord.ui.Section(discord.ui.TextDisplay("* **Punishment DMs:** Sends a DM to the user who is punished."),
                               accessory=dm_btn))

        container.add_item(discord.ui.Separator())
        return_btn = discord.ui.Button(label="Return to Dashboard", style=discord.ButtonStyle.secondary)
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
        super().__init__(user, cog, timeout=None)
        self.page = page
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

            container.add_item(discord.ui.TextDisplay(f"-# Page {self.page} of {total_pages}"))
            container.add_item(discord.ui.Separator())

            nav_row = discord.ui.ActionRow()

            left_btn = discord.ui.Button(label="◀️", style=discord.ButtonStyle.primary, disabled=(self.page <= 1))
            left_btn.callback = self.prev_page
            nav_row.add_item(left_btn)

            go_btn = discord.ui.Button(label="Go To Page", style=discord.ButtonStyle.secondary,
                                       disabled=(total_pages == 1))
            go_btn.callback = self.go_to_page_callback
            nav_row.add_item(go_btn)

            right_btn = discord.ui.Button(label="▶️", style=discord.ButtonStyle.primary,
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
        return_btn = discord.ui.Button(label="Return to Dashboard", style=discord.ButtonStyle.secondary)
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


class Points(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.user_cache: Dict[str, Dict[str, Any]] = {}
        self.action_cache: Dict[int, List[Dict[str, Any]]] = {}
        self.settings_cache: Dict[int, Dict[str, Any]] = {}

        self.db_pool: Optional[asyncio.Queue] = None

    async def cog_load(self):
        await self.init_pools()
        await self.init_db()
        await self.populate_caches()
        self.unban_loop.start()
        self.decay_loop.start()

    async def cog_unload(self):
        self.unban_loop.stop()
        self.decay_loop.stop()
        if self.db_pool:
            while not self.db_pool.empty():
                conn = await self.db_pool.get()
                await conn.close()

    async def init_pools(self, pool_size: int = 5):
        if self.db_pool is None:
            self.db_pool = asyncio.Queue(maxsize=pool_size)
            for _ in range(pool_size):
                conn = await aiosqlite.connect(DB_PATH, timeout=5)
                await conn.execute("PRAGMA busy_timeout=5000")
                await conn.execute("PRAGMA journal_mode=WAL")
                await conn.execute("PRAGMA synchronous = NORMAL")
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
            await db.executescript('''
                CREATE TABLE IF NOT EXISTS users (
                    guild_id INTEGER,
                    user_id INTEGER,
                    points INTEGER DEFAULT 0,
                    last_punishment INTEGER,
                    last_decay INTEGER, 
                    PRIMARY KEY (guild_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER,
                    action_type TEXT,
                    duration INTEGER DEFAULT 0,
                    points INTEGER
                );
                CREATE TABLE IF NOT EXISTS ban_schedule (
                    guild_id INTEGER, 
                    user_id INTEGER,
                    unban_at INTEGER,
                    PRIMARY KEY (guild_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS settings (
                    guild_id INTEGER PRIMARY KEY,
                    punishment_dm INTEGER DEFAULT 1,
                    punishment_log INTEGER DEFAULT 1,
                    decay_interval INTEGER DEFAULT 14,
                    rejoin_points INTEGER DEFAULT 4,
                    simple_mode INTEGER DEFAULT 0
                );
            ''')
            await db.commit()

    async def apply_default_actions(self, guild_id: int):
        default_actions = [
            ("warning", 0, 1),
            ("timeout", 900, 2),
            ("timeout", 1800, 3),
            ("timeout", 2700, 4),
            ("timeout", 3600, 5),
            ("ban", 43200, 6),
            ("ban", 43200, 7),
            ("ban", 86400, 8),
            ("ban", 259200, 9),
            ("ban", 604800, 10),
            ("ban", 604800, 11),
            ("ban", 0, 12)
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

    async def populate_caches(self):
        self.user_cache.clear()
        self.action_cache.clear()
        self.settings_cache.clear()

        async with self.acquire_db() as db:
            async with db.execute("SELECT * FROM users") as cursor:
                async for row in cursor:
                    self.user_cache[f"{row[0]}:{row[1]}"] = {
                        "points": row[2],
                        "last_punishment": row[3],
                        "last_decay": row[4]
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

            async with db.execute("SELECT * FROM settings") as cursor:
                async for row in cursor:
                    self.settings_cache[row[0]] = {
                        "punishment_dm": row[1],
                        "punishment_log": row[2],
                        "decay_interval": row[3],
                        "rejoin_points": row[4],
                        "simple_mode": row[5]
                    }

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
            data = {"points": 0, "last_punishment": None, "last_decay": None}
            self.user_cache[key] = data
            async with self.acquire_db() as db:
                await db.execute(
                    "INSERT OR IGNORE INTO users (guild_id, user_id, points) VALUES (?, ?, ?)",
                    (guild_id, user_id, 0)
                )
                await db.commit()
        return self.user_cache[key]

    async def update_user_points(self, guild_id: int, user_id: int, points: int, punishment_ts: Optional[int] = None):
        key = f"{guild_id}:{user_id}"
        data = await self.get_user_data(guild_id, user_id)
        data["points"] = points
        if punishment_ts:
            data["last_punishment"] = punishment_ts
            data["last_decay"] = None

        self.user_cache[key] = data

        async with self.acquire_db() as db:
            await db.execute('''
                             UPDATE users
                             SET points          = ?,
                                 last_punishment = ?,
                                 last_decay      = ?
                             WHERE guild_id = ?
                               AND user_id = ?
                             ''', (points, data["last_punishment"], data["last_decay"], guild_id, user_id))
            await db.commit()

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
        if hasattr(self, 'manager'):
            channel_id = await self.manager.logging_get(guild.id)
            if channel_id:
                channel = self.bot.get_channel(channel_id)
                if not channel:
                    try:
                        channel = await self.bot.fetch_channel(channel_id)
                    except:
                        return None
                return channel
        return None

    async def apply_punishment(self, interaction: discord.Interaction, member: discord.Member, amount: int,
                               reason: str):
        settings = self.settings_cache.get(interaction.guild.id, {})
        is_simple = settings.get("simple_mode", 0) == 1
        term = "warning" if is_simple else "point"

        action, duration = self.get_punishment_data(amount, interaction.guild.id)
        if not action: return

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
            display_action = action_text
            if "ban" in action_text.lower() and duration_str is None:
                display_action = "permanently banned"

            if duration_str:
                first_line = f"{member.mention} has been **{display_action}** for **{duration_str}**."
            else:
                first_line = f"{member.mention} has been **{display_action}.**"

            dm_preposition = "from" if "ban" in action_text.lower() or "kick" in action_text.lower() else "in"

            if duration_str:
                dm_first_line = f"You have been **{display_action}** {dm_preposition} **{interaction.guild.name}** for **{duration_str}**."
            else:
                dm_first_line = f"You have been **{display_action}** {dm_preposition} **{interaction.guild.name}**."

            description = (
                f"User has **{amount}** {term}(s) – {first_line}\n\n"
                f"**Reason:** {reason or 'No reason provided.'}"
            )

            dm_description = (
                f"You have **{amount}** {term}(s) – {dm_first_line}\n\n"
                f"**Reason:** {reason or 'No reason provided.'}"
            )
            is_ban = "ban" in action_text.lower() or "kick" in action_text.lower()
            main_color = discord.Color.red() if is_ban else discord.Color.orange()

            embed = discord.Embed(description=description, color=main_color)
            embed.set_author(name=f"{member} ({member.id})", icon_url=member.display_avatar.url)
            embed.set_footer(text=f"by {interaction.user}", icon_url=interaction.user.display_avatar.url)

            dm_embed = discord.Embed(description=dm_description, color=main_color)
            dm_embed.set_footer(text=f"by {interaction.user}", icon_url=interaction.user.display_avatar.url)

            return embed, dm_embed

        log_embed, dm_embed = build_embed(interaction, action_text, duration_str)

        if settings.get("punishment_dm", 1):
            try:
                await member.send(embed=dm_embed)
            except:
                pass

        try:
            if action == "timeout" and duration:
                await member.timeout(discord.utils.utcnow() + duration, reason=reason_text)
            elif action == "kick":
                await member.kick(reason=reason_text)
            elif action == "ban":
                await interaction.guild.ban(member, reason=reason_text, delete_message_days=0)
                if duration:
                    unban_ts = int((discord.utils.utcnow() + duration).timestamp())
                    async with self.acquire_db() as db:
                        await db.execute(
                            "INSERT OR REPLACE INTO ban_schedule (guild_id, user_id, unban_at) VALUES (?, ?, ?)",
                            (interaction.guild.id, member.id, unban_ts)
                        )
                        await db.commit()
        except discord.Forbidden:
            await interaction.followup.send("Failed to execute punishment. Check my permissions.", ephemeral=True)

        if settings.get("punishment_log", 1):
            log_ch = await self.get_log_channel(interaction.guild)
            if log_ch:
                await log_ch.send(embed=log_embed)

    @tasks.loop(seconds=60)
    async def unban_loop(self):
        now = int(discord.utils.utcnow().timestamp())
        async with self.acquire_db() as db:
            async with db.execute(
                    "SELECT guild_id, user_id FROM ban_schedule WHERE unban_at <= ?",
                    (now,)
            ) as cursor:
                rows = await cursor.fetchall()

            for guild_id, user_id in rows:
                guild = self.bot.get_guild(guild_id)
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

                await db.execute(
                    "DELETE FROM ban_schedule WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id)
                )
            await db.commit()

    @tasks.loop(hours=6)
    async def decay_loop(self):
        now = int(discord.utils.utcnow().timestamp())

        async with self.acquire_db() as db:
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

                    data["points"] = new_points
                    data["last_decay"] = new_decay_ts if new_points > 0 else None

                    await db.execute('''
                                     UPDATE users
                                     SET points     = ?,
                                         last_decay = ?
                                     WHERE guild_id = ?
                                       AND user_id = ?
                                     ''', (new_points, data["last_decay"], guild_id, user_id))

            await db.commit()

    mod_group = app_commands.Group(name="moderation", description="Moderation system settings")

    @mod_group.command(name="dashboard", description="Open the moderation dashboard.")
    @app_commands.check(slash_mod_check)
    async def moderation_dashboard(self, interaction: discord.Interaction):
        if interaction.guild.id not in self.settings_cache:
            async with self.acquire_db() as db:
                await db.execute("INSERT OR IGNORE INTO settings (guild_id) VALUES (?)", (interaction.guild.id,))
                await db.commit()
            self.settings_cache[interaction.guild.id] = {"punishment_dm": 1, "punishment_log": 1, "simple_mode": 0,
                                                         "decay_interval": 14, "rejoin_points": 4}
        await self.apply_default_actions(interaction.guild.id)
        await interaction.response.send_message(view=ModerationDashboard(interaction.user, self))

    @app_commands.command(name="point", description="Add points to a user.")
    @app_commands.check(slash_mod_check)
    async def point(self, interaction: discord.Interaction, member: discord.Member, amount: int,
                    reason: Optional[str] = None):
        settings = self.settings_cache.get(interaction.guild.id, {})
        if settings.get("simple_mode", 0) == 1:
            return await interaction.response.send_message("Simple Mode is enabled. Use `/warn` instead.",
                                                           ephemeral=True)

        await self._add_infraction(interaction, member, amount, reason)

    @app_commands.command(name="warn", description="Issue a warning (Add 1 warning to user).")
    @app_commands.check(slash_mod_check)
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = None):
        settings = self.settings_cache.get(interaction.guild.id, {})
        if settings.get("simple_mode", 0) == 0:
            return await interaction.response.send_message("Simple Mode is disabled. Use `/point` instead.",
                                                           ephemeral=True)

        await self._add_infraction(interaction, member, 1, reason)

    async def _add_infraction(self, interaction: discord.Interaction, member: discord.Member, amount: int, reason: str):
        await interaction.response.defer()

        data = await self.get_user_data(interaction.guild.id, member.id)
        new_points = max(0, data["points"] + amount)
        now = int(time.time())

        await self.update_user_points(interaction.guild.id, member.id, new_points, punishment_ts=now)

        action, duration = self.get_punishment_data(new_points, interaction.guild.id)

        settings = self.settings_cache.get(interaction.guild.id, {})
        term = "warning" if settings.get("simple_mode", 0) == 1 else "point"

        if action:
            act_text = action
            if duration:
                dur_text = format_duration_str(int(duration.total_seconds()))
                punishment_text = f"{act_text} for {dur_text}"
            else:
                punishment_text = f"{act_text}"
        else:
            punishment_text = "No punishment (No threshold reached)"

        embed = discord.Embed(
            description=f"**{member.mention}** now has **{new_points}** {term}(s) – {punishment_text}.\n\n**Reason**: {reason or 'No reason provided.'}",
            color=discord.Color.red()
        )
        embed.set_author(name=f"{member.display_name} ({member.id})", icon_url=member.display_avatar.url)
        embed.set_footer(text=f"by {interaction.user}")

        await interaction.edit_original_response(embed=embed)
        await self.apply_punishment(interaction, member, new_points, reason)

    @app_commands.command(name="pardon", description="Remove points/warnings from a user.")
    @app_commands.check(slash_mod_check)
    async def pardon(self, interaction: discord.Interaction, member: discord.Member, amount: int,
                     reason: Optional[str] = None):
        data = await self.get_user_data(interaction.guild.id, member.id)
        old_points = data["points"]
        new_points = max(0, old_points - amount)

        await self.update_user_points(interaction.guild.id, member.id, new_points)

        settings = self.settings_cache.get(interaction.guild.id, {})
        term = "Warnings" if settings.get("simple_mode", 0) == 1 else "Points"

        embed = discord.Embed(
            description=f"## {term} Updated\n\n{term} removed: **{amount}**\nOld: **{old_points}** | New: **{new_points}**\n\n{f"**Reason**: {reason}" if reason else "**Reason**: No reason provided."}",
            color=discord.Color.blue()
        )
        embed.set_author(name=f"{member.name} ({member.id})", icon_url=member.display_avatar.url)
        await interaction.response.send_message(embed=embed)

        if settings.get("punishment_log", 1):
            log_ch = await self.get_log_channel(interaction.guild)
            if log_ch:
                log_embed = discord.Embed(
                    description=(f"## {term} Updated\n\n{term} removed: **{amount}**\n\n"
                                 f"Old {term}**:{old_points}**\nNew {term}**:{new_points}**\n\n**Reason**: {reason}"),
                    color=discord.Color(0x337fd5)
                )
                log_embed.set_author(name=f"{member.name} ({member.id})", icon_url=member.display_avatar.url)
                log_embed.set_footer(text=f"by {interaction.user}", icon_url=interaction.user.display_avatar.url)
                await log_ch.send(embed=log_embed)

    @app_commands.command(name="unban", description="Unban a user.")
    @app_commands.check(slash_mod_check)
    async def unban(self, interaction: discord.Interaction, user: discord.User, reason: Optional[str] = None):
        try:
            await interaction.guild.unban(user, reason=f"Unbanned by {interaction.user}: {reason}")

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
                                          color=discord.Color(0x337fd5))
                log_embed.set_author(name=f"{user.name} ({user.id})", icon_url=user.display_avatar.url)
                log_embed.set_footer(text=f"by {interaction.user}", icon_url=interaction.user.display_avatar.url)
                await log_ch.send(embed=log_embed)

    @app_commands.command(name="points", description="Show points info.")
    @app_commands.check(slash_mod_check)
    async def points_lookup_slash(self, interaction: discord.Interaction, user: discord.User):
        settings = self.settings_cache.get(interaction.guild.id, {})
        if settings.get("simple_mode", 0) == 1:
            return await interaction.response.send_message("Simple Mode is enabled. Use `/warnings` instead.",
                                                           ephemeral=True)
        await self._show_info(interaction, user, "Points")

    @app_commands.command(name="warnings", description="Show warnings info.")
    @app_commands.check(slash_mod_check)
    async def warnings_lookup(self, interaction: discord.Interaction, user: discord.User):
        settings = self.settings_cache.get(interaction.guild.id, {})
        if settings.get("simple_mode", 0) == 0:
            return await interaction.response.send_message("Simple Mode is disabled. Use `/points` instead.",
                                                           ephemeral=True)
        await self._show_info(interaction, user, "Warnings")

    async def _show_info(self, interaction: discord.Interaction, user: discord.User, term: str):
        data = await self.get_user_data(interaction.guild.id, user.id)

        last_p = f"<t:{data['last_punishment']}:f>" if data['last_punishment'] else "never"
        last_d = f"<t:{data['last_decay']}:f>" if data['last_decay'] else "never"

        embed = discord.Embed(
            description=f"## {term} info\n\n{term}: **{data['points']}**\nLast punishment: **{last_p}**\nLast decay: **{last_d}**",
            color=discord.Color.blue()
        )
        embed.set_author(name=f"{user.name} ({user.id})", icon_url=user.display_avatar.url)
        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(Points(bot))