import asyncio
import json
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Dict, List, Optional, Any, Set

import aiosqlite
import discord
from beacon import PrivateLayoutView, beacon_commands
from discord.ext import commands
from rapidfuzz import fuzz

from cogs.embed import UseEmbedPage
from config import ARSPDB_PATH
from utils.data_handlers import export_table
from utils.data_protocol import DataDeleteResult, DataExportChunk, DataFeatureMeta, DataMonitorResult
from utils.discord_health import is_access_error, report_access_failure


@dataclass
class AutoresponseRecord:
    id: int
    guild_id: int
    trigger: str
    response_type: str
    response_text: Optional[str]
    embed_content: Optional[str]
    embed_data: Optional[Dict[str, Any]]
    channels: Optional[Set[int]]
    match_mode: str
    fuzzy_threshold: int
    case_sensitive: bool
    created_by: int
    created_at: int


def _serialize_channels(channels: Optional[Set[int]]) -> str:
    if not channels:
        return ""
    return ",".join(str(c) for c in sorted(channels))


def _deserialize_channels(raw: Optional[str]) -> Optional[Set[int]]:
    if not raw:
        return None
    try:
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        return {int(p) for p in parts}
    except ValueError:
        return None


def _serialize_embed_data(data: Optional[Dict[str, Any]]) -> Optional[str]:
    if data is None:
        return None
    return json.dumps(data, ensure_ascii=False)


def _deserialize_embed_data(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def apply_variables(text: str, message: discord.Message) -> str:
    if not text:
        return text

    author = message.author
    channel = message.channel
    guild = message.guild

    replacements = {
        "{author.mention}": getattr(author, "mention", ""),
        "{author.name}": getattr(author, "name", ""),
        "{author.display_name}": getattr(author, "display_name", ""),
        "{author.id}": str(getattr(author, "id", "")),
        "{channel.mention}": getattr(channel, "mention", ""),
        "{channel.name}": getattr(channel, "name", ""),
        "{channel.id}": str(getattr(channel, "id", "")),
        "{guild.name}": getattr(guild, "name", "") if guild else "",
        "{guild.member_count}": str(getattr(guild, "member_count", "")) if guild else "",
        "{guild.id}": str(getattr(guild, "id", "")) if guild else "",
    }

    for key, value in replacements.items():
        text = text.replace(key, value)
    return text

class GoToPageModal(discord.ui.Modal):
    def __init__(self, current_page: int, total_pages: int, parent_view: "ManageAutoresponsePage"):
        super().__init__(title="Go to Page")
        self.total_pages = total_pages
        self.parent_view = parent_view

        self.page_input = discord.ui.TextInput(
            label=f"Enter Page Number (1-{total_pages})",
            default=str(current_page),
            required=True,
            max_length=5,
        )
        self.add_item(self.page_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            target_page = int(self.page_input.value)
            if not 1 <= target_page <= self.total_pages:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                f"Please enter a valid whole number between 1 and {self.total_pages}.",
                ephemeral=True
            )
            return

        self.parent_view.page = target_page
        self.parent_view.autoresponses = self.parent_view.cog.get_guild_autoresponses(self.parent_view.guild_id)
        self.parent_view.build_layout()
        await interaction.response.edit_message(view=self.parent_view)

class DestructiveConfirmationView(PrivateLayoutView):
    def __init__(self, user, record_id, cog, guild_id):
        super().__init__(user, timeout=30)
        self.record_id = record_id
        self.cog = cog
        self.color = None
        self.guild_id = guild_id
        self.value = None
        self.title_text = "Delete Autoresponse"
        self.body_text = f"Are you sure you want to permanently delete the Autoresponse? This cannot be undone."
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
        await self.cog.delete_autoresponse(self.guild_id, self.record_id)
        await self.update_view(interaction, "Action Confirmed", discord.Color.green())

    async def on_timeout(self, interaction: discord.Interaction):
        if self.value is None:
            self.value = False
            await self.update_view(interaction, "Timed Out", discord.Color(0xdf5046))

class AutoresponseDashboard(PrivateLayoutView):
    def __init__(self, user, cog: "Autoresponse", guild_id: int):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("## Autoresponse Dashboard"))
        container.add_item(discord.ui.TextDisplay("Reply to messages automatically that contain specific letters or words with a text message, link, or embed."))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay("### Match Modes:\n* **Exact Match:** Matches the string exactly.\n* **Partial Match:** Triggers if the string is anywhere in the message.\n* **Fuzzy Match:** Triggers response based on how much a specific string matches the message. It works on a percentage score. Default is 75%, but you can customise it for each Autoresponse!"))
        container.add_item(discord.ui.TextDisplay("### Variables:\n* **User Variables (User who triggered the response):** `{author.mention}`, `{author.name}`, `{author.display_name}`, and `{author.id}`.\n* **Channel Variables (Channel where response was triggered):** `{channel.mention}`, `{channel.name}`, and `{channel.id}`.\n* **Guild Variables (Guild is the Discord internal word for server):** `{guild.name}`, `{guild.member_count}`, and `{guild.id}`."))
        create_btn = discord.ui.Button(label="Create", style=discord.ButtonStyle.primary)
        manage_btn = discord.ui.Button(label="Manage & Edit", style=discord.ButtonStyle.secondary)

        create_btn.callback = self.create_callback
        manage_btn.callback = self.manage_callback

        row = discord.ui.ActionRow()
        row.add_item(create_btn)
        row.add_item(manage_btn)
        container.add_item(discord.ui.Separator())
        container.add_item(row)

        self.add_item(container)

    async def create_callback(self, interaction: discord.Interaction):
        draft = {
            "guild_id": self.guild_id,
            "trigger": "",
            "response_type": None,
            "response_text": None,
            "embed_content": None,
            "embed_data": None,
            "channels": None,
            "match_mode": "exact",
            "fuzzy_threshold": 75,
            "case_sensitive": False,
        }
        view = CreateAutoresponseView(interaction.user, self.cog, self.guild_id, draft)
        await interaction.response.send_message(view=view, ephemeral=True)

    async def manage_callback(self, interaction: discord.Interaction):
        autoresponses = self.cog.get_guild_autoresponses(self.guild_id)
        view = ManageAutoresponsePage(interaction.user, self.cog, self.guild_id, autoresponses, page=1)
        await interaction.response.edit_message(view=view)


