import asyncio
import json
import time
from typing import Optional, Dict, List, Any

import discord
from beacon import PrivateLayoutView
from beacon import beacon_commands
from discord.ext import commands, tasks

from cogs.embed import UseEmbedPage
from utils.data_handlers import export_table
from utils.data_protocol import DataDeleteResult, DataExportChunk, DataFeatureMeta, DataMonitorResult
from utils.discord_health import is_access_error, report_access_failure


def parse_color(value: str) -> Optional[discord.Color]:
    if not value:
        return None

    val = value.strip().lower()

    if hasattr(discord.Color, val.replace(" ", "_")):
        method = getattr(discord.Color, val.replace(" ", "_"))
        if callable(method):
            try:
                return method()
            except:
                pass

    hex_val = val.lstrip('#')
    if len(hex_val) == 6:
        try:
            return discord.Color(int(hex_val, 16))
        except:
            pass

    if ',' in val:
        try:
            parts = [int(p.strip()) for p in val.split(',')]
            if len(parts) == 3:
                return discord.Color.from_rgb(*parts)
        except:
            pass

    return None


class DestructiveConfirmationView(PrivateLayoutView):
    def __init__(self, user, title_name, cog, guild_id):
        super().__init__(user, timeout=30)
        self.title_name = title_name
        self.cog = cog
        self.color = None
        self.guild_id = guild_id
        self.value = None
        self.title_text = "Delete Sticky Message"
        self.body_text = f"Are you sure you want to permanently delete the sticky message **{title_name}**? This cannot be undone."
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
        await self.cog.delete_panel(self.guild_id, self.title_name)

    async def on_timeout(self, interaction: discord.Interaction):
        if self.value is None:
            self.value = False
            await self.update_view(interaction, "Timed Out", discord.Color(0xdf5046))


