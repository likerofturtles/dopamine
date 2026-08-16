import asyncio
import json
import re
import time
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any

import discord
from beacon import PrivateLayoutView, beacon_commands
from discord.ext import commands, tasks
from discord.ui import Modal, TextInput

from cogs.embed import UseEmbedPage
from utils.data_handlers import export_table
from utils.data_protocol import DataDeleteResult, DataExportChunk, DataFeatureMeta, DataMonitorResult
from utils.discord_health import is_access_error, report_access_failure


class CreateRepeatingMessageModal(Modal):
    def __init__(self, cog: "RepeatingMessages", channel: discord.TextChannel):
        super().__init__(title=f"Create Message for #{channel.name}")
        self.cog = cog
        self.channel = channel

        self.name_input = TextInput(
            label="Name",
            placeholder="Daily Reminder",
            max_length=50,
            required=True,
        )
        self.frequency_input = TextInput(
            label="Frequency (Minimum 60 Sec.)",
            placeholder="2w 7d 8hr 11m",
            max_length=50,
            required=True,
        )
        self.content_input = TextInput(
            label="Message Content",
            placeholder="Write the message that will be sent...",
            max_length=2000,
            required=True,
            style=discord.TextStyle.paragraph,
        )

        self.add_item(self.name_input)
        self.add_item(self.frequency_input)
        self.add_item(self.content_input)

    async def on_submit(self, interaction: discord.Interaction):
        frequency_seconds = self.cog.parse_frequency(self.frequency_input.value)

        if frequency_seconds is None or frequency_seconds < 60:
            await interaction.response.send_message(
                "Error: Invalid frequency (Min 60s).", ephemeral=True
            )
            return

        guild_id = interaction.guild_id
        message_id = self.cog.get_next_message_id(guild_id)
        current_time = time.time()

        data = {
            "guild_id": guild_id,
            "message_id": message_id,
            "name": self.name_input.value,
            "channel_id": self.channel.id,
            "message_content": self.content_input.value,
            "response_type": "text",
            "embed_content": None,
            "embed_data": None,
            "frequency_seconds": frequency_seconds,
            "next_send_time": current_time,
            "is_active": 1,
            "started_at": current_time
        }

        async with self.cog.acquire_db() as db:
            await db.execute(
                """
                INSERT INTO scheduled_messages
                (guild_id, message_id, name, channel_id, message_content, response_type, embed_content, embed_data,
                 frequency_seconds, next_send_time, is_active, started_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(data.values()),
            )
            await db.commit()

        if guild_id not in self.cog.message_cache:
            self.cog.message_cache[guild_id] = {}
        self.cog.message_cache[guild_id][message_id] = data

        await interaction.response.send_message(
            f"Successfully created **{data['name']}** in {self.channel.mention}!",
            ephemeral=True
        )
        asyncio.create_task(self.cog.send_repeating_messages())


class CreateRepeatingEmbedModal(Modal):
    def __init__(
        self,
        cog: "RepeatingMessages",
        channel: discord.TextChannel,
        embed_content: Optional[str],
        embed_data: Dict[str, Any],
    ):
        super().__init__(title=f"Create Message for #{channel.name}")
        self.cog = cog
        self.channel = channel
        self.embed_content = embed_content
        self.embed_data = embed_data

        self.name_input = TextInput(
            label="Name",
            placeholder="Daily Reminder",
            max_length=50,
            required=True,
        )
        self.frequency_input = TextInput(
            label="Frequency (Minimum 60 Sec.)",
            placeholder="2w 7d 8hr 11m",
            max_length=50,
            required=True,
        )
        self.add_item(self.name_input)
        self.add_item(self.frequency_input)

    async def on_submit(self, interaction: discord.Interaction):
        frequency_seconds = self.cog.parse_frequency(self.frequency_input.value)

        if frequency_seconds is None or frequency_seconds < 60:
            await interaction.response.send_message(
                "Error: Invalid frequency (Min 60s).", ephemeral=True
            )
            return

        guild_id = interaction.guild_id
        message_id = self.cog.get_next_message_id(guild_id)
        current_time = time.time()

        data = {
            "guild_id": guild_id,
            "message_id": message_id,
            "name": self.name_input.value,
            "channel_id": self.channel.id,
            "message_content": None,
            "response_type": "embed",
            "embed_content": self.embed_content,
            "embed_data": json.dumps(self.embed_data, ensure_ascii=False),
            "frequency_seconds": frequency_seconds,
            "next_send_time": current_time,
            "is_active": 1,
            "started_at": current_time
        }

        async with self.cog.acquire_db() as db:
            await db.execute(
                """
                INSERT INTO scheduled_messages
                (guild_id, message_id, name, channel_id, message_content, response_type, embed_content, embed_data,
                 frequency_seconds, next_send_time, is_active, started_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(data.values()),
            )
            await db.commit()

        if guild_id not in self.cog.message_cache:
            self.cog.message_cache[guild_id] = {}
        cached = dict(data)
        cached["embed_data"] = self.embed_data
        self.cog.message_cache[guild_id][message_id] = cached

        await interaction.response.send_message(
            f"Successfully created **{cached['name']}** in {self.channel.mention}!",
            ephemeral=True
        )
        asyncio.create_task(self.cog.send_repeating_messages())

class EditMessageContentModal(Modal):
    def __init__(self, cog: "RepeatingMessages", guild_id: int, message_id: int, current_content: str, parent_view):
        super().__init__(title="Edit Content")
        self.cog = cog
        self.guild_id = guild_id
        self.message_id = message_id
        self.parent_view = parent_view

        self.content_input = TextInput(
            label="New Content",
            default=current_content,
            style=discord.TextStyle.paragraph,
            max_length=2000,
            required=True
        )
        self.add_item(self.content_input)

    async def on_submit(self, interaction: discord.Interaction):
        new_content = self.content_input.value
        async with self.cog.acquire_db() as db:
            await db.execute(
                "UPDATE scheduled_messages SET message_content = ?, response_type = ?, embed_content = NULL, embed_data = NULL "
                "WHERE guild_id = ? AND message_id = ?",
                (new_content, "text", self.guild_id, self.message_id)
            )
            await db.commit()

        self.cog.message_cache[self.guild_id][self.message_id]["message_content"] = new_content
        self.cog.message_cache[self.guild_id][self.message_id]["response_type"] = "text"
        self.cog.message_cache[self.guild_id][self.message_id]["embed_content"] = None
        self.cog.message_cache[self.guild_id][self.message_id]["embed_data"] = None

        self.parent_view.panel_data["message_content"] = new_content
        self.parent_view.panel_data["response_type"] = "text"
        self.parent_view.panel_data["embed_content"] = None
        self.parent_view.panel_data["embed_data"] = None
        self.parent_view.build_layout()
        await interaction.response.edit_message(view=self.parent_view)


class EditFrequencyModal(Modal):
    def __init__(self, cog: "RepeatingMessages", guild_id: int, message_id: int, current_freq_str: str, parent_view):
        super().__init__(title="Edit Frequency")
        self.cog = cog
        self.guild_id = guild_id
        self.message_id = message_id
        self.parent_view = parent_view

        self.freq_input = TextInput(
            label="New Frequency",
            default=current_freq_str,
            placeholder="e.g. 1d 2h",
            style=discord.TextStyle.short,
            required=True
        )
        self.add_item(self.freq_input)

    async def on_submit(self, interaction: discord.Interaction):
        seconds = self.cog.parse_frequency(self.freq_input.value)
        if not seconds or seconds < 60:
            await interaction.response.send_message("Invalid frequency (min 60s).", ephemeral=True)
            return

        now = time.time()
        new_next = now + seconds

        async with self.cog.acquire_db() as db:
            await db.execute(
                "UPDATE scheduled_messages SET frequency_seconds = ?, next_send_time = ? WHERE guild_id = ? AND message_id = ?",
                (seconds, new_next, self.guild_id, self.message_id)
            )
            await db.commit()

        self.cog.message_cache[self.guild_id][self.message_id]["frequency_seconds"] = seconds
        self.cog.message_cache[self.guild_id][self.message_id]["next_send_time"] = new_next
        self.parent_view.panel_data["frequency_seconds"] = seconds
        self.parent_view.build_layout()
        await interaction.response.edit_message(view=self.parent_view)


class RepeatingMessagesDashboard(PrivateLayoutView):
    def __init__(self, user, cog):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("## Repeating Messages Dashboard"))
        container.add_item(discord.ui.TextDisplay(
            "This is the dashboard for Dopamine's Repeating Messages feature. Repeating Messages are repeatedly sent in a channel at a set frequency.\nUse the buttons below to create a new Repeating Message, or to Manage existing ones."))
        container.add_item(discord.ui.Separator())

        row = discord.ui.ActionRow()
        btn_create = discord.ui.Button(label="Create", style=discord.ButtonStyle.primary)
        btn_create.callback = self.create_callback
        btn_manage = discord.ui.Button(label="Manage & Edit", style=discord.ButtonStyle.secondary)
        btn_manage.callback = self.manage_callback
        row.add_item(btn_create)
        row.add_item(btn_manage)
        container.add_item(row)
        self.add_item(container)

    async def create_callback(self, interaction: discord.Interaction):
        view = CreateChannelSelectView(self.user, self.cog)
        await interaction.response.send_message(view=view)

    async def manage_callback(self, interaction: discord.Interaction):
        view = ManagePage(self.user, self.cog, interaction.guild_id)
        await interaction.response.edit_message(view=view)