class ManageAutoresponsePage(PrivateLayoutView):
    def __init__(self, user, cog: "Autoresponse", guild_id: int, autoresponses: List[AutoresponseRecord], page: int = 1):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.autoresponses = autoresponses
        self.page = page
        self.items_per_page = 5
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("## Manage Autoresponse"))
        container.add_item(discord.ui.Separator())

        total_items = len(self.autoresponses)
        total_pages = (total_items + self.items_per_page - 1) // self.items_per_page if total_items > 0 else 1

        start_idx = (self.page - 1) * self.items_per_page
        end_idx = start_idx + self.items_per_page
        current_items = self.autoresponses[start_idx:end_idx]

        if not current_items:
            container.add_item(discord.ui.TextDisplay("*No Autoresponses found.*"))
        else:
            for idx, ar in enumerate(current_items, start=start_idx + 1):
                edit_btn = discord.ui.Button(label=f"Edit", style=discord.ButtonStyle.secondary)

                async def make_callback(interaction: discord.Interaction, record: AutoresponseRecord = ar):
                    view = EditAutoresponsePage(self.user, self.cog, self.guild_id, record)
                    await interaction.response.edit_message(view=view)

                edit_btn.callback = make_callback
                display_text = f"### {idx}. `{ar.trigger}`"
                container.add_item(discord.ui.Section(discord.ui.TextDisplay(display_text), accessory=edit_btn))

            container.add_item(discord.ui.Separator())

            nav_row = discord.ui.ActionRow()
            left_btn = discord.ui.Button(emoji="◀️", style=discord.ButtonStyle.primary, disabled=(self.page <= 1))
            goto_btn = discord.ui.Button(label=f"Page {self.page} of {total_pages}", style=discord.ButtonStyle.secondary)
            right_btn = discord.ui.Button(emoji="▶️", style=discord.ButtonStyle.primary, disabled=(self.page >= total_pages))

            async def prev_page(interaction: discord.Interaction):
                self.page -= 1
                self.autoresponses = self.cog.get_guild_autoresponses(self.guild_id)
                self.build_layout()
                await interaction.response.edit_message(view=self)

            async def next_page(interaction: discord.Interaction):
                self.page += 1
                self.autoresponses = self.cog.get_guild_autoresponses(self.guild_id)
                self.build_layout()
                await interaction.response.edit_message(view=self)

            async def goto_page_callback(interaction: discord.Interaction):
                modal = GoToPageModal(current_page=self.page, total_pages=total_pages, parent_view=self)
                await interaction.response.send_modal(modal)

            left_btn.callback = prev_page
            goto_btn.callback = goto_page_callback
            right_btn.callback = next_page
            nav_row.add_item(left_btn)
            nav_row.add_item(goto_btn)
            nav_row.add_item(right_btn)
            container.add_item(nav_row)

        container.add_item(discord.ui.Separator())
        return_btn = discord.ui.Button(emoji=self.cog.bot.back_emoji, label="Back", style=discord.ButtonStyle.secondary)

        async def return_home(interaction: discord.Interaction):
            view = AutoresponseDashboard(self.user, self.cog, self.guild_id)
            await interaction.response.edit_message(view=view)

        return_btn.callback = return_home
        row = discord.ui.ActionRow()
        row.add_item(return_btn)
        container.add_item(row)
        self.add_item(container)