class EditPage(PrivateLayoutView):
    def __init__(self, user, cog, guild_id, panel_data):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.panel_data = panel_data
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        p = self.panel_data
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay(f"## Edit: {p['title']}"))
        container.add_item(discord.ui.Separator())

        bots_enabled = p.get('include_bots', 1) == 1
        is_embed_response = p.get("response_type") == "embed"
        conv_mode = p.get('conv_mode', 'Dynamic')
        response_label = "Embed" if is_embed_response else "Text"
        response_preview = (p.get("embed_content") if is_embed_response else p.get("response_text")) or "*None*"
        details = (
            f"**Channel:** <#{p['channel_id']}>\n"
            f"**Response Type:** `{response_label}`\n"
            f"**Conversation Mode:** `{conv_mode}`\n"
            f"**Conversation Duration:** `{p.get('conversation_duration', 10)}s`\n"
            f"**Include Bots:** `{'Yes' if bots_enabled else 'No'}`\n"
            f"**Response Content:** ```{response_preview}```"
        )
        container.add_item(discord.ui.TextDisplay(details))
        container.add_item(discord.ui.Separator())

        row1 = discord.ui.ActionRow()
        if p.get("response_type") == "embed":
            msg_label = "Edit Embed"
            msg_style = discord.ButtonStyle.primary
        else:
            msg_label = "Edit Response"
            msg_style = discord.ButtonStyle.secondary
        btn_edit_message = discord.ui.Button(label=msg_label, style=msg_style)
        btn_edit_message.callback = self.edit_message_callback
        btn_edit_channel = discord.ui.Button(label="Edit Channel", style=discord.ButtonStyle.secondary)
        btn_edit_channel.callback = self.edit_channel_callback
        btn_delete = discord.ui.Button(label="Delete", style=discord.ButtonStyle.danger)
        btn_delete.callback = self.delete_callback
        btn_duration = discord.ui.Button(label="Edit Conversation Duration", style=discord.ButtonStyle.secondary)
        btn_duration.callback = self.edit_duration_callback
        btn_conv_mode = discord.ui.Button(label=f" Conversation Detection Mode: {conv_mode}", style=discord.ButtonStyle.secondary)
        btn_conv_mode.callback = self.toggle_conv_mode_callback
        btn_bots = discord.ui.Button(label=f"{'Disable' if bots_enabled else 'Enable'} Include Bots",
                                     style=discord.ButtonStyle.secondary if bots_enabled else discord.ButtonStyle.primary)
        btn_bots.callback = self.toggle_bots_callback

        row1.add_item(btn_edit_message)
        row1.add_item(btn_edit_channel)
        row1.add_item(btn_delete)
        row1.add_item(btn_bots)
        container.add_item(row1)

        row = discord.ui.ActionRow()

        row.add_item(btn_duration)
        row.add_item(btn_conv_mode)
        container.add_item(row)

        back_row = discord.ui.ActionRow()
        btn_back = discord.ui.Button(emoji=self.cog.bot.back_emoji, label="Back", style=discord.ButtonStyle.secondary)
        btn_back.callback = self.back_callback
        back_row.add_item(btn_back)
        container.add_item(discord.ui.Separator())
        container.add_item(back_row)

        self.add_item(container)

    async def edit_message_callback(self, interaction: discord.Interaction):
        if self.panel_data.get("response_type") == "text":
            modal = StickyTextSetupModal(
                self.cog,
                self.guild_id,
                self.panel_data['channel_id'],
                is_edit=True,
                original_title=self.panel_data['title'],
            )
            await interaction.response.send_modal(modal)
            return

        if self.panel_data.get("response_type") == "embed":
            embeds_cog = self.cog.bot.get_cog("Embeds")
            if embeds_cog is None:
                await interaction.response.send_message(
                    "Embed system is not available right now. Please try again later.",
                    ephemeral=True,
                )
                return

            embeds = await embeds_cog.fetch_embeds_for_guild(interaction.guild.id)
            if not embeds:
                await interaction.response.send_message(
                    "No saved embeds found for this server. Use `/embed` to create one first.",
                    ephemeral=True,
                )
                return

            async def on_pick(inter: discord.Interaction, content: Optional[str], embed_obj: discord.Embed):
                raw_embed = json.dumps(embed_obj.to_dict(), ensure_ascii=False)
                title = self.panel_data["title"]
                await self.cog.bot.db.execute(
                    "UPDATE sticky_panels SET response_type = ?, response_text = NULL, embed_content = ?, embed_data = ? "
                    "WHERE guild_id = ? AND title = ?",
                    ("embed", content or None, raw_embed, self.guild_id, title),
                )

                panel = self.cog.panel_cache[self.guild_id][title]
                panel["response_type"] = "embed"
                panel["response_text"] = None
                panel["embed_content"] = content or None
                panel["embed_data"] = embed_obj.to_dict()
                self.panel_data = panel
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

        modal = StickySetupModal(self.cog, self.guild_id, self.panel_data['channel_id'], is_edit=True,
                                 original_title=self.panel_data['title'])
        await interaction.response.send_modal(modal)

    async def edit_channel_callback(self, interaction: discord.Interaction):
        view = ChannelSelectView(self.user, self.cog, self.guild_id, is_rebind=True,
                                 panel_title=self.panel_data['title'])
        await interaction.response.send_message(view=view,
                                                ephemeral=True)

    async def delete_callback(self, interaction: discord.Interaction):
        view = DestructiveConfirmationView(self.user, self.panel_data['title'], self.cog, self.guild_id)
        await interaction.response.send_message(view=view)

    async def back_callback(self, interaction: discord.Interaction):
        view = ManagePage(self.user, self.cog, self.guild_id)
        await interaction.response.edit_message(view=view)

    async def edit_duration_callback(self, interaction: discord.Interaction):
        modal = DurationModal(self.cog, self.guild_id, self.panel_data['title'], parent_view=self)
        await interaction.response.send_modal(modal)

    async def toggle_bots_callback(self, interaction: discord.Interaction):
        title = self.panel_data['title']
        panel = self.cog.panel_cache[self.guild_id][title]

        new_val = 0 if panel.get('include_bots', 1) else 1

        await self.cog.bot.db.execute("UPDATE sticky_panels SET include_bots = ? WHERE guild_id = ? AND title = ?",
                                     (new_val, self.guild_id, title))

        panel['include_bots'] = new_val
        self.panel_data['include_bots'] = new_val

        self.build_layout()
        await interaction.response.edit_message(view=self)

    async def toggle_conv_mode_callback(self, interaction: discord.Interaction):
        title = self.panel_data['title']
        panel = self.cog.panel_cache[self.guild_id][title]

        new_mode = "One Shot" if panel.get('conv_mode', 'Dynamic') == "Dynamic" else "Dynamic"

        await self.cog.bot.db.execute("UPDATE sticky_panels SET conv_mode = ? WHERE guild_id = ? AND title = ?",
                                     (new_mode, self.guild_id, title))

        panel['conv_mode'] = new_mode
        self.panel_data['conv_mode'] = new_mode

        self.build_layout()
        await interaction.response.edit_message(view=self)


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

        all_panels = self.cog.panel_cache.get(self.guild_id, {})
        sorted_keys = sorted(all_panels.keys())
        total_items = len(sorted_keys)
        total_pages = (total_items + self.items_per_page - 1) // self.items_per_page if total_items > 0 else 1

        start_idx = (self.page - 1) * self.items_per_page
        end_idx = start_idx + self.items_per_page
        current_keys = sorted_keys[start_idx:end_idx]

        panels = [all_panels[k] for k in current_keys]

        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("## Manage Sticky Messages"))
        container.add_item(discord.ui.TextDisplay(
            "List of all existing sticky messages. Click Edit to configure details or the channel."))
        container.add_item(discord.ui.Separator())

        if not panels:
            container.add_item(discord.ui.TextDisplay("*No Sticky Messages found.*"))
        else:
            for idx, panel in enumerate(panels, start_idx + 1):
                p_title = panel['title']
                chan_id = panel['channel_id']

                btn_edit = discord.ui.Button(label="Edit", style=discord.ButtonStyle.secondary)
                btn_edit.callback = self.create_edit_callback(panel)

                display_text = f"{idx}. **{p_title}** in <#{chan_id}>"
                container.add_item(discord.ui.Section(discord.ui.TextDisplay(display_text), accessory=btn_edit))

            container.add_item(discord.ui.Separator())

            nav_row = discord.ui.ActionRow()

            left_btn = discord.ui.Button(emoji="◀️", style=discord.ButtonStyle.primary, disabled=(self.page <= 1))
            left_btn.callback = self.prev_page
            nav_row.add_item(left_btn)

            go_btn = discord.ui.Button(label=f"Page{self.page} of {total_pages}", style=discord.ButtonStyle.secondary,
                                       disabled=(total_pages == 1))
            go_btn.callback = self.go_to_page_callback
            nav_row.add_item(go_btn)

            right_btn = discord.ui.Button(emoji="▶️", style=discord.ButtonStyle.primary,
                                          disabled=(self.page >= total_pages))
            right_btn.callback = self.next_page
            nav_row.add_item(right_btn)

            container.add_item(nav_row)

        container.add_item(discord.ui.Separator())
        footer_row = discord.ui.ActionRow()
        return_btn = discord.ui.Button(emoji=self.cog.bot.back_emoji, label="Back", style=discord.ButtonStyle.secondary)
        return_btn.callback = self.return_home
        footer_row.add_item(return_btn)
        container.add_item(footer_row)

        self.add_item(container)

    def create_edit_callback(self, panel_data):
        async def callback(interaction: discord.Interaction):
            view = EditPage(self.user, self.cog, self.guild_id, panel_data)
            await interaction.response.edit_message(view=view)

        return callback

    async def go_to_page_callback(self, interaction: discord.Interaction):
        all_panels = self.cog.panel_cache.get(self.guild_id, {})
        total_pages = (len(all_panels) + self.items_per_page - 1) // self.items_per_page
        modal = GoToPageModal(self, total_pages)
        await interaction.response.send_modal(modal)

    async def prev_page(self, interaction: discord.Interaction):
        self.page -= 1
        self.build_layout()
        await interaction.response.edit_message(view=self)

    async def next_page(self, interaction: discord.Interaction):
        self.page += 1
        self.build_layout()
        await interaction.response.edit_message(view=self)

    async def return_home(self, interaction: discord.Interaction):
        view = StickyDashboard(self.user, self.cog, self.guild_id)
        await interaction.response.edit_message(view=view)