class CreateChannelSelectView(PrivateLayoutView):

    def __init__(self, user, cog):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()

        self.select = discord.ui.ChannelSelect(
            placeholder="Where should this message be sent?",
            channel_types=[discord.ChannelType.text],
            min_values=1, max_values=1
        )
        self.select.callback = self.select_callback

        row = discord.ui.ActionRow()
        row.add_item(self.select)

        container.add_item(discord.ui.TextDisplay("### Step 1: Select a Channel"))
        container.add_item(discord.ui.TextDisplay("Choose the channel where you want the repeating message to appear."))
        container.add_item(row)
        self.add_item(container)

    async def select_callback(self, interaction: discord.Interaction):
        selected_channel = self.select.values[0]
        view = RepeatingResponseTypeView(self.user, self.cog, selected_channel)
        await interaction.response.edit_message(view=view)


class RepeatingResponseTypeView(PrivateLayoutView):
    def __init__(self, user, cog: "RepeatingMessages", channel: discord.TextChannel):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.channel = channel
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("### Step 2: Select the response type"))
        container.add_item(discord.ui.Separator())

        text_btn = discord.ui.Button(label="Text", style=discord.ButtonStyle.primary)
        embed_btn = discord.ui.Button(label="Embed", style=discord.ButtonStyle.primary)

        async def choose_text(interaction: discord.Interaction):
            modal = CreateRepeatingMessageModal(self.cog, self.channel)
            await interaction.response.send_modal(modal)

        async def choose_embed(interaction: discord.Interaction):
            embeds_cog = self.cog.bot.get_cog("Embeds")
            if embeds_cog is None:
                await interaction.response.send_message(
                    "Embed system is not available right now. Please try again later.", ephemeral=True
                )
                return

            embeds = await embeds_cog.fetch_embeds_for_guild(interaction.guild.id)
            if not embeds:
                await interaction.response.send_message(
                    "No saved embeds found for this server. Use `/embed` to create one first.", ephemeral=True
                )
                return

            async def on_pick(inter: discord.Interaction, content: Optional[str], embed_obj: discord.Embed):
                modal = CreateRepeatingEmbedModal(
                    self.cog,
                    self.channel,
                    content or None,
                    embed_obj.to_dict(),
                )
                await inter.response.send_modal(modal)

            view = UseEmbedPage(
                user=self.user,
                cog=embeds_cog,
                guild_id=interaction.guild.id,
                embeds=embeds,
                returnembed=True,
                on_pick=on_pick,
            )
            await interaction.response.edit_message(view=view)

        text_btn.callback = choose_text
        embed_btn.callback = choose_embed
        row = discord.ui.ActionRow()
        row.add_item(text_btn)
        row.add_item(embed_btn)
        container.add_item(row)
        self.add_item(container)

