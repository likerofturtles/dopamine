import asyncio
import re
import time
from typing import Optional, List, Dict, Tuple, Set, Any

import discord
import emoji
from beacon import PrivateLayoutView, beacon_commands
from discord.ext import commands

from utils.data_protocol import DataDeleteResult, DataExportChunk, DataFeatureMeta, DataMonitorResult
from utils.discord_health import is_access_error, report_access_failure

EMOJI_REGEX = re.compile(
    r'(<a?:\w{2,32}:\d{15,25}>)'
    r'|([\U0001F1E6-\U0001F1FF]{2})'
    r'|([\U0001F300-\U0001FAFF]\uFE0F?)'
    r'|([\u2600-\u27BF]\uFE0F?)',
    flags=re.UNICODE
)


class CreatePanelModal(discord.ui.Modal):
    def __init__(self, cog, guild_id, channel_id):
        super().__init__(title="Configure Panel Details")
        self.cog = cog
        self.guild_id = guild_id
        self.channel_id = channel_id

        self.name_input = discord.ui.TextInput(
            label="Panel Name",
            placeholder="e.g. Art Channel Reactions",
            min_length=1, max_length=50, required=True
        )
        self.emoji_input = discord.ui.TextInput(
            label="Emojis (Max 3)",
            placeholder="🔥, :thumbup:",
            min_length=1, required=True
        )
        self.add_item(self.name_input)
        self.add_item(self.emoji_input)

    async def on_submit(self, interaction: discord.Interaction):
        name = self.name_input.value
        emoji_raw = self.emoji_input.value

        parsed = self.cog.parse_emoji_input(emoji_raw)
        if not (0 < len(parsed) <= 3):
            await interaction.response.send_message("Provide 1-3 valid emojis.", ephemeral=True)
            return

        guild_panels = [p for (g, pid), p in self.cog.panel_cache.items() if g == self.guild_id]
        existing_ids = {p['panel_id'] for p in guild_panels}
        panel_id = 1
        while panel_id in existing_ids:
            panel_id += 1

        now = time.time()
        serialized = self.cog.serialize_emojis(parsed)

        await self.cog.bot.db.execute(
            '''
            INSERT INTO autoreact_panels (guild_id, panel_id, name, emoji, channel_id, is_active, started_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            ''',
            (self.guild_id, panel_id, name, serialized, self.channel_id, now)
        )

        self.cog.panel_cache[(self.guild_id, panel_id)] = {
            "guild_id": self.guild_id, "panel_id": panel_id, "name": name,
            "emoji": serialized, "emoji_list": parsed, "channel_id": self.channel_id,
            "is_active": 1, "member_whitelist": 0, "image_only_mode": 0, "started_at": now
        }
        await interaction.response.send_message("Autoreact Panel created and started successfully!")


class EditPanelDetailsModal(discord.ui.Modal):
    def __init__(self, cog, panel: Dict, parent_view):
        super().__init__(title="Edit Panel Details")
        self.cog = cog
        self.panel = panel
        self.parent_view = parent_view

        self.name_input = discord.ui.TextInput(
            label="Panel Name",
            default=panel['name'],
            min_length=1, max_length=50, required=True
        )
        self.emoji_input = discord.ui.TextInput(
            label="Emojis",
            default=self.cog.format_emojis_for_display(panel['emoji_list']),
            required=True
        )
        self.add_item(self.name_input)
        self.add_item(self.emoji_input)

    async def on_submit(self, interaction: discord.Interaction):
        name = self.name_input.value
        parsed = self.cog.parse_emoji_input(self.emoji_input.value)

        if not (0 < len(parsed) <= 3):
            await interaction.response.send_message("Provide 1-3 valid emojis.", ephemeral=True)
            return

        serialized = self.cog.serialize_emojis(parsed)

        await self.cog.bot.db.execute(
            "UPDATE autoreact_panels SET name = ?, emoji = ? WHERE guild_id = ? AND panel_id = ?",
            (name, serialized, self.panel['guild_id'], self.panel['panel_id'])
        )

        self.panel['name'] = name
        self.panel['emoji'] = serialized
        self.panel['emoji_list'] = parsed

        self.parent_view.build_layout()
        await interaction.response.edit_message(view=self.parent_view)


class GoToPageModal(discord.ui.Modal):
    def __init__(self, parent_view, total_pages: int):
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


