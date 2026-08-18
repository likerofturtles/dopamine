import re
from contextlib import asynccontextmanager
from typing import Dict

import discord
from beacon import PrivateLayoutView, beacon_commands
from discord.ext import commands, tasks

from utils.discord_health import (
    channel_can_send,
    is_access_error,
    report_access_failure,
    resolve_guild,
    resolve_guild_channel,
)


class MemberTrackerEditModal(discord.ui.Modal, title="Edit Member Tracker Settings"):
    member_goal = discord.ui.TextInput(
        label="Member Goal",
        placeholder="Set a member count goal... (leave blank to keep unchanged)",
        required=False,
        max_length=10
    )
    format_template = discord.ui.TextInput(
        label="Format",
        style=discord.TextStyle.paragraph,
        placeholder="Available: {count}, {remaining}, {goal}, {server}",
        required=False,
        max_length=1000
    )
    embed_color = discord.ui.TextInput(
        label="Embed Color",
        placeholder="Enter a HEX value... (leave blank to keep unchanged)",
        required=False,
        max_length=9
    )

    def __init__(self, cog: "MemberCountTracker", dashboard_view: "TrackerDashboard"):
        super().__init__()
        self.cog = cog
        self.dashboard_view = dashboard_view

    async def on_submit(self, interaction: discord.Interaction):
        updates = []
        guild_id = interaction.guild.id

        if not await self.cog.check_vote_access(interaction.user.id):
            return await interaction.response.send_message("Vote required to use this feature.", ephemeral=True)

        if guild_id not in self.cog.tracker_cache or not self.cog.tracker_cache[guild_id].get('is_active'):
            return await interaction.response.send_message("Tracker not enabled.", ephemeral=True)

        async with self.cog.acquire_db() as db:
            if self.member_goal.value:
                try:
                    goal_val = int(self.member_goal.value)
                    if goal_val <= 0: raise ValueError
                except ValueError:
                    return await interaction.response.send_message("Enter a positive integer for goal.", ephemeral=True)

                await db.execute("UPDATE member_tracker SET member_goal = ? WHERE guild_id = ?", (goal_val, guild_id))
                self.cog.tracker_cache[guild_id]['member_goal'] = goal_val
                updates.append(f"Member goal set to **{goal_val}**")

            if self.format_template.value:
                template = self.format_template.value.strip()
                if not any(token in template for token in ("{count}", "{remaining}", "{goal}", "{server}")):
                    return await interaction.response.send_message(
                        "Invalid format tokens. Use {count}, {remaining}, {goal}, or {server}.", ephemeral=True)

                await db.execute("UPDATE member_tracker SET custom_format = ? WHERE guild_id = ?", (template, guild_id))
                self.cog.tracker_cache[guild_id]['custom_format'] = template
                updates.append("Custom format updated")

            if self.embed_color.value:
                hex_value = self.embed_color.value.strip().lstrip("#")
                if not re.fullmatch(r"[0-9a-fA-F]{6}", hex_value):
                    return await interaction.response.send_message("Invalid hex color.", ephemeral=True)

                color_int = int(hex_value, 16)
                await db.execute("UPDATE member_tracker SET color = ? WHERE guild_id = ?", (color_int, guild_id))
                self.cog.tracker_cache[guild_id]['color'] = color_int
                updates.append(f"Embed color set to `#{hex_value.upper()}`")

            await db.commit()

        self.dashboard_view.build_layout()
        if interaction.response.is_done():
            await interaction.edit_original_response(view=self.dashboard_view)
        else:
            await interaction.response.edit_message(view=self.dashboard_view)