class EditAutoresponsePage(PrivateLayoutView):
    def __init__(self, user, cog: "Autoresponse", guild_id: int, record: AutoresponseRecord):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.record = record
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        trigger_str = self.record.trigger
        container.add_item(discord.ui.TextDisplay(f"## Edit: `{trigger_str}`"))
        container.add_item(discord.ui.Separator())

        if self.record.response_type == "text":
            reply_display = f"```{self.record.response_text or ''}```"
        else:
            reply_display = "Custom Embed"

        if self.record.channels:
            channel_mentions = ", ".join(f"<#{cid}>" for cid in sorted(self.record.channels))
        else:
            channel_mentions = "All"

        if self.record.match_mode == "exact":
            mode_display = "Exact Match"
        elif self.record.match_mode == "partial":
            mode_display = "Partial Match"
        else:
            mode_display = "Fuzzy Match"

        lines = [
            f"### ➤ Trigger: `{trigger_str}`",
            f"* **Reply:** {reply_display}",
            f"* **Channel(s):** {channel_mentions}",
            f"* **Match Mode:** {mode_display}",
        ]

        if self.record.match_mode == "fuzzy":
            lines.append(f"**Fuzzy Match:** Default ({self.record.fuzzy_threshold}%)")

        lines.append(f"* **Case Sensitive:** {'Yes' if self.record.case_sensitive else 'No'}")

        container.add_item(discord.ui.TextDisplay("\n".join(lines)))

        container.add_item(discord.ui.Separator())

        edit_str_btn = discord.ui.Button(label="Edit Trigger",
                                         style=discord.ButtonStyle.primary)
        edit_cn_btn = discord.ui.Button(label="Edit Channel",
                                        style=discord.ButtonStyle.primary)
        mode_labels = {
            "exact": "Mode: Exact Match",
            "partial": "Mode: Partial Match",
            "fuzzy": "Mode: Fuzzy Match"
        }

        current_mode_label = mode_labels.get(self.record.match_mode, "Mode: Exact Match")

        mode_btn = discord.ui.Button(
            label=current_mode_label,
            style=discord.ButtonStyle.primary
        )
        edit_fuzzy_btn = discord.ui.Button(label="Edit Fuzzy Mode Score",
                                           style=discord.ButtonStyle.secondary)
        case_btn = discord.ui.Button(
            label=f"{'Disable' if self.record.case_sensitive else 'Enable'} Case Sensitivity",
            style=discord.ButtonStyle.secondary if self.record.case_sensitive else discord.ButtonStyle.primary
        )
        delete_btn = discord.ui.Button(label="Delete",
                                       style=discord.ButtonStyle.danger)
        row = discord.ui.ActionRow()
        row.add_item(edit_str_btn)
        row.add_item(edit_cn_btn)
        row.add_item(mode_btn)
        if self.record.match_mode == "fuzzy":
            row.add_item(edit_fuzzy_btn)
        row.add_item(case_btn)
        container.add_item(row)

        row2 = discord.ui.ActionRow()
        if self.record.response_type == "text":
            response_btn_label = "Edit Response"
        else:
            response_btn_label = "Edit Embed"

        response_btn = discord.ui.Button(
            label=response_btn_label,
            style=discord.ButtonStyle.primary,
        )

        row2.add_item(response_btn)
        row2.add_item(delete_btn)
        container.add_item(row2)

        container.add_item(discord.ui.Separator())

        return_btn = discord.ui.Button(emoji=self.cog.bot.back_emoji, label="Back", style=discord.ButtonStyle.secondary)
        row3 = discord.ui.ActionRow()
        row3.add_item(return_btn)
        container.add_item(row3)
        self.add_item(container)

        async def edit_trigger_callback(interaction: discord.Interaction):
            modal = TriggerEditModal(self.cog, self.guild_id, self.record, parent_view=self)
            await interaction.response.send_modal(modal)

        async def edit_channel_callback(interaction: discord.Interaction):
            view = ChannelSelect(self.user, self.cog, self.guild_id, draft=None, autoresponse=self.record, firsttime=0)
            await interaction.response.send_message(view=view, ephemeral=True)

        async def toggle_mode_callback(interaction: discord.Interaction):
            new_mode = {
                "exact": "partial",
                "partial": "fuzzy",
                "fuzzy": "exact",
            }.get(self.record.match_mode, "exact")
            await self.cog.update_autoresponse_mode(self.guild_id, self.record.id, new_mode)
            self.record.match_mode = new_mode
            self.build_layout()
            await interaction.response.edit_message(view=self)

        async def edit_fuzzy_callback(interaction: discord.Interaction):
            if self.record.match_mode != "fuzzy":
                await interaction.response.send_message("Fuzzy mode is not enabled for this Autoresponse.", ephemeral=True)
                return
            modal = FuzzyScoreModal(self.cog, self.guild_id, self.record, parent_view=self)
            await interaction.response.send_modal(modal)

        async def toggle_case_callback(interaction: discord.Interaction):
            new_value = not self.record.case_sensitive
            await self.cog.update_autoresponse_case(self.guild_id, self.record.id, new_value)
            self.record.case_sensitive = new_value
            self.build_layout()
            await interaction.response.edit_message(view=self)

        async def delete_callback(interaction: discord.Interaction):
            view = DestructiveConfirmationView(user=interaction.user, record_id=self.record.id, cog=self.cog, guild_id=self.guild_id)
            await interaction.response.send_message(view=view)

        async def return_dashboard_callback(interaction: discord.Interaction):
            autoresponses = self.cog.get_guild_autoresponses(self.guild_id)
            view = ManageAutoresponsePage(interaction.user, self.cog, self.guild_id, autoresponses, page=1)
            await interaction.response.edit_message(view=view)

        edit_str_btn.callback = edit_trigger_callback
        edit_cn_btn.callback = edit_channel_callback
        mode_btn.callback = toggle_mode_callback
        edit_fuzzy_btn.callback = edit_fuzzy_callback
        case_btn.callback = toggle_case_callback
        delete_btn.callback = delete_callback
        return_btn.callback = return_dashboard_callback

        async def response_action_callback(interaction: discord.Interaction):
            if self.record.response_type == "text":
                modal = EditTextResponseModal(self.cog, self.guild_id, self.record, parent_view=self)
                await interaction.response.send_modal(modal)
                return

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
                await self.cog.update_autoresponse_embed(
                    self.guild_id,
                    self.record.id,
                    content or None,
                    embed_obj.to_dict(),
                )
                self.record.response_type = "embed"
                self.record.embed_content = content or None
                self.record.embed_data = embed_obj.to_dict()
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

        response_btn.callback = response_action_callback