class AutoreactDashboard(PrivateLayoutView):
    def __init__(self, user, cog):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        container.add_item((discord.ui.TextDisplay("## Autoreact Dashboard")))
        container.add_item(discord.ui.TextDisplay(
            "Set up Dopamine to automatically react to messages in a channel with one or more emojis."))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(
            "* **Emojis:** You can add up to three emojis per Autoreact panel. Order the emojis in the same order in which you want them to be added in, separated by commas."))
        container.add_item(discord.ui.TextDisplay(
            "* **Image-only Mode:** Dopamine will only react to messages which contain an image."))
        container.add_item(discord.ui.TextDisplay(
            "* **Member Whitelist Mode:** Dopamine will only react to messages from specific members. Add upto 25 members."))
        container.add_item(discord.ui.Separator())

        create_btn = discord.ui.Button(label="Create", style=discord.ButtonStyle.primary)
        create_btn.callback = self.create_callback

        manage_btn = discord.ui.Button(label="Manage & Edit", style=discord.ButtonStyle.secondary)
        manage_btn.callback = self.manage_callback

        row = discord.ui.ActionRow()
        row.add_item(create_btn)
        row.add_item(manage_btn)

        container.add_item(row)
        self.add_item(container)

    async def create_callback(self, interaction: discord.Interaction):
        if not await self.cog.bot.get_cog('TopGGVoter').check_vote_access(interaction.user.id):
            await interaction.response.send_message("Vote required to use this feature.", ephemeral=True)
            return
        if interaction.guild is not None:
            view = CreateChannelSelect(self.user, self.cog, interaction.guild.id)
            await interaction.response.send_message(view=view, ephemeral=True)

    async def manage_callback(self, interaction: discord.Interaction):
        if interaction.guild is not None:
            view = ManagePage(self.user, self.cog, interaction.guild.id)
            await interaction.response.edit_message(content=None, embed=None, view=view)


class ManagePage(PrivateLayoutView):
    def __init__(self, user, cog, guild_id):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.page = 1
        self.items_per_page = 5
        self.build_layout()

    def build_layout(self):
        self.clear_items()

        all_panels = [p for (g, pid), p in self.cog.panel_cache.items() if g == self.guild_id]
        all_panels.sort(key=lambda x: x['panel_id'])

        total_items = len(all_panels)
        total_pages = (total_items + self.items_per_page - 1) // self.items_per_page or 1

        if self.page > total_pages:
            self.page = total_pages
        if self.page < 1:
            self.page = 1

        start = (self.page - 1) * self.items_per_page
        end = start + self.items_per_page
        current_panels = all_panels[start:end]

        container = discord.ui.Container()
        container.add_item((discord.ui.TextDisplay("## Manage Autoreact Panels")))
        container.add_item(discord.ui.TextDisplay(
            "List of all existing Autoreact Panels. Click Edit to configure details or the channel."))
        container.add_item(discord.ui.Separator())

        if not current_panels:
            container.add_item(discord.ui.TextDisplay("*No Autoreact Panels found.*"))
        else:
            for panel in current_panels:
                edit_btn = discord.ui.Button(label="Edit", style=discord.ButtonStyle.secondary,
                                             custom_id=f"edit_{panel['panel_id']}")
                edit_btn.callback = self.make_edit_callback(panel)

                channel_mention = f"<#{panel['channel_id']}>"
                display_name = f"**{panel['name']}** in {channel_mention}"

                container.add_item(discord.ui.Section(discord.ui.TextDisplay(display_name), accessory=edit_btn))

            container.add_item(discord.ui.Separator())

            left_btn = discord.ui.Button(emoji="◀", style=discord.ButtonStyle.primary, disabled=(self.page == 1))
            left_btn.callback = self.prev_page

            go_btn = discord.ui.Button(label=f"Page {self.page} of {total_pages}", style=discord.ButtonStyle.secondary, disabled=(total_pages <= 1))
            go_btn.callback = lambda interaction: interaction.response.send_modal(GoToPageModal(self, total_pages))

            right_btn = discord.ui.Button(emoji="▶", style=discord.ButtonStyle.primary, disabled=(self.page == total_pages))
            right_btn.callback = self.next_page

            row = discord.ui.ActionRow()
            row.add_item(left_btn)
            row.add_item(go_btn)
            row.add_item(right_btn)
            container.add_item(row)

        container.add_item(discord.ui.Separator())

        return_btn = discord.ui.Button(emoji=self.cog.bot.back_emoji, label="Back", style=discord.ButtonStyle.secondary)
        return_btn.callback = self.return_home

        row_ret = discord.ui.ActionRow()
        row_ret.add_item(return_btn)
        container.add_item(row_ret)

        self.add_item(container)

    def make_edit_callback(self, panel):
        async def callback(interaction: discord.Interaction):
            view = EditPage(self.user, self.cog, panel)
            await interaction.response.edit_message(content=None, embed=None, view=view)

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
        view = AutoreactDashboard(self.user, self.cog)
        await interaction.response.edit_message(content=None, embed=None, view=view)