class ChannelSelectView(PrivateLayoutView):
    def __init__(self, user, cog, guild_id, message_id, previous_view):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.message_id = message_id
        self.previous_view = previous_view
        self.build_layout()

    def build_layout(self):
        container = discord.ui.Container()

        self.select = discord.ui.ChannelSelect(
            placeholder="Select a channel...",
            channel_types=[discord.ChannelType.text],
            min_values=1, max_values=1
        )
        self.select.callback = self.select_callback

        row = discord.ui.ActionRow()
        row.add_item(self.select)
        container.add_item(discord.ui.TextDisplay("### Select a channel for the Repeating Message:"))
        container.add_item(row)
        self.add_item(container)

    async def select_callback(self, interaction: discord.Interaction):
        channel = self.select.values[0]

        async with self.cog.acquire_db() as db:
            await db.execute(
                "UPDATE scheduled_messages SET channel_id = ? WHERE guild_id = ? AND message_id = ?",
                (channel.id, self.guild_id, self.message_id)
            )
            await db.commit()

        self.cog.message_cache[self.guild_id][self.message_id]["channel_id"] = channel.id

        self.previous_view.panel_data["channel_id"] = channel.id
        self.previous_view.build_layout()
        await interaction.response.edit_message(view=self.previous_view)


class ManagePage(PrivateLayoutView):
    def __init__(self, user, cog, guild_id, page=1):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.page = page
        self.items_per_page = 5
        self.build_layout()

    def build_layout(self):
        self.clear_items()

        all_messages = self.cog.message_cache.get(self.guild_id, {})
        sorted_keys = sorted(all_messages.keys())
        total_items = len(sorted_keys)
        total_pages = (total_items + self.items_per_page - 1) // self.items_per_page if total_items > 0 else 1

        start_idx = (self.page - 1) * self.items_per_page
        end_idx = start_idx + self.items_per_page
        current_keys = sorted_keys[start_idx:end_idx]

        panels = [all_messages[k] for k in current_keys]

        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("## Manage Repeating Messages"))
        container.add_item(discord.ui.TextDisplay(
            "List of all existing repeating messages. Click Edit to configure details or the channel."))
        container.add_item(discord.ui.Separator())

        if not panels:
            container.add_item(discord.ui.TextDisplay("*No Repeating Messages found.*"))
        else:
            for idx, panel in enumerate(panels, start_idx + 1):
                p_title = panel['name']
                chan_id = panel['channel_id']
                m_id = panel['message_id']

                btn_edit = discord.ui.Button(label="Edit", style=discord.ButtonStyle.secondary)
                btn_edit.callback = self.create_edit_callback(panel)

                display_text = f"{idx}. **{p_title}** in <#{chan_id}>"
                container.add_item(discord.ui.Section(discord.ui.TextDisplay(display_text), accessory=btn_edit))


            container.add_item(discord.ui.Separator())
            row = discord.ui.ActionRow()

            left_btn = discord.ui.Button(emoji="◀️", style=discord.ButtonStyle.primary, disabled=(self.page <= 1))
            left_btn.callback = self.prev_page
            row.add_item(left_btn)

            go_btn = discord.ui.Button(label=f"Page {self.page} of {total_pages}", style=discord.ButtonStyle.secondary, disabled=(total_pages == 1))

            async def go_to_page_callback(interaction: discord.Interaction):
                modal = GoToPageModal(self, total_pages)
                await interaction.response.send_modal(modal)

            go_btn.callback = go_to_page_callback
            row.add_item(go_btn)

            right_btn = discord.ui.Button(emoji="▶️", style=discord.ButtonStyle.primary,
                                          disabled=(self.page >= total_pages))
            right_btn.callback = self.next_page
            row.add_item(right_btn)

            container.add_item(row)

            row = discord.ui.ActionRow()

            return_btn = discord.ui.Button(emoji=self.cog.bot.back_emoji, label="Back", style=discord.ButtonStyle.secondary)
            return_btn.callback = self.return_home
            row.add_item(return_btn)

            container.add_item(discord.ui.Separator())
            container.add_item(row)

        if not panels and self.page == 1:
            container.add_item(discord.ui.Separator())
            row = discord.ui.ActionRow()
            return_btn = discord.ui.Button(emoji=self.cog.bot.back_emoji, label="Back", style=discord.ButtonStyle.secondary)
            return_btn.callback = self.return_home
            row.add_item(return_btn)
            container.add_item(row)

        self.add_item(container)

    def create_edit_callback(self, panel_data):
        async def callback(interaction: discord.Interaction):
            view = EditPage(self.user, self.cog, self.guild_id, panel_data)
            await interaction.response.edit_message(view=view)

        return callback

    async def prev_page(self, interaction: discord.Interaction):
        self.page -= 1
        self.build_layout()
        await interaction.response.edit_message(view=self)

    async def next_page(self, interaction: discord.Interaction):
        self.page += 1
        self.build_layout()
        await interaction.response.edit_message(view=self)

    async def return_home(self, interaction: discord.Interaction):
        view = RepeatingMessagesDashboard(self.user, self.cog)
        await interaction.response.edit_message(view=view)