class GoToPageModal(discord.ui.Modal):
    def __init__(self, parent_view: "ManagePage", total_pages: int):
        super().__init__(title="Jump to Page")
        self.parent_view = parent_view
        self.total_pages = total_pages

        self.page_input = discord.ui.TextInput(
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


class ChannelSelectView(PrivateLayoutView):
    def __init__(self, user, cog, guild_id, is_rebind=False, panel_title=None):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.is_rebind = is_rebind
        self.panel_title = panel_title
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
        container.add_item(discord.ui.TextDisplay("### Step 1: Select a Channel"))
        container.add_item(discord.ui.TextDisplay("Choose the channel where you want the sticky message to appear:"))
        container.add_item(row)
        self.add_item(container)

    async def select_callback(self, interaction: discord.Interaction):
        selected_channel = self.select.values[0]

        if selected_channel.id in self.cog.active_channels:
            existing_panel = self.cog.active_channels[selected_channel.id]

            is_different_sticky = not self.is_rebind or (existing_panel['title'] != self.panel_title)

            if is_different_sticky:
                return await interaction.response.send_message(
                    f"The channel {selected_channel.mention} already has a sticky message named **{existing_panel['title']}**.\n"
                    f"A channel can only have one sticky message at a time.",
                    ephemeral=True
                )
        if self.is_rebind:
            panel = self.cog.panel_cache[self.guild_id][self.panel_title]
            old_channel_id = panel['channel_id']
            panel['channel_id'] = selected_channel.id

            await self.cog.bot.db.execute(
                "UPDATE sticky_panels SET channel_id = ?, last_message_id = NULL WHERE guild_id = ? AND title = ?",
                (selected_channel.id, self.guild_id, self.panel_title)
            )

            self.cog.active_channels.pop(old_channel_id, None)
            self.cog.active_channels[selected_channel.id] = panel

            await interaction.response.send_message(
                content=f"Moved **{self.panel_title}** to {selected_channel.mention}", ephemeral=True)

            new_channel = self.cog.bot.get_channel(selected_channel.id) or await self.cog.bot.fetch_channel(selected_channel.id)
            if new_channel:
                await self.cog.update_sticky_message(panel, new_channel)

        else:
            view = StickyResponseTypeView(self.user, self.cog, self.guild_id, selected_channel.id)
            await interaction.response.edit_message(view=view)


class StickyResponseTypeView(PrivateLayoutView):
    def __init__(self, user, cog, guild_id: int, channel_id: int):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("### Step 2: Select response type"))
        container.add_item(discord.ui.Separator())
        text_btn = discord.ui.Button(label="Text", style=discord.ButtonStyle.primary)
        embed_btn = discord.ui.Button(label="Embed", style=discord.ButtonStyle.primary)

        async def choose_text(interaction: discord.Interaction):
            modal = StickyTextSetupModal(self.cog, self.guild_id, self.channel_id, is_edit=False)
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
                modal = StickyEmbedNameModal(
                    self.cog,
                    self.guild_id,
                    self.channel_id,
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


class StickyDashboard(PrivateLayoutView):
    def __init__(self, user, cog, guild_id):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.panels = self.cog.get_guild_panels(guild_id)
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        has_panels = len(self.panels) > 0

        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("## Sticky Messages Dashboard"))
        container.add_item(discord.ui.TextDisplay("This is the dashboard for Dopamine's Sticky Messages feature. Sticky messages allow you to pin important information at the bottom of a channel."))
        container.add_item(discord.ui.Separator())

        container.add_item(discord.ui.TextDisplay(
                "* **Conversation Detection:** Dopamine automatically detects a conversation if 2 messages are sent within 5 seconds, and pauses sending the sticky message to avoid spam. The duration that Dopamine will wait after the last message can be customized."))

        container.add_item(discord.ui.TextDisplay(
                "* **Conversation Detection Mode:** Choose between **One Shot** (conversation duration isn't extended when a third message is sent after conversation is detected) or **Dynamic** (conversation duration is extended when any new messages are sent while a conversation is happening)."))

        container.add_item(discord.ui.TextDisplay(
                "* **Bot Detection:** Choose whether Dopamine should re-send the sticky message if a bot sends a message or ignore bots."))

        container.add_item(discord.ui.TextDisplay("To customize the above and more for a Sticky Message or to create a new Sticky Message, use the buttons below."))

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
        view = ChannelSelectView(self.user, self.cog, self.guild_id)
        await interaction.response.send_message(view=view, ephemeral=True)

    async def manage_callback(self, interaction: discord.Interaction):
        view = ManagePage(self.user, self.cog, self.guild_id)
        await interaction.response.edit_message(view=view)