class EditPage(PrivateLayoutView):
    def __init__(self, user, cog, panel: Dict):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.panel = panel
        self.build_layout()

    def build_layout(self):
        self.clear_items()

        p_data = self.cog.panel_cache.get((self.panel['guild_id'], self.panel['panel_id']))
        if not p_data:
            self.add_item(discord.ui.TextDisplay("This panel no longer exists."))
            return

        self.panel = p_data

        is_active = bool(self.panel['is_active'])
        is_whitelist = bool(self.panel['member_whitelist'])
        is_image_only = bool(self.panel['image_only_mode'])

        whitelist_count = len(self.cog.whitelist_cache.get((self.panel['guild_id'], self.panel['panel_id']),
                                                           [])) if is_whitelist else "All"

        container = discord.ui.Container()
        container.add_item((discord.ui.TextDisplay(f"Edit: {self.panel['name']}")))
        container.add_item(discord.ui.Separator())

        details = (
            f"**State:** {'🟢 Active' if is_active else '🔴 Inactive'}\n"
            f"**Emojis:** {self.cog.format_emojis_for_display(self.panel['emoji_list'])}\n"
            f"**Channel:** <#{self.panel['channel_id']}>\n"
            f"**Target:** {whitelist_count} Members\n"
            f"**Mode:** {'Image-only' if is_image_only else 'All Messages'}\n"
        )
        container.add_item(discord.ui.TextDisplay(details))
        container.add_item(discord.ui.Separator())

        state_btn = discord.ui.Button(label=f"{'Deactivate' if is_active else 'Activate'}",
                                      style=discord.ButtonStyle.secondary if is_active else discord.ButtonStyle.primary)
        state_btn.callback = self.toggle_state

        edit_btn = discord.ui.Button(label="Edit", style=discord.ButtonStyle.secondary)
        edit_btn.callback = self.open_edit_modal

        channel_btn = discord.ui.Button(label="Edit Channel", style=discord.ButtonStyle.secondary)
        channel_btn.callback = self.open_channel_select

        delete_btn = discord.ui.Button(label="Delete", style=discord.ButtonStyle.danger)
        delete_btn.callback = self.delete_panel

        member_btn = discord.ui.Button(
            label=f"{'Disable Member Whitelist' if is_whitelist else 'Enable Member Whitelist'}",
            style=discord.ButtonStyle.secondary if is_whitelist else discord.ButtonStyle.primary)
        member_btn.callback = self.toggle_whitelist

        image_btn = discord.ui.Button(
            label=f"{'Disable Image-only Mode' if is_image_only else 'Enable Image-only Mode'}",
            style=discord.ButtonStyle.secondary if is_image_only else discord.ButtonStyle.primary)
        image_btn.callback = self.toggle_image_only

        row1 = discord.ui.ActionRow()
        row1.add_item(state_btn)
        row1.add_item(edit_btn)
        row1.add_item(channel_btn)
        row1.add_item(delete_btn)
        container.add_item(row1)

        row2 = discord.ui.ActionRow()
        row2.add_item(member_btn)
        row2.add_item(image_btn)
        container.add_item(row2)

        container.add_item(discord.ui.Separator())

        return_btn = discord.ui.Button(emoji=self.cog.bot.back_emoji, label="Back", style=discord.ButtonStyle.secondary)
        return_btn.callback = self.return_manage

        row3 = discord.ui.ActionRow()
        row3.add_item(return_btn)
        container.add_item(row3)

        self.add_item(container)

    async def toggle_state(self, interaction: discord.Interaction):
        new_state = 0 if self.panel['is_active'] else 1
        await self.cog.bot.db.execute(
            "UPDATE autoreact_panels SET is_active = ?, started_at = ? WHERE guild_id = ? AND panel_id = ?",
            (new_state, time.time(), self.panel['guild_id'], self.panel['panel_id'])
        )
        self.panel['is_active'] = new_state
        self.build_layout()
        await interaction.response.edit_message(view=self)

    async def open_edit_modal(self, interaction: discord.Interaction):
        await interaction.response.send_modal(EditPanelDetailsModal(self.cog, self.panel, self))

    async def open_channel_select(self, interaction: discord.Interaction):
        view = EditChannelSelect(self.user, self.cog, self.panel['guild_id'], is_rebind=True, panel_data=self.panel)
        await interaction.response.edit_message(content=None, embed=None, view=view)

    async def delete_panel(self, interaction: discord.Interaction):
        view = DestructiveConfirmationView(self.user, self.panel['name'], self.cog, self.panel['guild_id'],
                                           self.panel['panel_id'])
        await interaction.response.send_message(view=view)

    async def toggle_whitelist(self, interaction: discord.Interaction):
        if self.panel['member_whitelist']:
            await self.cog.bot.db.execute(
                "UPDATE autoreact_panels SET member_whitelist = 0 WHERE guild_id = ? AND panel_id = ?",
                (self.panel['guild_id'], self.panel['panel_id'])
            )
            await self.cog.bot.db.execute(
                "DELETE FROM autoreact_whitelist WHERE guild_id = ? AND panel_id = ?",
                (self.panel['guild_id'], self.panel['panel_id'])
            )

            self.panel['member_whitelist'] = 0
            if (self.panel['guild_id'], self.panel['panel_id']) in self.cog.whitelist_cache:
                del self.cog.whitelist_cache[(self.panel['guild_id'], self.panel['panel_id'])]

            self.build_layout()
            await interaction.response.edit_message(view=self)
        else:
            view = MemberSelect(self.user, self.cog, self.panel['guild_id'], is_rebind=True, panel_data=self.panel)
            await interaction.response.edit_message(content=None, embed=None, view=view)

    async def toggle_image_only(self, interaction: discord.Interaction):
        new_mode = 0 if self.panel['image_only_mode'] else 1
        await self.cog.bot.db.execute(
            "UPDATE autoreact_panels SET image_only_mode = ? WHERE guild_id = ? AND panel_id = ?",
            (new_mode, self.panel['guild_id'], self.panel['panel_id'])
        )
        self.panel['image_only_mode'] = new_mode
        self.build_layout()
        await interaction.response.edit_message(view=self)

    async def return_manage(self, interaction: discord.Interaction):
        view = ManagePage(self.user, self.cog, self.panel['guild_id'])
        await interaction.response.edit_message(content=None, embed=None, view=view)