class GoToPageModal(Modal):
    def __init__(self, parent_view: "ManagePage", total_pages: int):
        super().__init__(title="Jump to Page")
        self.parent_view = parent_view
        self.total_pages = total_pages

        self.page_input = TextInput(
            label=f"Page Number (1-{total_pages})",
            placeholder="Enter a page number...",
            min_length=1,
            max_length=5,
            required=True,
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
                await interaction.response.send_message(
                    f"Please enter a number between 1 and {self.total_pages}.",
                    ephemeral=True
                )
        except ValueError:
            await interaction.response.send_message(
                "Invalid input. Please enter a valid whole number.",
                ephemeral=True
            )

class EditPage(PrivateLayoutView):
    def __init__(self, user, cog, guild_id, panel_data):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.panel_data = panel_data
        self.build_layout()

    def build_layout(self):
        self.clear_items()

        m_id = self.panel_data['message_id']
        current_data = self.cog.message_cache.get(self.guild_id, {}).get(m_id)
        if current_data:
            self.panel_data = current_data

        status_emoji = "🟢" if self.panel_data['is_active'] else "🔴"
        status_text = "Active" if self.panel_data['is_active'] else "Inactive"

        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay(f"## Edit: {self.panel_data['name']}"))
        container.add_item(discord.ui.Separator())

        fmt_freq = self.cog.format_frequency(self.panel_data['frequency_seconds'])

        next_send_unix = int(self.panel_data['next_send_time'])
        timestamp_str = f"<t:{next_send_unix}:F> (<t:{next_send_unix}:R>)" if self.panel_data[
            'is_active'] else "N/A (Paused)"

        details = (
            f"**State:** {status_text} {status_emoji}\n"
            f"**Channel:** <#{self.panel_data['channel_id']}>\n"
            f"**Frequency:** {fmt_freq}\n"
            f"**Next Send:** {timestamp_str}\n"
            f"**Response Type:** {('Embed' if self.panel_data.get('response_type') == 'embed' else 'Text')}\n"
            f"**Message Content:**\n```\n{(self.panel_data.get('message_content') or self.panel_data.get('embed_content') or '*None*')[:1500]}\n```"
        )
        container.add_item(discord.ui.TextDisplay(details))
        container.add_item(discord.ui.Separator())

        row1 = discord.ui.ActionRow()

        btn_state_label = "Deactivate" if self.panel_data['is_active'] else "Activate"
        btn_state_style = discord.ButtonStyle.secondary if self.panel_data['is_active'] else discord.ButtonStyle.primary

        btn_state = discord.ui.Button(label=btn_state_label, style=btn_state_style)
        btn_state.callback = self.toggle_state_callback

        edit_label = "Edit Embed" if self.panel_data.get('response_type') == 'embed' else "Edit Response"
        edit_style = discord.ButtonStyle.primary if self.panel_data.get('response_type') == 'embed' else discord.ButtonStyle.secondary
        btn_edit_message = discord.ui.Button(label=edit_label, style=edit_style)
        btn_edit_message.callback = self.edit_message_callback

        btn_edit_channel = discord.ui.Button(label="Edit Channel", style=discord.ButtonStyle.secondary)
        btn_edit_channel.callback = self.edit_channel_callback

        btn_frequency = discord.ui.Button(label="Edit Frequency", style=discord.ButtonStyle.secondary)
        btn_frequency.callback = self.edit_duration_callback

        btn_delete = discord.ui.Button(label="Delete", style=discord.ButtonStyle.danger)
        btn_delete.callback = self.delete_callback

        row1.add_item(btn_state)
        row1.add_item(btn_edit_message)
        row1.add_item(btn_edit_channel)
        row1.add_item(btn_frequency)
        row1.add_item(btn_delete)
        container.add_item(row1)

        back_row = discord.ui.ActionRow()
        btn_back = discord.ui.Button(emoji=self.cog.bot.back_emoji, label="Back", style=discord.ButtonStyle.secondary)
        btn_back.callback = self.back_callback
        back_row.add_item(btn_back)
        container.add_item(discord.ui.Separator())
        container.add_item(back_row)

        self.add_item(container)

    async def toggle_state_callback(self, interaction: discord.Interaction):
        new_state = 0 if self.panel_data['is_active'] else 1
        m_id = self.panel_data['message_id']

        now = time.time()
        next_send = now + self.panel_data['frequency_seconds']

        async with self.cog.acquire_db() as db:
            if new_state == 1:
                await db.execute(
                    "UPDATE scheduled_messages SET is_active = 1, started_at = ?, next_send_time = ? WHERE guild_id = ? AND message_id = ?",
                    (now, next_send, self.guild_id, m_id)
                )
                self.cog.message_cache[self.guild_id][m_id]["started_at"] = now
                self.cog.message_cache[self.guild_id][m_id]["next_send_time"] = next_send
            else:
                await db.execute(
                    "UPDATE scheduled_messages SET is_active = 0 WHERE guild_id = ? AND message_id = ?",
                    (self.guild_id, m_id)
                )
            await db.commit()

        self.cog.message_cache[self.guild_id][m_id]["is_active"] = new_state
        self.panel_data["is_active"] = new_state

        self.build_layout()
        await interaction.response.edit_message(view=self)

    async def edit_message_callback(self, interaction: discord.Interaction):
        if self.panel_data.get("response_type") == "embed":
            embeds_cog = self.cog.bot.get_cog("Embeds")
            if embeds_cog is None:
                await interaction.response.send_message(
                    "Embed system is not available right now. Please try again later.", ephemeral=True
                )
                return

            embeds = await embeds_cog.fetch_embeds_for_guild(interaction.guild.id)
            if not embeds:
                await interaction.response.send_message(
                    "No saved embeds found for this server. Use `/embed` to create one first.", ephemeral=True
                )
                return

            async def on_pick(inter: discord.Interaction, content: Optional[str], embed_obj: discord.Embed):
                raw_embed = json.dumps(embed_obj.to_dict(), ensure_ascii=False)
                async with self.cog.acquire_db() as db:
                    await db.execute(
                        "UPDATE scheduled_messages SET response_type = ?, message_content = NULL, embed_content = ?, embed_data = ? "
                        "WHERE guild_id = ? AND message_id = ?",
                        ("embed", content or None, raw_embed, self.guild_id, self.panel_data["message_id"]),
                    )
                    await db.commit()
                cache_item = self.cog.message_cache[self.guild_id][self.panel_data["message_id"]]
                cache_item["response_type"] = "embed"
                cache_item["message_content"] = None
                cache_item["embed_content"] = content or None
                cache_item["embed_data"] = embed_obj.to_dict()
                self.panel_data = cache_item
                self.build_layout()
                await inter.response.edit_message(view=self)

            view = UseEmbedPage(
                user=self.user,
                cog=embeds_cog,
                guild_id=interaction.guild.id,
                embeds=embeds,
                returnembed=True,
                on_pick=on_pick,
            )
            await interaction.response.edit_message(view=view)
            return

        modal = EditMessageContentModal(
            self.cog, self.guild_id, self.panel_data['message_id'],
            self.panel_data.get('message_content') or "", self
        )
        await interaction.response.send_modal(modal)

    async def edit_channel_callback(self, interaction: discord.Interaction):
        view = ChannelSelectView(
            self.user, self.cog, self.guild_id, self.panel_data['message_id'], self
        )
        await interaction.response.edit_message(view=view)

    async def edit_duration_callback(self, interaction: discord.Interaction):
        current_freq_str = self.cog.format_frequency(self.panel_data['frequency_seconds'])
        modal = EditFrequencyModal(
            self.cog, self.guild_id, self.panel_data['message_id'],
            current_freq_str, self
        )
        await interaction.response.send_modal(modal)

    async def delete_callback(self, interaction: discord.Interaction):
        view = DestructiveConfirmationView(
            self.user, self.panel_data['name'], self.cog, self.guild_id, self.panel_data['message_id']
        )
        await interaction.response.send_message(view=view)

    async def back_callback(self, interaction: discord.Interaction):
        view = ManagePage(self.user, self.cog, self.guild_id)
        await interaction.response.edit_message(view=view)