class PanelSelectView(PrivateLayoutView):
    def __init__(self, user, panels, placeholder, callback_func):
        super().__init__(user)
        self.placeholder = placeholder
        self.panels = panels
        self.callback_func = callback_func
        self.build_layout()

    def build_layout(self):
        container = discord.ui.Container()
        options = [discord.SelectOption(label=p['title'], value=p['title']) for p in self.panels[:25]]
        select = discord.ui.Select(placeholder=self.placeholder, options=options)
        select.callback = self.select_callback
        row = discord.ui.ActionRow()
        row.add_item(select)
        container.add_item(discord.ui.TextDisplay("### Select the sticky message whose setting you want to change: "))
        container.add_item(row)
        self.add_item(container)

    async def select_callback(self, interaction: discord.Interaction):
        await self.callback_func(interaction, interaction.data['values'][0])


class DurationModal(discord.ui.Modal):
    def __init__(self, cog, guild_id, title_name, parent_view: EditPage):
        super().__init__(title="Edit Duration")
        self.cog = cog
        self.guild_id = guild_id
        self.title_name = title_name
        self.parent_view = parent_view
        self.duration = discord.ui.TextInput(label="Duration (seconds)", placeholder="10", max_length=2)
        self.add_item(self.duration)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            val = int(self.duration.value)
            if not 0 <= val <= 60: raise ValueError
        except ValueError:
            return await interaction.response.send_message("Enter a number between 0 and 60.", ephemeral=True)

        await self.cog.bot.db.execute("UPDATE sticky_panels SET conversation_duration = ? WHERE guild_id = ? AND title = ?",
                                     (val, self.guild_id, self.title_name))

        self.cog.panel_cache[self.guild_id][self.title_name]['conversation_duration'] = val
        self.parent_view.panel_data['conversation_duration'] = val

        self.parent_view.build_layout()
        await interaction.response.edit_message(view=self.parent_view)