class CreateChannelSelect(PrivateLayoutView):
    def __init__(self, user, cog, guild_id):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.guild_id = guild_id
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
        container.add_item(discord.ui.TextDisplay("Choose the channel where you want the reactions to be made:"))
        container.add_item(row)
        self.add_item(container)

    async def select_callback(self, interaction: discord.Interaction):
        channel_id = self.select.values[0].id
        await interaction.response.send_modal(CreatePanelModal(self.cog, self.guild_id, channel_id))


class EditChannelSelect(PrivateLayoutView):
    def __init__(self, user, cog, guild_id, is_rebind=False, panel_data=None):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.is_rebind = is_rebind
        self.panel_data = panel_data if panel_data is not None else {}
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
        container.add_item(discord.ui.TextDisplay("Select a Channel"))
        container.add_item(discord.ui.TextDisplay("Choose the new channel for this panel:"))
        container.add_item(row)
        self.add_item(container)

    async def select_callback(self, interaction: discord.Interaction):
        new_channel_id = self.select.values[0].id
        await self.cog.bot.db.execute(
            "UPDATE autoreact_panels SET channel_id = ? WHERE guild_id = ? AND panel_id = ?",
            (new_channel_id, self.guild_id, self.panel_data['panel_id'])
        )

        self.panel_data['channel_id'] = new_channel_id

        view = EditPage(self.user, self.cog, self.panel_data)
        await interaction.response.edit_message(content=None, embed=None, view=view)