class DestructiveConfirmationView(PrivateLayoutView):
    def __init__(self, user, title_text: str, body_text: str, cog: "MemberCountTracker",
                 dashboard_view: "TrackerDashboard", color: discord.Color = None):
        super().__init__(user=user, timeout=30)
        self.value = None
        self.title_text = title_text
        self.body_text = body_text
        self.cog = cog
        self.dashboard_view = dashboard_view
        self.color = color
        self.message: discord.Message = None
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
        confirm = discord.ui.Button(label="Reset to Default", style=discord.ButtonStyle.red, disabled=is_disabled)

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
        await self.dashboard_view.update_view(interaction)

    async def confirm_callback(self, interaction: discord.Interaction):
        self.value = True

        async with self.cog.acquire_db() as db:
            await db.execute("DELETE FROM member_tracker WHERE guild_id = ?", (interaction.guild.id,))
            await db.commit()

        self.cog.tracker_cache.pop(interaction.guild.id, None)

        await self.update_view(interaction, "Action Confirmed", discord.Color.green())
        await self.dashboard_view.update_view(interaction)

    async def on_timeout(self, interaction: discord.Interaction):
        if self.value is None and self.message:
            self.value = False
            await self.update_view(interaction, "Timed Out", discord.Color(0xdf5046))


class ChannelSelectView(PrivateLayoutView):
    def __init__(self, user, cog: "MemberCountTracker", guild: discord.Guild):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.guild = guild
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()

        self.select = discord.ui.ChannelSelect(
            placeholder="Select a channel...",
            channel_types=[discord.ChannelType.text],
            min_values=1, max_values=1
        )
        self.select.callback = self.select_callback

        row = discord.ui.ActionRow()
        row.add_item(self.select)
        container.add_item(discord.ui.TextDisplay("### Select a Channel"))
        container.add_item(
            discord.ui.TextDisplay("Choose the channel where you want the Member Tracker messages to be posted:"))
        container.add_item(row)
        self.add_item(container)

    async def select_callback(self, interaction: discord.Interaction):
        if not await self.cog.check_vote_access(interaction.user.id):
            return await interaction.response.send_message("Voting required to enable!", ephemeral=True)

        channel_id = self.select.values[0].id
        guild_id = self.guild.id
        count = self.guild.member_count
        default_color = 0x944ae8

        async with self.cog.acquire_db() as db:
            await db.execute('''
                INSERT INTO member_tracker (guild_id, channel_id, is_active, last_member_count, color, exclude_bots)
                VALUES (?, ?, 1, ?, ?, 0)
                ON CONFLICT(guild_id) DO UPDATE SET
                channel_id = excluded.channel_id,
                is_active = 1
            ''', (guild_id, channel_id, count, default_color))
            await db.commit()

        if guild_id not in self.cog.tracker_cache:
            self.cog.tracker_cache[guild_id] = {"guild_id": guild_id, "member_goal": None, "custom_format": None,
                                                "exclude_bots": 0}

        self.cog.tracker_cache[guild_id].update({
            "channel_id": channel_id,
            "is_active": 1,
            "last_member_count": count,
            "color": self.cog.tracker_cache[guild_id].get('color', default_color)
        })

        dashboard = TrackerDashboard(self.cog, self.user, self.guild)
        await interaction.response.edit_message(view=dashboard)