class StickyTextSetupModal(discord.ui.Modal):
    def __init__(self, cog, guild_id, channel_id, is_edit=False, original_title=None):
        super().__init__(title="Configure Sticky Text Response")
        self.cog = cog
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.is_edit = is_edit
        self.original_title = original_title

        self.title_input = discord.ui.TextInput(label="Sticky Name (Identifier)", default=original_title or "", required=True)
        self.response_input = discord.ui.TextInput(
            label="Text Response",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=2000,
        )
        self.add_item(self.title_input)
        self.add_item(self.response_input)

        if is_edit:
            data = cog.panel_cache[guild_id].get(original_title, {})
            self.response_input.default = data.get("response_text", "")

    async def on_submit(self, interaction: discord.Interaction):
        title = self.title_input.value.strip()
        response_text = self.response_input.value

        if not title:
            await interaction.response.send_message("Sticky name is required.", ephemeral=True)
            return

        if not self.is_edit and title in self.cog.panel_cache.get(self.guild_id, {}):
            await interaction.response.send_message("A sticky message with that title already exists.", ephemeral=True)
            return

        if self.is_edit:
            panel = self.cog.panel_cache[self.guild_id].get(self.original_title)
            if panel is None:
                await interaction.response.send_message("Sticky message not found.", ephemeral=True)
                return

            await self.cog.bot.db.execute(
                "UPDATE sticky_panels SET title = ?, response_type = ?, response_text = ?, embed_content = NULL, embed_data = NULL "
                "WHERE guild_id = ? AND title = ?",
                (title, "text", response_text, self.guild_id, self.original_title),
            )

            panel["title"] = title
            panel["response_type"] = "text"
            panel["response_text"] = response_text
            panel["embed_content"] = None
            panel["embed_data"] = None

            if title != self.original_title:
                self.cog.panel_cache[self.guild_id][title] = self.cog.panel_cache[self.guild_id].pop(self.original_title)
            self.cog.active_channels[panel["channel_id"]] = panel
            await interaction.response.send_message(f"Sticky message **{title}** updated!", ephemeral=True)
            return

        data = {
            "guild_id": self.guild_id,
            "title": title,
            "embed_color": None,
            "description": None,
            "image_url": None,
            "footer": None,
            "channel_id": self.channel_id,
            "last_message_id": None,
            "conversation_duration": 10,
            "include_bots": 1,
            "panel_id": int(time.time()),
            "response_type": "text",
            "response_text": response_text,
            "embed_content": None,
            "embed_data": None,
            "conv_mode": "Dynamic"
        }

        cols = ", ".join(data.keys())
        placeholders = ", ".join(["?"] * len(data))
        await self.cog.bot.db.execute(f"INSERT INTO sticky_panels ({cols}) VALUES ({placeholders})", list(data.values()))

        self.cog.panel_cache.setdefault(self.guild_id, {})[title] = data
        self.cog.active_channels[self.channel_id] = data
        channel = self.cog.bot.get_channel(self.channel_id) or await self.cog.bot.fetch_channel(self.channel_id)
        if channel:
            await self.cog.update_sticky_message(data, channel)
        await interaction.response.send_message(f"Sticky message **{title}** created!", ephemeral=True)