class MemberSelect(PrivateLayoutView):
    def __init__(self, user, cog, guild_id, is_rebind=False, panel_data=None):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.is_rebind = is_rebind
        self.panel_data = panel_data if panel_data is not None else {}
        self.build_layout()

    def build_layout(self):
        container = discord.ui.Container()

        self.select = discord.ui.UserSelect(
            placeholder="Select members...",
            min_values=1, max_values=25
        )
        self.select.callback = self.select_callback

        row = discord.ui.ActionRow()
        row.add_item(self.select)
        container.add_item(discord.ui.TextDisplay("Select Members"))
        container.add_item(discord.ui.TextDisplay("Choose only the member(s) whose messages should get the reaction:"))
        container.add_item(row)
        self.add_item(container)

    async def select_callback(self, interaction: discord.Interaction):
        members = self.select.values
        panel_id = self.panel_data['panel_id']
        key = (self.guild_id, panel_id)

        await self.cog.bot.db.execute(
            "UPDATE autoreact_panels SET member_whitelist = 1 WHERE guild_id = ? AND panel_id = ?",
            key
        )
        self.panel_data['member_whitelist'] = 1

        if key not in self.cog.whitelist_cache:
            self.cog.whitelist_cache[key] = set()

        for member in members:
            if member.bot:
                continue
            await self.cog.bot.db.execute(
                "INSERT OR REPLACE INTO autoreact_whitelist (guild_id, panel_id, user_id) VALUES (?, ?, ?)",
                (self.guild_id, panel_id, member.id)
            )
            self.cog.whitelist_cache[key].add(member.id)

        view = EditPage(self.user, self.cog, self.panel_data)
        await interaction.response.edit_message(content=None, embed=None, view=view)


class DestructiveConfirmationView(PrivateLayoutView):
    def __init__(self, user, title_name, cog, guild_id, panel_id):
        super().__init__(user, timeout=30)
        self.title_name = title_name
        self.cog = cog
        self.color = None
        self.guild_id = guild_id
        self.panel_id = panel_id
        self.value = None
        self.title_text = "Delete Autoreact Panel"
        self.body_text = f"Are you sure you want to permanently delete the panel **{title_name}**? This cannot be undone."
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
        await self.cog.delete_panel(self.guild_id, self.panel_id)