class TrackerDashboard(PrivateLayoutView):
    def __init__(self, cog: "MemberCountTracker", user: discord.User, guild: discord.Guild):
        super().__init__(user=user, timeout=None)
        self.cog = cog
        self.guild = guild
        self.build_layout()

    def build_layout(self):
        self.clear_items()

        guild_id = self.guild.id
        data = self.cog.tracker_cache.get(guild_id, {})
        is_active = data.get('is_active', 0) == 1

        container = discord.ui.Container()

        toggle_btn = discord.ui.Button(
            label=f"{'Disable' if is_active else 'Enable'}",
            style=discord.ButtonStyle.secondary if is_active else discord.ButtonStyle.primary
        )
        toggle_btn.callback = self.toggle_active_callback

        container.add_item(discord.ui.Section(
            discord.ui.TextDisplay("## Member Tracker Dashboard"),
            accessory=toggle_btn
        ))

        container.add_item(discord.ui.TextDisplay(
            "Member Tracker tracks the number of members in the server, and posts a new message in a set channel when the count goes up. You can set a goal and a celebratory message will be posted in the same channel."))

        if is_active:
            container.add_item(discord.ui.Separator())

            channel = self.guild.get_channel(data.get('channel_id'))
            channel_mention = channel.mention if channel else 'Unknown/Deleted'

            container.add_item(discord.ui.TextDisplay(f"**Channel:** {channel_mention}"))

            if data.get('member_goal'):
                container.add_item(discord.ui.TextDisplay(f"### Goal\n{data['member_goal']} members"))

            if data.get('custom_format'):
                container.add_item(discord.ui.TextDisplay(f"### Format\n```{data['custom_format']}```"))

            if data.get('color'):
                hex_color = f"#{data['color']:06X}"
                container.add_item(discord.ui.TextDisplay(f"**Embed Color:** `{hex_color}`"))

            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay(
                """### ➤ DOCUMENTATION\n\n**Available Variables**\n* `{count}` - Current member count of your server\n* `{remaining}` - Members remaining to reach the goal\n* `{goal}` - The member goal you've set\n* `{server}` - Name of your server\n**Example Formats**\n* `🎉 {count} members! Only {remaining} more to go!`\n* `{server} reached {count}! Goal: {goal}`\n**Notes**\n* You can customize it however you want, you don't have to use these examples!\n* `{remaining}` will only work if a goal is set."""))


            edit_btn = discord.ui.Button(label="Edit Goal & Format", style=discord.ButtonStyle.primary)
            edit_btn.callback = self.edit_callback

            chan_btn = discord.ui.Button(label="Edit Channel", style=discord.ButtonStyle.secondary)
            chan_btn.callback = self.edit_channel_callback

            exclude_bots = data.get('exclude_bots', 0) == 1
            bot_btn = discord.ui.Button(
                label=f"{'Include Bots' if exclude_bots else 'Exclude Bots'}",
                style=discord.ButtonStyle.secondary if exclude_bots else discord.ButtonStyle.primary
            )
            bot_btn.callback = self.toggle_bots_callback

            container.add_item(discord.ui.TextDisplay("**Including/Excluding Bots**"))
            container.add_item(discord.ui.Section(discord.ui.TextDisplay("* Use the toggle to choose whether you want to subtract bots from the total member count!"), accessory=bot_btn))
            container.add_item(discord.ui.Separator())
            row = discord.ui.ActionRow()
            row.add_item(edit_btn)
            row.add_item(chan_btn)
            container.add_item(row)

            container.add_item(discord.ui.Separator())

            container.add_item(discord.ui.TextDisplay("### Reset to Default"))
            btn_reset = discord.ui.Button(label="Reset", style=discord.ButtonStyle.secondary)
            btn_reset.callback = self.reset_button_callback

            container.add_item(discord.ui.Section(
                discord.ui.TextDisplay("Click the Reset button to reset everything to default."),
                accessory=btn_reset
            ))

        self.add_item(container)

    async def update_view(self, interaction: discord.Interaction):
        self.build_layout()
        if interaction.response.is_done():
            await interaction.edit_original_response(view=self)
        else:
            await interaction.response.edit_message(view=self)

    async def toggle_active_callback(self, interaction: discord.Interaction):
        guild_id = self.guild.id
        data = self.cog.tracker_cache.get(guild_id, {})

        if data.get('is_active', 0):
            async with self.cog.acquire_db() as db:
                await db.execute("UPDATE member_tracker SET is_active = 0 WHERE guild_id = ?", (guild_id,))
                await db.commit()
            if guild_id in self.cog.tracker_cache:
                self.cog.tracker_cache[guild_id]['is_active'] = 0
            await self.update_view(interaction)
        else:
            view = ChannelSelectView(self.user, self.cog, self.guild)
            await interaction.response.edit_message(view=view)

    async def edit_channel_callback(self, interaction: discord.Interaction):
        view = ChannelSelectView(self.user, self.cog, self.guild)
        await interaction.response.edit_message(view=view)

    async def enable_with_channel_callback(self, interaction: discord.Interaction):
        if not await self.cog.check_vote_access(interaction.user.id):
            return await interaction.response.send_message("Voting required to enable!", ephemeral=True)

        selected_channel = interaction.data['values'][0]
        channel_id = int(selected_channel)
        guild_id = self.guild.id

        count = self.guild.member_count
        default_color = 0x944ae8

        async with self.cog.acquire_db() as db:
            await db.execute('''
                INSERT OR REPLACE INTO member_tracker 
                (guild_id, channel_id, is_active, last_member_count, color, exclude_bots)
                VALUES (?, ?, 1, ?, ?, 0)
            ''', (guild_id, channel_id, count, default_color))
            await db.commit()

        self.cog.tracker_cache[guild_id] = {
            "guild_id": guild_id,
            "channel_id": channel_id,
            "is_active": 1,
            "last_member_count": count,
            "color": default_color,
            "member_goal": None,
            "custom_format": None,
            "exclude_bots": 0
        }

        await self.update_view(interaction)

    async def toggle_bots_callback(self, interaction: discord.Interaction):
        guild_id = self.guild.id
        data = self.cog.tracker_cache.get(guild_id)
        if not data: return

        current_setting = data.get('exclude_bots', 0)
        new_setting = 1 if current_setting == 0 else 0

        if new_setting == 1:
            new_count = len([m for m in self.guild.members if not m.bot])
        else:
            new_count = self.guild.member_count

        async with self.cog.acquire_db() as db:
            await db.execute(
                "UPDATE member_tracker SET exclude_bots = ?, last_member_count = ? WHERE guild_id = ?",
                (new_setting, new_count, guild_id)
            )
            await db.commit()

        self.cog.tracker_cache[guild_id]['exclude_bots'] = new_setting
        self.cog.tracker_cache[guild_id]['last_member_count'] = new_count

        await self.update_view(interaction)

    async def edit_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(MemberTrackerEditModal(self.cog, self))

    async def reset_button_callback(self, interaction: discord.Interaction):
        confirmation = DestructiveConfirmationView(
            user=self.user,
            title_text="Reset Member Tracker?",
            body_text="This will delete all your settings, including goals, custom formats, and disable the tracker. This cannot be undone.",
            cog=self.cog,
            dashboard_view=self
        )
        await interaction.response.edit_message(view=confirmation)


class MemberCountTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.tracker_cache: Dict[int, dict] = {}

    async def cog_load(self):
        await self.bot.db.wait_ready()
        await self.populate_caches()
        if not self.member_count_monitor.is_running():
            self.member_count_monitor.start()

    async def cog_unload(self):
        self.member_count_monitor.cancel()

    @asynccontextmanager
    async def acquire_db(self):
        async with self.bot.db.acquire_db() as db:
            yield db

    async def init_db(self):
        await self.bot.db.wait_ready()

    async def populate_caches(self):
        self.tracker_cache.clear()
        async with self.acquire_db() as db:
            async with db.execute("SELECT * FROM member_tracker WHERE is_active = 1") as cursor:
                rows = cursor.fetchall()
                if rows:
                    columns = [column[0] for column in cursor.description]
                    for row in rows:
                        data = dict(zip(columns, row))
                        self.tracker_cache[data["guild_id"]] = data

    async def check_vote_access(self, user_id: int) -> bool:
        voter_cog = self.bot.get_cog('TopGGVoter')
        return await voter_cog.check_vote_access(user_id) if voter_cog else True

    member = beacon_commands.Group(name="member", description="Member Tracker commands", permissions_preset="automation")
    @member.command(name="tracker", description="Open the dashboard for Member Tracker.")
    async def member_tracker_dashboard(self, interaction: discord.Interaction):
        view = TrackerDashboard(self, interaction.user, interaction.guild)
        await interaction.response.send_message(view=view)

    @tasks.loop(minutes=5)
    async def member_count_monitor(self):
        await self.bot.wait_until_ready()

        active_trackers = list(self.tracker_cache.values())

        for data in active_trackers:
            guild_id = data['guild_id']
            guild = await resolve_guild(self.bot, guild_id, feature_id="member_tracker")
            if not guild:
                continue

            exclude_bots = data.get('exclude_bots', 0)
            if exclude_bots:
                current_count = len([m for m in guild.members if not m.bot])
            else:
                current_count = guild.member_count

            last_count = data.get('last_member_count', 0)

            if current_count is None:
                continue
                
            if current_count <= last_count:
                continue

            channel_id = data['channel_id']
            _, channel = await resolve_guild_channel(
                self.bot, guild_id, channel_id, feature_id="member_tracker"
            )
            if not channel:
                continue

            if not channel_can_send(channel, guild):
                await report_access_failure(self.bot, guild_id, "member_tracker", str(channel_id))
                continue

            fmt = data['custom_format']
            goal = data['member_goal']
            remaining = max(0, goal - current_count) if goal else None

            if fmt:
                msg = fmt.replace('{count}', str(current_count)) \
                    .replace('{server}', guild.name) \
                    .replace('{remaining}', str(remaining) if remaining is not None else "N/A") \
                    .replace('{goal}', str(goal) if goal else "N/A")
            else:
                msg = f"{guild.name} now has **{current_count}** members!"

            embed = discord.Embed(description=msg, color=data['color'] or 0x944ae8)

            try:
                await channel.send(embed=embed)

                async with self.acquire_db() as db:
                    if goal and current_count >= goal:
                        await channel.send(
                            embed=discord.Embed(description=f"Congratulations! Goal of **{goal}** members has been reached! 🎉", color=discord.Color.gold()))
                        await db.execute(
                            "UPDATE member_tracker SET is_active = 0, last_member_count = ? WHERE guild_id = ?",
                            (current_count, guild_id))
                        self.tracker_cache.pop(guild_id, None)
                    else:
                        await db.execute("UPDATE member_tracker SET last_member_count = ? WHERE guild_id = ?",
                                         (current_count, guild_id))
                        self.tracker_cache[guild_id]['last_member_count'] = current_count
                    await db.commit()
            except Exception as e:
                if is_access_error(e):
                    await report_access_failure(self.bot, guild_id, "member_tracker")

    def data_features(self) -> list:
        from utils.data_protocol import DataFeatureMeta
        return [DataFeatureMeta(feature_id="member_tracker", name="Member Tracker", guild_export=True, guild_delete=True)]

    async def data_export_user(self, user_id: int, *, guild_ids: list[int] | None):
        from utils.data_protocol import DataExportChunk
        return DataExportChunk(feature_id="member_tracker")

    async def data_export_guild(self, guild_id: int):
        from utils.data_handlers import export_table
        from utils.data_protocol import DataExportChunk
        chunk = DataExportChunk(feature_id="member_tracker")
        async with self.acquire_db() as db:
            rows = await export_table(db, "SELECT * FROM member_tracker WHERE guild_id = ?", (guild_id,))
        if rows:
            chunk.guild_data[guild_id] = {"tracker": rows[0]}
        return chunk

    async def data_delete_user(self, user_id: int, *, guild_ids: list[int] | None, feature_id: str | None):
        from utils.data_protocol import DataDeleteResult
        return DataDeleteResult(feature_id="member_tracker")

    async def data_delete_guild(self, guild_id: int, feature_id: str | None):
        from utils.data_protocol import DataDeleteResult
        async with self.acquire_db() as db:
            cur = await db.execute("DELETE FROM member_tracker WHERE guild_id = ?", (guild_id,))
            await db.commit()
        self.tracker_cache.pop(guild_id, None)
        return DataDeleteResult(feature_id="member_tracker", deleted=True, rows_affected=cur.rowcount)

    async def data_monitor_guild(self, guild: discord.Guild):
        from utils.data_protocol import DataMonitorResult
        result = DataMonitorResult(feature_id="member_tracker")
        data = self.tracker_cache.get(guild.id)
        if not data:
            return result
        channel_id = data.get("channel_id")
        if not channel_id:
            async with self.acquire_db() as db:
                await db.execute(
                    "UPDATE member_tracker SET is_active = 0 WHERE guild_id = ?", (guild.id,)
                )
                await db.commit()
            self.tracker_cache.pop(guild.id, None)
            result.actions.append("disabled_member_tracker")
            return result
        _, channel = await resolve_guild_channel(
            self.bot, guild.id, channel_id, feature_id="member_tracker"
        )
        if channel and channel_can_send(channel, guild):
            return result
        async with self.acquire_db() as db:
            await db.execute(
                "UPDATE member_tracker SET is_active = 0 WHERE guild_id = ?", (guild.id,)
            )
            await db.commit()
        self.tracker_cache.pop(guild.id, None)
        result.actions.append("disabled_member_tracker")
        return result


async def setup(bot):
    await bot.add_cog(MemberCountTracker(bot))