class DestructiveConfirmationView(PrivateLayoutView):
    def __init__(self, user, name, cog, guild_id, message_id):
        super().__init__(user, timeout=30)
        self.name = name
        self.cog = cog
        self.color = None
        self.guild_id = guild_id
        self.message_id = message_id
        self.value = None
        self.title_text = "Delete Repeating Message"
        self.body_text = f"Are you sure you want to permanently delete the repeating message **{name}**? This cannot be undone."
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
        confirm = discord.ui.Button(label="Delete Permanently",
                                    style=discord.ButtonStyle.danger, disabled=is_disabled)

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
        panel_data = self.cog.message_cache.get(self.guild_id, {}).get(self.message_id)
        if panel_data:
            view = EditPage(self.user, self.cog, self.guild_id, panel_data)
            await interaction.response.edit_message(view=view)
        else:
            await self.update_view(interaction, "Action Canceled", discord.Color(0xdf5046))

    async def confirm_callback(self, interaction: discord.Interaction):
        self.value = True

        async with self.cog.acquire_db() as db:
            await db.execute("DELETE FROM scheduled_messages WHERE guild_id = ? AND message_id = ?",
                             (self.guild_id, self.message_id))
            await db.commit()

        if self.guild_id in self.cog.message_cache and self.message_id in self.cog.message_cache[self.guild_id]:
            del self.cog.message_cache[self.guild_id][self.message_id]

        await self.update_view(interaction, "Action Confirmed", discord.Colour.green())

    async def on_timeout(self, interaction: discord.Interaction):
        if self.value is None:
            self.value = False
            await self.update_view(interaction, "Timed Out", discord.Color(0xdf5046))