class TriggerEditModal(discord.ui.Modal):
    def __init__(self, cog: "Autoresponse", guild_id: int, record: AutoresponseRecord, parent_view: EditAutoresponsePage):
        super().__init__(title="Edit Trigger")
        self.cog = cog
        self.guild_id = guild_id
        self.record = record
        self.parent_view = parent_view

        self.trigger_input = discord.ui.TextInput(
            label="Trigger String",
            default=record.trigger,
            required=True,
            max_length=200,
        )
        self.add_item(self.trigger_input)

    async def on_submit(self, interaction: discord.Interaction):
        new_trigger = self.trigger_input.value.strip()

        if self.cog.is_trigger_duplicate(self.guild_id, new_trigger, exclude_id=self.record.id):
            await interaction.response.send_message(
                f"Another autoresponse already uses the trigger `{new_trigger}`!",
                ephemeral=True
            )
            return

        await self.cog.update_autoresponse_trigger(self.guild_id, self.record.id, new_trigger)
        self.record.trigger = new_trigger
        self.parent_view.build_layout()
        await interaction.response.edit_message(view=self.parent_view)


class EditTextResponseModal(discord.ui.Modal):
    def __init__(self, cog: "Autoresponse", guild_id: int, record: AutoresponseRecord, parent_view: EditAutoresponsePage):
        super().__init__(title="Edit Text Response")
        self.cog = cog
        self.guild_id = guild_id
        self.record = record
        self.parent_view = parent_view

        self.content_input = discord.ui.TextInput(
            label="Response Content",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=2000,
            default=record.response_text or "",
        )
        self.add_item(self.content_input)

    async def on_submit(self, interaction: discord.Interaction):
        new_text = self.content_input.value
        await self.cog.update_autoresponse_response_text(self.guild_id, self.record.id, new_text)
        self.record.response_type = "text"
        self.record.response_text = new_text
        self.parent_view.build_layout()
        await interaction.response.edit_message(view=self.parent_view)