class AutoReact(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

        self.panel_cache: Dict[Tuple[int, int], Dict[str, Any]] = {}
        self.whitelist_cache: Dict[Tuple[int, int], Set[int]] = {}

        self._reaction_queue: asyncio.Queue[Tuple[discord.Message, str]] = asyncio.Queue()
        self._reaction_semaphore = asyncio.Semaphore(5)
        self._reaction_task: Optional[asyncio.Task] = None

    async def cog_load(self):
        await self.bot.db.wait_ready()
        await self.populate_caches()
        if self._reaction_task is None or self._reaction_task.done():
            self._reaction_task = asyncio.create_task(self.reaction_processor())

    async def cog_unload(self):
        if self._reaction_task is not None:
            self._reaction_task.cancel()
            try:
                await self._reaction_task
            except asyncio.CancelledError:
                pass

    async def populate_caches(self):
        self.panel_cache.clear()
        self.whitelist_cache.clear()

        rows = await self.bot.db.execute("SELECT * FROM autoreact_panels")
        for data in rows:
            key = (data['guild_id'], data['panel_id'])
            data['emoji_list'] = self.deserialize_emojis(data['emoji'])
            self.panel_cache[key] = data

        rows = await self.bot.db.execute("SELECT guild_id, panel_id, user_id FROM autoreact_whitelist")
        for row in rows:
            g_id = row['guild_id']
            p_id = row['panel_id']
            u_id = row['user_id']
            key = (g_id, p_id)
            if key not in self.whitelist_cache:
                self.whitelist_cache[key] = set()
            self.whitelist_cache[key].add(u_id)

    def parse_emoji_input(self, emoji_input: str) -> List[str]:
        if not emoji_input:
            return []
        emoji_input = emoji.emojize(emoji_input, language='alias')
        tokens = []
        parts = [p.strip() for p in re.split(r'[,\s]+', emoji_input) if p.strip()]
        for part in parts:
            matches = [m.group(0) for m in EMOJI_REGEX.finditer(part)]
            if matches:
                tokens.extend(matches)
            else:
                tokens.append(part)
        return tokens

    def serialize_emojis(self, emojis: List[str]) -> str:
        return '|'.join(emojis)

    def deserialize_emojis(self, value: str) -> List[str]:
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
        return ', '.join(emojis) if emojis else 'None'

    async def delete_panel(self, guild_id: int, panel_id: int):
        key = (guild_id, panel_id)
        await self.bot.db.execute("DELETE FROM autoreact_whitelist WHERE guild_id = ? AND panel_id = ?", key)
        await self.bot.db.execute("DELETE FROM autoreact_panels WHERE guild_id = ? AND panel_id = ?", key)

        self.panel_cache.pop(key, None)
        self.whitelist_cache.pop(key, None)

    @beacon_commands.command(name="autoreact", description="Manage AutoReact panels via dashboard", permissions_preset="automations")
    async def autoreact_dashboard_cmd(self, interaction: discord.Interaction):
        view = AutoreactDashboard(interaction.user, self)
        await interaction.response.send_message(view=view)

    def data_features(self) -> list[DataFeatureMeta]:
        return [DataFeatureMeta(
            feature_id="autoreact",
            name="AutoReact",
            guild_export=True,
            guild_delete=True,
        )]

    async def data_export_user(self, user_id: int, *, guild_ids: list[int] | None) -> DataExportChunk:
        return DataExportChunk(feature_id="autoreact")

    async def data_export_guild(self, guild_id: int) -> DataExportChunk:
        chunk = DataExportChunk(feature_id="autoreact")
        panels = await self.bot.db.execute(
            "SELECT * FROM autoreact_panels WHERE guild_id = ?", (guild_id,))
        whitelist = await self.bot.db.execute(
            "SELECT * FROM autoreact_whitelist WHERE guild_id = ?", (guild_id,))
        chunk.guild_data[guild_id] = {"panels": panels, "whitelist": whitelist}
        return chunk

    async def data_delete_user(self, user_id: int, *, guild_ids: list[int] | None, feature_id: str | None) -> DataDeleteResult:
        return DataDeleteResult(feature_id="autoreact")

    async def data_delete_guild(self, guild_id: int, feature_id: str | None) -> DataDeleteResult:
        if feature_id and feature_id != "autoreact":
            return DataDeleteResult(feature_id="autoreact")
        count_rows = await self.bot.db.execute(
            "SELECT COUNT(*) AS cnt FROM autoreact_whitelist WHERE guild_id = ?", (guild_id,))
        rows_affected = count_rows[0]["cnt"] if count_rows else 0
        count_rows = await self.bot.db.execute(
            "SELECT COUNT(*) AS cnt FROM autoreact_panels WHERE guild_id = ?", (guild_id,))
        rows_affected += count_rows[0]["cnt"] if count_rows else 0
        await self.bot.db.execute("DELETE FROM autoreact_whitelist WHERE guild_id = ?", (guild_id,))
        await self.bot.db.execute("DELETE FROM autoreact_panels WHERE guild_id = ?", (guild_id,))
        keys = [k for k in self.panel_cache if k[0] == guild_id]
        for key in keys:
            self.panel_cache.pop(key, None)
            self.whitelist_cache.pop(key, None)
        return DataDeleteResult(feature_id="autoreact", deleted=True, rows_affected=rows_affected)

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
        return perms.view_channel and perms.add_reactions

    async def data_monitor_guild(self, guild: discord.Guild) -> DataMonitorResult:
        result = DataMonitorResult(feature_id="autoreact")
        for (g_id, panel_id), panel in list(self.panel_cache.items()):
            if g_id != guild.id or not panel.get("is_active"):
                continue
            if await self._channel_sendable(guild, panel["channel_id"]):
                continue
            await self.bot.db.execute(
                "UPDATE autoreact_panels SET is_active = 0 WHERE guild_id = ? AND panel_id = ?",
                (g_id, panel_id),
            )
            panel["is_active"] = 0
            result.actions.append(f"deactivated_panel:{panel_id}")
        return result

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        for (g_id, p_id), panel in self.panel_cache.items():
            if g_id != message.guild.id or panel['channel_id'] != message.channel.id or not panel['is_active']:
                continue

            if panel['image_only_mode']:
                has_img = bool(message.attachments) or any(e.type == 'image' for e in message.embeds)
                if not has_img:
                    continue

            if panel['member_whitelist']:
                allowed = self.whitelist_cache.get((g_id, p_id), set())
                if message.author.id not in allowed:
                    continue
            for em in panel['emoji_list']:
                await self._reaction_queue.put((message, em))

    async def reaction_processor(self):
        while True:
            try:
                await self.bot.db.wait_ready()
                message, em = await self._reaction_queue.get()
                async with self._reaction_semaphore:
                    try:
                        cleaned_emoji = emoji.emojize(em, language='alias')
                        await message.add_reaction(cleaned_emoji)
                    except Exception as e:
                        if is_access_error(e) and message.guild:
                            await report_access_failure(
                                self.bot, message.guild.id, "autoreact", str(message.channel.id)
                            )
                self._reaction_queue.task_done()
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                break
            except:
                continue


async def setup(bot):
    await bot.add_cog(AutoReact(bot))