class RepeatingMessages(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.message_cache: Dict[int, Dict[int, dict]] = {}

    async def cog_load(self):
        await self.bot.db.wait_ready()
        await self.populate_caches()
        if not self.send_repeating_messages.is_running():
            self.send_repeating_messages.start()

    async def cog_unload(self):
        if self.send_repeating_messages.is_running():
            self.send_repeating_messages.cancel()

    @asynccontextmanager
    async def acquire_db(self):
        async with self.bot.db.acquire_db() as db:
            yield db

    async def init_db(self):
        await self.bot.db.wait_ready()

    async def populate_caches(self):
        self.message_cache.clear()
        async with self.acquire_db() as db:
            async with db.execute("SELECT * FROM scheduled_messages") as cursor:
                rows = await cursor.fetchall()
                columns = [column[0] for column in cursor.description]
                for row in rows:
                    data = dict(zip(columns, row))
                    if data.get("response_type") is None:
                        data["response_type"] = "text"
                    raw_embed = data.get("embed_data")
                    if raw_embed:
                        try:
                            data["embed_data"] = json.loads(raw_embed)
                        except Exception:
                            data["embed_data"] = None
                    g_id = data["guild_id"]
                    m_id = data["message_id"]

                    if g_id not in self.message_cache:
                        self.message_cache[g_id] = {}
                    self.message_cache[g_id][m_id] = data

    def get_next_message_id(self, guild_id: int) -> int:
        guild_msgs = self.message_cache.get(guild_id, {})
        if not guild_msgs:
            return 1
        return max(guild_msgs.keys()) + 1

    def parse_frequency(self, frequency_str: str) -> Optional[int]:
        frequency_str = frequency_str.lower().strip()
        units = {
            "s": 1, "sec": 1, "second": 1, "seconds": 1,
            "m": 60, "min": 60, "minute": 60, "minutes": 60,
            "h": 3600, "hour": 3600, "hours": 3600,
            "d": 86400, "day": 86400, "days": 86400,
            "w": 604800, "week": 604800, "weeks": 604800,
            "mon": 2629746, "month": 2629746, "months": 2629746,
            "y": 31556952, "year": 31556952, "years": 31556952,
        }
        total_seconds = 0
        pattern = r"(\d+(?:\.\d+)?)\s*([a-zA-Z]+)"
        matches = re.findall(pattern, frequency_str)
        if not matches: return None
        for number_str, unit in matches:
            try:
                number = float(number_str)
                if unit in units:
                    total_seconds += number * units[unit]
                else:
                    return None
            except ValueError:
                return None
        return int(total_seconds) if total_seconds > 0 else None

    def format_frequency(self, seconds: int) -> str:
        if seconds < 60: return f"{seconds} second{'s' if seconds != 1 else ''}"
        units = [
            (31556952, "year", "years"), (2629746, "month", "months"),
            (604800, "week", "weeks"), (86400, "day", "days"),
            (3600, "hour", "hours"), (60, "minute", "minutes"), (1, "second", "seconds"),
        ]
        parts, remaining = [], seconds
        for unit_seconds, singular, plural in units:
            if remaining >= unit_seconds:
                count = remaining // unit_seconds
                remaining %= unit_seconds
                parts.append(f"{count} {singular if count == 1 else plural}")
        if not parts: return f"{seconds} seconds"
        if len(parts) == 1: return parts[0]
        if len(parts) == 2: return f"{parts[0]} and {parts[1]}"
        return f"{', '.join(parts[:-1])}, and {parts[-1]}"

    repeating = beacon_commands.Group(name="repeating", description="Repeating Message commands", permissions_preset="automation")

    def data_features(self) -> list[DataFeatureMeta]:
        return [DataFeatureMeta(
            feature_id="repeating_messages",
            name="Repeating Messages",
            guild_export=True,
            guild_delete=True,
        )]

    async def data_export_user(self, user_id: int, *, guild_ids: list[int] | None) -> DataExportChunk:
        return DataExportChunk(feature_id="repeating_messages")

    async def data_export_guild(self, guild_id: int) -> DataExportChunk:
        chunk = DataExportChunk(feature_id="repeating_messages")
        async with self.acquire_db() as db:
            rows = await export_table(
                db, "SELECT * FROM scheduled_messages WHERE guild_id = ?", (guild_id,))
        chunk.guild_data[guild_id] = {"scheduled_messages": rows}
        return chunk

    async def data_delete_user(self, user_id: int, *, guild_ids: list[int] | None, feature_id: str | None) -> DataDeleteResult:
        return DataDeleteResult(feature_id="repeating_messages")

    async def data_delete_guild(self, guild_id: int, feature_id: str | None) -> DataDeleteResult:
        if feature_id and feature_id != "repeating_messages":
            return DataDeleteResult(feature_id="repeating_messages")
        async with self.acquire_db() as db:
            cur = await db.execute("DELETE FROM scheduled_messages WHERE guild_id = ?", (guild_id,))
            await db.commit()
        self.message_cache.pop(guild_id, None)
        return DataDeleteResult(feature_id="repeating_messages", deleted=True, rows_affected=cur.rowcount)

    async def _channel_sendable(self, guild: discord.Guild, channel_id: int) -> bool:
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return False
        if not isinstance(channel, discord.abc.GuildChannel) or channel.guild.id != guild.id:
            return False
        perms = channel.permissions_for(guild.me)
        return perms.view_channel and perms.send_messages

    async def data_monitor_guild(self, guild: discord.Guild) -> DataMonitorResult:
        result = DataMonitorResult(feature_id="repeating_messages")
        for m_id, data in list(self.message_cache.get(guild.id, {}).items()):
            if not data.get("is_active"):
                continue
            if await self._channel_sendable(guild, data["channel_id"]):
                continue
            async with self.acquire_db() as db:
                await db.execute(
                    "UPDATE scheduled_messages SET is_active = 0 WHERE guild_id = ? AND message_id = ?",
                    (guild.id, m_id),
                )
                await db.commit()
            data["is_active"] = 0
            result.actions.append(f"deactivated_message:{m_id}")
        return result

    @repeating.command(name="message", description="Open the Repeating Messages Dashboard")
    async def dashboard(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            view=RepeatingMessagesDashboard(interaction.user, self)
        )

    @tasks.loop(seconds=60)
    async def send_repeating_messages(self):
        now = time.time()
        updates = []

        for guild_id, messages in self.message_cache.items():
            for m_id, data in messages.items():
                if data["is_active"] and now >= data["next_send_time"]:
                    try:
                        channel = self.bot.get_channel(data["channel_id"]) or await self.bot.fetch_channel(data["channel_id"])
                        if channel:
                            if data.get("response_type") == "embed" and data.get("embed_data"):
                                embed_obj = discord.Embed.from_dict(data["embed_data"])
                                await channel.send(content=data.get("embed_content"), embed=embed_obj)
                            else:
                                await channel.send(data.get("message_content"))

                            new_next_send = now + data["frequency_seconds"]
                            updates.append((new_next_send, guild_id, m_id))
                            data["next_send_time"] = new_next_send
                    except Exception as e:
                        if is_access_error(e):
                            data["is_active"] = 0
                            async with self.acquire_db() as db:
                                await db.execute(
                                    "UPDATE scheduled_messages SET is_active = 0 WHERE guild_id = ? AND message_id = ?",
                                    (guild_id, m_id),
                                )
                                await db.commit()
                            await report_access_failure(
                                self.bot, guild_id, "repeating_messages", f"message:{m_id}"
                            )

        if updates:
            async with self.acquire_db() as db:
                await db.executemany(
                    "UPDATE scheduled_messages SET next_send_time = ? WHERE guild_id = ? AND message_id = ?",
                    updates
                )
                await db.commit()


async def setup(bot):
    await bot.add_cog(RepeatingMessages(bot))