class StickyEmbedNameModal(discord.ui.Modal):
    def __init__(self, cog, guild_id, channel_id, embed_content: Optional[str], embed_data: Dict[str, Any]):
        super().__init__(title="Name Sticky Embed Response")
        self.cog = cog
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.embed_content = embed_content
        self.embed_data = embed_data
        self.title_input = discord.ui.TextInput(label="Sticky Name (Identifier)", required=True, max_length=100)
        self.add_item(self.title_input)

    async def on_submit(self, interaction: discord.Interaction):
        title = self.title_input.value.strip()
        if not title:
            await interaction.response.send_message("Sticky name is required.", ephemeral=True)
            return
        if title in self.cog.panel_cache.get(self.guild_id, {}):
            await interaction.response.send_message("A sticky message with that title already exists.", ephemeral=True)
            return

        data = {
            "guild_id": self.guild_id,
            "title": title,
            "embed_color": None,
            "description": None,
            "image_url": None,
            "footer": None,
            "channel_id": self.channel_id,
            "last_message_id": None,
            "conversation_duration": 10,
            "include_bots": 1,
            "panel_id": int(time.time()),
            "response_type": "embed",
            "response_text": None,
            "embed_content": self.embed_content,
            "embed_data": json.dumps(self.embed_data, ensure_ascii=False),
            "conv_mode": "Dynamic"
        }

        cols = ", ".join(data.keys())
        placeholders = ", ".join(["?"] * len(data))
        await self.cog.bot.db.execute(f"INSERT INTO sticky_panels ({cols}) VALUES ({placeholders})", list(data.values()))

        cache_data = dict(data)
        cache_data["embed_data"] = self.embed_data
        self.cog.panel_cache.setdefault(self.guild_id, {})[title] = cache_data
        self.cog.active_channels[self.channel_id] = cache_data

        channel = self.cog.bot.get_channel(self.channel_id) or await self.cog.bot.fetch_channel(self.channel_id)
        if channel:
            await self.cog.update_sticky_message(cache_data, channel)
        await interaction.response.send_message(f"Sticky message **{title}** created!", ephemeral=True)


class StickySetupModal(discord.ui.Modal):
    def __init__(self, cog, guild_id, channel_id, is_edit=False, original_title=None):
        super().__init__(title="Configure Sticky Message")
        self.cog = cog
        self.guild_id = guild_id
        self.is_edit = is_edit
        self.channel_id = channel_id
        self.original_title = original_title

        self.color_input = discord.ui.TextInput(label="Embed Color (Hex, RGB, or Name)", placeholder="#FFFFFF or blue",
                                                required=False)
        self.title_input = discord.ui.TextInput(label="Embed Title (Identifier)", default=original_title or "",
                                                required=True)
        self.description_input = discord.ui.TextInput(label="Embed Description", style=discord.TextStyle.paragraph,
                                                      required=False)
        self.footer_input = discord.ui.TextInput(label="Embed Footer", required=False)
        self.image_url_input = discord.ui.TextInput(label="Embed Image URL", required=False)

        self.add_item(self.color_input)
        self.add_item(self.title_input)
        self.add_item(self.description_input)
        self.add_item(self.footer_input)
        self.add_item(self.image_url_input)

        if is_edit:
            data = cog.panel_cache[guild_id].get(original_title, {})
            self.color_input.default = data.get('embed_color', '')
            self.description_input.default = data.get('description', '')
            self.footer_input.default = data.get('footer', '')
            self.image_url_input.default = data.get('image_url', '')

    async def on_submit(self, interaction: discord.Interaction):
        title = self.title_input.value
        color_val = self.color_input.value

        if color_val and not parse_color(color_val):
            return await interaction.response.send_message("Invalid color format provided.", ephemeral=True)

        if not self.is_edit and title in self.cog.panel_cache.get(self.guild_id, {}):
            return await interaction.response.send_message("A sticky message with that title already exists.",
                                                           ephemeral=True)

        data = {
            "guild_id": self.guild_id,
            "title": title,
            "embed_color": color_val or None,
            "description": self.description_input.value or None,
            "image_url": self.image_url_input.value or None,
            "footer": self.footer_input.value or None,
            "channel_id": self.channel_id,
            "last_message_id": None,
            "conversation_duration": 10,
            "include_bots": 1,
            "panel_id": int(time.time())
        }

        if self.is_edit:
            panel = self.cog.panel_cache[self.guild_id].get(self.original_title)

            panel.update({
                "title": title,
                "embed_color": color_val or None,
                "description": self.description_input.value or None,
                "image_url": self.image_url_input.value or None,
                "footer": self.footer_input.value or None,
            })

            if title != self.original_title:
                self.cog.panel_cache[self.guild_id][title] = self.cog.panel_cache[self.guild_id].pop(
                    self.original_title)

            await self.cog.bot.db.execute("""UPDATE sticky_panels SET title=?, description=?, footer=?, image_url=?, embed_color=?
                                WHERE guild_id=? AND title=?""",
                             (title, panel['description'], panel['footer'], panel['image_url'], color_val,
                              self.guild_id, self.original_title))
            msg = f"Sticky message **{title}** updated!"
            data = panel
        else:
            cols = ", ".join(data.keys())
            placeholders = ", ".join(["?"] * len(data))
            await self.cog.bot.db.execute(f"INSERT INTO sticky_panels ({cols}) VALUES ({placeholders})", list(data.values()))
            msg = f"Sticky message **{title}** created!"

        if self.guild_id not in self.cog.panel_cache: self.cog.panel_cache[self.guild_id] = {}
        self.cog.panel_cache[self.guild_id][title] = data
        self.cog.active_channels[self.channel_id] = data
        channel = self.cog.bot.get_channel(self.channel_id) or await self.cog.bot.get_channel(self.channel_id)
        if channel: await self.cog.update_sticky_message(data, channel)
        await interaction.response.send_message(msg, ephemeral=True)