class FuzzyScoreModal(discord.ui.Modal):
    def __init__(self, cog: "Autoresponse", guild_id: int, record: AutoresponseRecord, parent_view: EditAutoresponsePage):
        super().__init__(title="Edit Fuzzy Match Score")
        self.cog = cog
        self.guild_id = guild_id
        self.record = record
        self.parent_view = parent_view

        self.score_input = discord.ui.TextInput(
            label="Fuzzy Match Threshold (25-100)",
            default=str(record.fuzzy_threshold),
            required=True,
            max_length=3,
        )
        self.add_item(self.score_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            value = int(self.score_input.value)
            if not 25 <= value <= 100:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("Enter a whole number between 25 and 100.", ephemeral=True)
            return

        await self.cog.update_autoresponse_fuzzy(self.guild_id, self.record.id, value)
        self.record.fuzzy_threshold = value
        self.parent_view.build_layout()
        await interaction.response.edit_message(view=self.parent_view)


class ChannelSelect(PrivateLayoutView):
    def __init__(self, user, cog: "Autoresponse", guild_id: int, draft: Optional[Dict[str, Any]] = None,
                 autoresponse: Optional[AutoresponseRecord] = None, firsttime: int = 0):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.draft = draft
        self.autoresponse = autoresponse
        self.firsttime = firsttime
        self.build_layout()

    def build_layout(self):
        container = discord.ui.Container()
        self.select = discord.ui.ChannelSelect(placeholder="Select a channel...", min_values=0, max_values=25)
        self.select.callback = self.select_channel
        container.add_item(discord.ui.TextDisplay(f"{"### Step 3: Select the channel where you want the string to be detected:" if self.firsttime else "### Select the channel where you want the string to be detected:"}"))
        container.add_item(discord.ui.ActionRow(self.select))
        if self.firsttime:
            skip_button = discord.ui.Button(label="Skip (Detect in All Channels / Set it up later)",
                                            style=discord.ButtonStyle.primary)

            async def skip_callback(interaction: discord.Interaction):
                if self.draft is not None:
                    self.draft["channels"] = None
                    view = FinalStep(self.user, self.cog, self.guild_id, self.draft)
                    await interaction.response.edit_message(view=view)
                else:
                    await interaction.response.send_message(
                        "No draft found for this Autoresponse creation.", ephemeral=True
                    )

            skip_button.callback = skip_callback
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.ActionRow(skip_button))
        self.add_item(container)

    async def select_channel(self, interaction: discord.Interaction):
        selected_channels = {ch.id for ch in self.select.values}

        if self.autoresponse is not None:
            await self.cog.update_autoresponse_channels(self.guild_id, self.autoresponse.id, selected_channels or None)
            self.autoresponse.channels = selected_channels or None
            await interaction.response.send_message(
                content="Updated channels for this Autoresponse.", ephemeral=True
            )
            return

        if self.draft is None:
            await interaction.response.send_message(
                "No draft found for this Autoresponse creation.", ephemeral=True
            )
            return

        self.draft["channels"] = selected_channels or None
        if self.firsttime:
            view = FinalStep(self.user, self.cog, self.guild_id, self.draft)
            await interaction.response.edit_message(view=view)
        else:
            await interaction.response.send_message(
                content="Channels selected for this Autoresponse draft.", ephemeral=True
            )


class FinalStep(PrivateLayoutView):
    def __init__(self, user, cog: "Autoresponse", guild_id: int, draft: Dict[str, Any]):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.draft = draft
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()

        container.add_item(discord.ui.TextDisplay(f"### Step 4: Finalise the Autoresponse by configuring the settings below."))
        container.add_item(discord.ui.Separator())
        case_btn = discord.ui.Button(label=f"{'Disable' if self.draft.get('case_sensitive', False) else 'Enable'} Case Sensitivity",
                                     style=discord.ButtonStyle.secondary if self.draft.get('case_sensitive', False) else discord.ButtonStyle.primary)
        mode_labels = {
            "exact": "Mode: Exact Match",
            "partial": "Mode: Partial Match",
            "fuzzy": "Mode: Fuzzy Match"
        }

        current_mode = self.draft.get("match_mode", "exact")
        current_mode_label = mode_labels.get(current_mode, "Mode: Exact Match")

        mode_btn = discord.ui.Button(
            label=current_mode_label,
            style=discord.ButtonStyle.secondary
        )
        edit_fuzzy_btn = discord.ui.Button(label="Edit Fuzzy Mode Score",
                                           style=discord.ButtonStyle.secondary)
        section = discord.ui.Section(discord.ui.TextDisplay("* **Case Sensitivity:** Whether the case (uppercase or lowercase) should match exactly with the trigger string."), accessory=case_btn)
        container.add_item(section)

        section = discord.ui.Section(discord.ui.TextDisplay(
            "* **Mode:** The Match Mode for the Autoresponse. Pick between Exact, Partial, and Fuzzy. For more info, refer to the Autoresponse dashboard."),
                                    accessory=mode_btn)
        container.add_item(section)

        section = discord.ui.Section(discord.ui.TextDisplay(
            "* **Fuzzy Score:** The fuzzy score is a number between 0 and 100 that tells you how similar the message is to the trigger string, where 100 is a perfect match and 0 means they have nothing in common. To avoid spam, the lowest number you can pick is **25**."),
            accessory=edit_fuzzy_btn)

        if self.draft.get("match_mode", "exact") == "fuzzy":
            container.add_item(section)


        container.add_item(discord.ui.Separator())

        save_btn = discord.ui.Button(label="Save and Start Autoresponse", style=discord.ButtonStyle.success)
        row2 = discord.ui.ActionRow()
        row2.add_item(save_btn)
        container.add_item(row2)
        self.add_item(container)

        async def toggle_case(interaction: discord.Interaction):
            self.draft["case_sensitive"] = not self.draft.get("case_sensitive", False)
            self.build_layout()
            await interaction.response.edit_message(view=self)

        async def toggle_mode(interaction: discord.Interaction):
            current = self.draft.get("match_mode", "exact")
            new_mode = {
                "exact": "partial",
                "partial": "fuzzy",
                "fuzzy": "exact",
            }.get(current, "exact")
            self.draft["match_mode"] = new_mode
            self.build_layout()
            await interaction.response.edit_message(view=self)

        async def edit_fuzzy(interaction: discord.Interaction):
            modal = DraftFuzzyScoreModal(self.draft, parent_view=self)
            await interaction.response.send_modal(modal)

        async def save_and_start(interaction: discord.Interaction):
            record = await self.cog.create_autoresponse_from_draft(self.guild_id, interaction.user.id, self.draft)
            await interaction.response.defer()
            await interaction.delete_original_response()
            await interaction.followup.send(
                content=f"Autoresponse for trigger `{record.trigger}` saved and enabled successfully!", ephemeral=True
            )

        case_btn.callback = toggle_case
        mode_btn.callback = toggle_mode
        edit_fuzzy_btn.callback = edit_fuzzy
        save_btn.callback = save_and_start


class DraftFuzzyScoreModal(discord.ui.Modal):
    def __init__(self, draft: Dict[str, Any], parent_view: FinalStep):
        super().__init__(title="Edit Fuzzy Match Score")
        self.draft = draft
        self.parent_view = parent_view

        self.score_input = discord.ui.TextInput(
            label="Fuzzy Match Threshold (25-100)",
            default=str(self.draft.get("fuzzy_threshold", 75)),
            required=True,
            max_length=3,
        )
        self.add_item(self.score_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            value = int(self.score_input.value)
            if not 25 <= value <= 100:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("Enter a whole number between 25 and 100.", ephemeral=True)
            return

        self.draft["fuzzy_threshold"] = value
        self.parent_view.build_layout()
        await interaction.response.edit_message(view=self.parent_view)


class CreateAutoresponseView(PrivateLayoutView):
    def __init__(self, user, cog: "Autoresponse", guild_id: int, draft: Dict[str, Any]):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.draft = draft
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("### Step 1: Enter the string that you want Dopamine to listen for"))
        container.add_item(discord.ui.Separator())

        trigger_btn = discord.ui.Button(label="Enter Trigger", style=discord.ButtonStyle.primary)

        async def enter_trigger(interaction: discord.Interaction):
            modal = TriggerCreateModal(self, self.draft)
            await interaction.response.send_modal(modal)

        trigger_btn.callback = enter_trigger
        row = discord.ui.ActionRow()
        row.add_item(trigger_btn)
        container.add_item(row)
        self.add_item(container)


class TriggerCreateModal(discord.ui.Modal):
    def __init__(self, parent_view: CreateAutoresponseView, draft: Dict[str, Any]):
        super().__init__(title="Enter Trigger")
        self.parent_view = parent_view
        self.draft = draft

        self.trigger_input = discord.ui.TextInput(
            label="Trigger String",
            placeholder="Enter the text that will trigger the response",
            required=True,
            max_length=200,
        )
        self.add_item(self.trigger_input)

    async def on_submit(self, interaction: discord.Interaction):
        trigger_val = self.trigger_input.value.strip()

        if self.parent_view.cog.is_trigger_duplicate(self.parent_view.guild_id, trigger_val):
            await interaction.response.send_message(
                f"An autoresponse with the trigger `{trigger_val}` already exists in this server!",
                ephemeral=True
            )
            return

        self.draft["trigger"] = trigger_val
        view = ResponseTypeView(self.parent_view.user, self.parent_view.cog, self.parent_view.guild_id, self.draft)
        await interaction.response.edit_message(view=view)


class ResponseTypeView(PrivateLayoutView):
    def __init__(self, user, cog: "Autoresponse", guild_id: int, draft: Dict[str, Any]):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.draft = draft
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("### Step 2: Select a type of message for the response to continue creating Autoresponse"))
        container.add_item(discord.ui.Separator())

        text_btn = discord.ui.Button(label="Text", style=discord.ButtonStyle.primary)
        embed_btn = discord.ui.Button(label="Embed", style=discord.ButtonStyle.primary)

        async def choose_text(interaction: discord.Interaction):
            modal = TextResponseModal(self.draft)
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
                self.draft["response_type"] = "embed"
                self.draft["embed_content"] = content or None
                self.draft["embed_data"] = embed_obj.to_dict()
                view = ChannelSelect(self.user, self.cog, self.guild_id, draft=self.draft, autoresponse=None, firsttime=1)
                await inter.response.edit_message(content=None, embed=None, view=view)

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


class TextResponseModal(discord.ui.Modal):
    def __init__(self, draft: Dict[str, Any]):
        super().__init__(title="Step 2.5: Enter Text Response")
        self.draft = draft

        self.content_input = discord.ui.TextInput(
            label="Response Content",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=2000,
        )
        self.add_item(self.content_input)

    async def on_submit(self, interaction: discord.Interaction):
        self.draft["response_type"] = "text"
        self.draft["response_text"] = self.content_input.value
        view = ChannelSelect(interaction.user, interaction.client.get_cog("Autoresponse"), interaction.guild.id,
                             draft=self.draft, autoresponse=None, firsttime=1)
        await interaction.response.edit_message(view=view)


class Autoresponse(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db_pool: Optional[asyncio.Queue[aiosqlite.Connection]] = None
        self.cache: Dict[int, Dict[int, AutoresponseRecord]] = {}

    async def cog_load(self):
        await self.init_pools(pool_size=5)
        await self.init_db()
        await self.populate_cache()

    async def cog_unload(self):
        if self.db_pool is not None:
            while not self.db_pool.empty():
                try:
                    conn = self.db_pool.get_nowait()
                    await conn.close()
                except (asyncio.QueueEmpty, Exception):
                    break

    async def init_pools(self, pool_size: int = 5):
        if self.db_pool is None:
            self.db_pool = asyncio.Queue(maxsize=pool_size)
            for _ in range(pool_size):
                conn = await aiosqlite.connect(
                    ARSPDB_PATH,
                    timeout=5,
                )
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
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS autoresponses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER,
                    trigger TEXT,
                    response_type TEXT,
                    response_text TEXT,
                    embed_content TEXT,
                    embed_data TEXT,
                    channels TEXT,
                    match_mode TEXT,
                    fuzzy_threshold INTEGER DEFAULT 75,
                    case_sensitive INTEGER DEFAULT 0,
                    created_by INTEGER,
                    created_at INTEGER
                )
                """
            )
            await db.commit()

    async def populate_cache(self):
        async with self.acquire_db() as db:
            async with db.execute(
                """
                SELECT id, guild_id, trigger, response_type, response_text, embed_content,
                       embed_data, channels, match_mode, fuzzy_threshold, case_sensitive,
                       created_by, created_at
                FROM autoresponses
                """
            ) as cursor:
                rows = await cursor.fetchall()
                columns = [col[0] for col in cursor.description]

        for row in rows:
            data = dict(zip(columns, row))
            record = AutoresponseRecord(
                id=data["id"],
                guild_id=data["guild_id"],
                trigger=data["trigger"],
                response_type=data["response_type"],
                response_text=data.get("response_text"),
                embed_content=data.get("embed_content"),
                embed_data=_deserialize_embed_data(data.get("embed_data")),
                channels=_deserialize_channels(data.get("channels")),
                match_mode=data.get("match_mode") or "exact",
                fuzzy_threshold=int(data.get("fuzzy_threshold") or 75),
                case_sensitive=bool(data.get("case_sensitive", 0)),
                created_by=data.get("created_by") or 0,
                created_at=data.get("created_at") or int(time.time()),
            )
            self.cache.setdefault(record.guild_id, {})[record.id] = record

    def is_trigger_duplicate(self, guild_id: int, trigger: str, exclude_id: Optional[int] = None) -> bool:
        guild_responses = self.cache.get(guild_id, {})
        trigger_to_check = trigger.lower()

        for record in guild_responses.values():
            if record.id == exclude_id:
                continue
            if record.trigger.lower() == trigger_to_check:
                return True
        return False

    def get_guild_autoresponses(self, guild_id: int) -> List[AutoresponseRecord]:
        return list(self.cache.get(guild_id, {}).values())

    async def create_autoresponse_from_draft(self, guild_id: int, user_id: int, draft: Dict[str, Any]) -> AutoresponseRecord:
        now_ts = int(time.time())
        channels_raw = _serialize_channels(draft.get("channels"))
        embed_data_raw = _serialize_embed_data(draft.get("embed_data"))

        async with self.acquire_db() as db:
            cursor = await db.execute(
                """
                INSERT INTO autoresponses (
                    guild_id, trigger, response_type, response_text, embed_content,
                    embed_data, channels, match_mode, fuzzy_threshold, case_sensitive,
                    created_by, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    draft.get("trigger"),
                    draft.get("response_type"),
                    draft.get("response_text"),
                    draft.get("embed_content"),
                    embed_data_raw,
                    channels_raw,
                    draft.get("match_mode", "exact"),
                    int(draft.get("fuzzy_threshold", 75)),
                    int(bool(draft.get("case_sensitive", False))),
                    user_id,
                    now_ts,
                ),
            )
            await db.commit()
            new_id = cursor.lastrowid

        record = AutoresponseRecord(
            id=new_id,
            guild_id=guild_id,
            trigger=draft.get("trigger"),
            response_type=draft.get("response_type"),
            response_text=draft.get("response_text"),
            embed_content=draft.get("embed_content"),
            embed_data=draft.get("embed_data"),
            channels=draft.get("channels"),
            match_mode=draft.get("match_mode", "exact"),
            fuzzy_threshold=int(draft.get("fuzzy_threshold", 75)),
            case_sensitive=bool(draft.get("case_sensitive", False)),
            created_by=user_id,
            created_at=now_ts,
        )
        self.cache.setdefault(guild_id, {})[new_id] = record
        return record

    async def update_autoresponse_trigger(self, guild_id: int, ar_id: int, new_trigger: str):
        async with self.acquire_db() as db:
            await db.execute(
                "UPDATE autoresponses SET trigger = ? WHERE id = ? AND guild_id = ?",
                (new_trigger, ar_id, guild_id),
            )
            await db.commit()
        if guild_id in self.cache and ar_id in self.cache[guild_id]:
            self.cache[guild_id][ar_id].trigger = new_trigger

    async def update_autoresponse_channels(self, guild_id: int, ar_id: int, channels: Optional[Set[int]]):
        raw = _serialize_channels(channels)
        async with self.acquire_db() as db:
            await db.execute(
                "UPDATE autoresponses SET channels = ? WHERE id = ? AND guild_id = ?",
                (raw, ar_id, guild_id),
            )
            await db.commit()
        if guild_id in self.cache and ar_id in self.cache[guild_id]:
            self.cache[guild_id][ar_id].channels = channels

    async def update_autoresponse_mode(self, guild_id: int, ar_id: int, mode: str):
        async with self.acquire_db() as db:
            await db.execute(
                "UPDATE autoresponses SET match_mode = ? WHERE id = ? AND guild_id = ?",
                (mode, ar_id, guild_id),
            )
            await db.commit()
        if guild_id in self.cache and ar_id in self.cache[guild_id]:
            self.cache[guild_id][ar_id].match_mode = mode

    async def update_autoresponse_case(self, guild_id: int, ar_id: int, case_sensitive: bool):
        async with self.acquire_db() as db:
            await db.execute(
                "UPDATE autoresponses SET case_sensitive = ? WHERE id = ? AND guild_id = ?",
                (int(case_sensitive), ar_id, guild_id),
            )
            await db.commit()
        if guild_id in self.cache and ar_id in self.cache[guild_id]:
            self.cache[guild_id][ar_id].case_sensitive = case_sensitive

    async def update_autoresponse_fuzzy(self, guild_id: int, ar_id: int, threshold: int):
        async with self.acquire_db() as db:
            await db.execute(
                "UPDATE autoresponses SET fuzzy_threshold = ? WHERE id = ? AND guild_id = ?",
                (threshold, ar_id, guild_id),
            )
            await db.commit()
        if guild_id in self.cache and ar_id in self.cache[guild_id]:
            self.cache[guild_id][ar_id].fuzzy_threshold = threshold

    async def update_autoresponse_response_text(self, guild_id: int, ar_id: int, text: str):
        async with self.acquire_db() as db:
            await db.execute(
                "UPDATE autoresponses SET response_type = ?, response_text = ?, embed_content = NULL, embed_data = NULL "
                "WHERE id = ? AND guild_id = ?",
                ("text", text, ar_id, guild_id),
            )
            await db.commit()
        if guild_id in self.cache and ar_id in self.cache[guild_id]:
            rec = self.cache[guild_id][ar_id]
            rec.response_type = "text"
            rec.response_text = text
            rec.embed_content = None
            rec.embed_data = None

    async def update_autoresponse_embed(
        self,
        guild_id: int,
        ar_id: int,
        content: Optional[str],
        embed_data: Dict[str, Any],
    ):
        raw_embed = _serialize_embed_data(embed_data)
        async with self.acquire_db() as db:
            await db.execute(
                "UPDATE autoresponses SET response_type = ?, response_text = NULL, embed_content = ?, embed_data = ? "
                "WHERE id = ? AND guild_id = ?",
                ("embed", content, raw_embed, ar_id, guild_id),
            )
            await db.commit()
        if guild_id in self.cache and ar_id in self.cache[guild_id]:
            rec = self.cache[guild_id][ar_id]
            rec.response_type = "embed"
            rec.response_text = None
            rec.embed_content = content
            rec.embed_data = embed_data

    async def delete_autoresponse(self, guild_id: int, ar_id: int):
        async with self.acquire_db() as db:
            await db.execute(
                "DELETE FROM autoresponses WHERE id = ? AND guild_id = ?",
                (ar_id, guild_id),
            )
            await db.commit()
        if guild_id in self.cache:
            self.cache[guild_id].pop(ar_id, None)

    def data_features(self) -> list[DataFeatureMeta]:
        return [DataFeatureMeta(
            feature_id="autoresponse",
            name="Autoresponse",
            guild_export=True,
            guild_delete=True,
        )]

    async def data_export_user(self, user_id: int, *, guild_ids: list[int] | None) -> DataExportChunk:
        return DataExportChunk(feature_id="autoresponse")

    async def data_export_guild(self, guild_id: int) -> DataExportChunk:
        chunk = DataExportChunk(feature_id="autoresponse")
        async with self.acquire_db() as db:
            rows = await export_table(db, "SELECT * FROM autoresponses WHERE guild_id = ?", (guild_id,))
        chunk.guild_data[guild_id] = {"autoresponses": rows}
        return chunk

    async def data_delete_user(self, user_id: int, *, guild_ids: list[int] | None, feature_id: str | None) -> DataDeleteResult:
        return DataDeleteResult(feature_id="autoresponse")

    async def data_delete_guild(self, guild_id: int, feature_id: str | None) -> DataDeleteResult:
        if feature_id and feature_id != "autoresponse":
            return DataDeleteResult(feature_id="autoresponse")
        async with self.acquire_db() as db:
            cur = await db.execute("DELETE FROM autoresponses WHERE guild_id = ?", (guild_id,))
            await db.commit()
        self.cache.pop(guild_id, None)
        return DataDeleteResult(feature_id="autoresponse", deleted=True, rows_affected=cur.rowcount)

    async def data_monitor_guild(self, guild: discord.Guild) -> DataMonitorResult:
        return DataMonitorResult(feature_id="autoresponse")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return

        guild_id = message.guild.id
        if guild_id not in self.cache:
            return

        content = message.content or ""

        for record in list(self.cache[guild_id].values()):
            if record.channels and message.channel.id not in record.channels:
                continue

            if not record.case_sensitive:
                msg_text = content.lower()
                trigger = record.trigger.lower()
            else:
                msg_text = content
                trigger = record.trigger

            matched = False
            if record.match_mode == "exact":
                matched = msg_text == trigger
            elif record.match_mode == "partial":
                pattern = rf"\b{re.escape(trigger)}\b"
                flags = re.IGNORECASE
                matched = bool(re.search(pattern, content, flags=flags))
            elif record.match_mode == "fuzzy":
                if msg_text and trigger:
                    score = fuzz.ratio(trigger, msg_text)
                    matched = score >= record.fuzzy_threshold

            if not matched:
                continue

            try:
                if record.response_type == "text":
                    response = apply_variables(record.response_text or "", message)
                    if response:
                        await message.channel.send(response)
                elif record.response_type == "embed" and record.embed_data:
                    embed = discord.Embed.from_dict(record.embed_data)
                    content_text = apply_variables(record.embed_content or "", message) if record.embed_content else None
                    await message.channel.send(content=content_text, embed=embed)
            except Exception as e:
                if is_access_error(e):
                    await report_access_failure(
                        self.bot, message.guild.id, "autoresponse", str(message.channel.id)
                    )
                continue

    @beacon_commands.command(name="autoresponse", description="Open the Autoresponse Dashboard", permissions_preset="automation")
    async def autoresponse_dashboard(self, interaction: discord.Interaction):
        view = AutoresponseDashboard(interaction.user, self, interaction.guild.id)
        await interaction.response.send_message(view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(Autoresponse(bot))