class StickyMessages(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.panel_cache: Dict[int, Dict[str, dict]] = {}
        self.active_channels: Dict[int, dict] = {}
        self.last_message_time: Dict[int, float] = {}
        self.last_activity: Dict[int, float] = {}
        self.sticky_tasks: Dict[int, asyncio.Task] = {}

    async def cog_load(self):
        await self.populate_caches()
        if not self.sticky_monitor.is_running(): self.sticky_monitor.start()

    async def cog_unload(self):
        if self.sticky_monitor.is_running():
            self.sticky_monitor.cancel()

        for t in self.sticky_tasks.values():
            t.cancel()

    async def populate_caches(self):
        rows = await self.bot.db.execute("SELECT * FROM sticky_panels")
        for d in rows:
            if d.get("response_type") is None:
                d["response_type"] = "embed"
            raw_embed = d.get("embed_data")
            if raw_embed:
                try:
                    d["embed_data"] = json.loads(raw_embed)
                except Exception:
                    d["embed_data"] = None
            self.panel_cache.setdefault(d["guild_id"], {})[d["title"]] = d
            if d["channel_id"]:
                self.active_channels[d["channel_id"]] = d

    def get_guild_panels(self, guild_id: int) -> List[dict]:
        return list(self.panel_cache.get(guild_id, {}).values())

    async def delete_panel(self, guild_id: int, title: str):
        panel = self.panel_cache.get(guild_id, {}).pop(title, None)
        if not panel:
            return
        self.active_channels.pop(panel['channel_id'], None)
        await self.bot.db.execute("DELETE FROM sticky_panels WHERE guild_id = ? AND title = ?", (guild_id, title))

    def build_panel_embed(self, data: dict) -> discord.Embed:
        color = parse_color(data.get('embed_color', ''))
        embed = discord.Embed(title=data.get('title'), description=data.get('description'),
                              color=color or discord.Color.default())
        if data.get('image_url'): embed.set_image(url=data['image_url'])
        if data.get('footer'): embed.set_footer(text=data['footer'])
        return embed

    async def sticky_worker(self, channel, panel, delay):
        try:
            await asyncio.sleep(delay)
            await self.update_sticky_message(panel, channel)
        except asyncio.CancelledError:
            pass
        finally:
            if self.sticky_tasks.get(channel.id) == asyncio.current_task():
                self.sticky_tasks.pop(channel.id, None)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.id == self.bot.user.id:
            return

        panel = self.active_channels.get(message.channel.id)
        if not panel:
            return

        if message.author.bot and not panel.get('include_bots', 1):
            return

        current_time = time.time()
        last_time = self.last_message_time.get(message.channel.id, 0)
        self.last_message_time[message.channel.id] = current_time

        conv_mode = panel.get('conv_mode', 'Dynamic')

        if conv_mode == 'One Shot' and message.channel.id in self.sticky_tasks:
            return

        if message.channel.id in self.sticky_tasks:
            self.sticky_tasks[message.channel.id].cancel()

        if (current_time - last_time) < round(panel.get('conversation_duration', 10) / 2):
            if conv_mode == 'One Shot' and message.channel.id in self.sticky_tasks:
                return

            delay = panel.get('conversation_duration', 10)
            self.sticky_tasks[message.channel.id] = asyncio.create_task(
                self.sticky_worker(message.channel, panel, delay)
            )
        else:
            self.sticky_tasks[message.channel.id] = asyncio.create_task(
                self.sticky_worker(message.channel, panel, 0)
            )

    async def update_sticky_message(self, panel, channel):
        try:
            if panel.get('last_message_id'):
                try:
                    await (await channel.fetch_message(panel['last_message_id'])).delete()
                except:
                    pass
            if panel.get("response_type") == "text":
                new_msg = await channel.send(panel.get("response_text") or "")
            elif panel.get("response_type") == "embed" and panel.get("embed_data"):
                embed_obj = discord.Embed.from_dict(panel.get("embed_data"))
                new_msg = await channel.send(content=panel.get("embed_content"), embed=embed_obj)
            else:
                new_msg = await channel.send(embed=self.build_panel_embed(panel))
            await self.bot.db.execute("UPDATE sticky_panels SET last_message_id = ? WHERE guild_id = ? AND title = ?",
                                     (new_msg.id, panel['guild_id'], panel['title']))
            panel['last_message_id'] = new_msg.id
        except Exception as e:
            if is_access_error(e):
                await report_access_failure(
                    self.bot, panel['guild_id'], "sticky_messages", panel.get('title', '')
                )

    @tasks.loop(seconds=120)
    async def sticky_monitor(self):
        for c_id, panel in list(self.active_channels.items()):
            if c_id in self.sticky_tasks:
                continue
            try:
                channel = self.bot.get_channel(c_id) or await self.bot.fetch_channel(c_id)
            except Exception as e:
                if is_access_error(e):
                    await report_access_failure(
                        self.bot, panel['guild_id'], "sticky_messages", panel.get('title', '')
                    )
                continue
            if channel and channel.last_message_id != panel.get('last_message_id'):
                await self.update_sticky_message(panel, channel)

    sticky_group = beacon_commands.Group(name="sticky", description="Sticky message commands", permissions_preset="automation")

    def data_features(self) -> list[DataFeatureMeta]:
        return [DataFeatureMeta(
            feature_id="sticky_messages",
            name="Sticky Messages",
            guild_export=True,
            guild_delete=True,
        )]

    async def data_export_user(self, user_id: int, *, guild_ids: list[int] | None) -> DataExportChunk:
        return DataExportChunk(feature_id="sticky_messages")

    async def data_export_guild(self, guild_id: int) -> DataExportChunk:
        chunk = DataExportChunk(feature_id="sticky_messages")
        async with self.bot.db.acquire_db() as db:
            rows = await export_table(db, "SELECT * FROM sticky_panels WHERE guild_id = ?", (guild_id,))
        chunk.guild_data[guild_id] = {"sticky_panels": rows}
        return chunk

    async def data_delete_user(self, user_id: int, *, guild_ids: list[int] | None, feature_id: str | None) -> DataDeleteResult:
        return DataDeleteResult(feature_id="sticky_messages")

    async def data_delete_guild(self, guild_id: int, feature_id: str | None) -> DataDeleteResult:
        if feature_id and feature_id != "sticky_messages":
            return DataDeleteResult(feature_id="sticky_messages")
        panels = list(self.panel_cache.get(guild_id, {}).keys())
        rows_affected = await self.bot.db.execute("DELETE FROM sticky_panels WHERE guild_id = ?", (guild_id,))
        for title in panels:
            panel = self.panel_cache.get(guild_id, {}).pop(title, None)
            if panel:
                self.active_channels.pop(panel.get("channel_id"), None)
        return DataDeleteResult(feature_id="sticky_messages", deleted=True, rows_affected=rows_affected)

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
        result = DataMonitorResult(feature_id="sticky_messages")
        for title in list(self.panel_cache.get(guild.id, {}).keys()):
            panel = self.panel_cache[guild.id][title]
            channel_id = panel.get("channel_id")
            if not channel_id or await self._channel_sendable(guild, channel_id):
                continue
            await self.delete_panel(guild.id, title)
            result.actions.append(f"removed_panel:{title}")
        return result

    @sticky_group.command(name="message", description="Open the Sticky Message Dashboard")
    async def sticky_dashboard(self, interaction: discord.Interaction):
        await interaction.response.send_message(view=StickyDashboard(interaction.user, self, interaction.guild.id))


async def setup(bot):
    await bot.add_cog(StickyMessages(bot))