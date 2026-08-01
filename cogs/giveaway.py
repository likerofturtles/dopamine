import asyncio
import random
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, List, Dict, Set

import aiosqlite
import discord
from beacon import PrivateLayoutView, PrivateView, beacon_commands
from discord import app_commands
from discord.ext import commands, tasks
from natsort import natsorted, ns

from config import GDB_PATH
from utils.data_handlers import export_table
from utils.data_protocol import DataDeleteResult, DataExportChunk, DataFeatureMeta, DataMonitorResult
from utils.discord_health import is_access_error, report_access_failure
from utils.time import get_duration_to_seconds, get_now_plus_seconds_unix

ADJECTIVES = ["alpha", "beta", "delta", "sonic", "prime", "global", "pivot", "solid", "static", "linear", "vital", "core", "urban", "nomad"]
NOUNS = ["node", "link", "point", "base", "grid", "zone", "unit", "flux", "pillar", "vector", "path", "shift", "pulse", "forge"]


def generate_template_id():
    return f"{random.choice(ADJECTIVES)}-{random.choice(NOUNS)}-{random.randint(100, 999)}".lower()


@dataclass
class GiveawayDraft:
    guild_id: int
    channel_id: int
    prize: str
    winners: int
    duration: str
    host_id: Optional[int] = None
    required_roles: List[int] = None
    required_behaviour: int = 0
    blacklisted_roles: List[int] = None
    extra_entries: List[int] = None
    winner_role: List[int] = None
    image: Optional[str] = None
    thumbnail: Optional[str] = None
    color: str = "discord.Color(0x944ae8)"


class CreateChoose(PrivateLayoutView):
    def __init__(self, cog, user):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        container.add_item((discord.ui.TextDisplay("## Create Giveaway")))
        container.add_item(discord.ui.TextDisplay(
            "Choose an option below to continue creating a giveaway. Create button leads to the regular creation menu, while the other option lets you enter a template code."))
        container.add_item(discord.ui.Separator())
        create_btn = discord.ui.Button(label="Create", style=discord.ButtonStyle.primary)
        create_btn.callback = self.create_callback
        template_btn = discord.ui.Button(label="Create from Template", style=discord.ButtonStyle.secondary)
        template_btn.callback = self.template_callback
        row = discord.ui.ActionRow()

        row.add_item(create_btn)
        row.add_item(template_btn)

        container.add_item(row)

        self.add_item(container)

    async def create_callback(self, interaction: discord.Interaction):
        draft = GiveawayDraft(
            guild_id=interaction.guild.id,
            channel_id=interaction.channel.id,
            prize="Unspecified Prize",
            winners=1,
            duration="24h"
        )

        embed = self.cog.create_giveaway_embed(draft)
        view = GiveawayPreviewView(self.cog, self.user, draft)
        expires = get_now_plus_seconds_unix(900)

        await interaction.response.send_message(
            content=f"This is a preview of your giveaway. Configure it using the buttons below, then start it.\nThis preview expires **<t:{expires}:R>**!",
            embed=embed,
            view=view,
            ephemeral=False
        )
        view.message = await interaction.original_response()

    async def template_callback(self, interaction: discord.Interaction):
        view = CreatewithtemplatePage(self.cog, self.user)
        await interaction.response.edit_message(view=view)


class GiveawayEditSelect(discord.ui.Select):
    def __init__(self, cog, draft: GiveawayDraft, parent_view):
        self.cog = cog
        self.draft = draft
        self.parent_view = parent_view
        options = [
            discord.SelectOption(label="1. Prize", value="prize", description="Change the prize being given away."),
            discord.SelectOption(label="2. Duration", value="duration",
                                 description="Change how long the giveaway lasts (e.g., 1h, 2d)."),
            discord.SelectOption(label="3. Winners Count", value="winners",
                                 description="Change the number of winners."),
            discord.SelectOption(label="4. Channel", value="channel",
                                 description="Change where the giveaway is posted."),
            discord.SelectOption(label="5. Giveaway Host", value="host",
                                 description="The host name to be shown in the giveaway Embed."),
            discord.SelectOption(label="6. Extra Entries Role", value="extra",
                                 description="Roles that will give extra entries. Each role gives +1 entries."),
            discord.SelectOption(label="7. Required Roles", value="required",
                                 description="Roles required to participate."),
            discord.SelectOption(label="8. Required Roles Behaviour", value="behavior",
                                 description="The behavior of the required roles feature."),
            discord.SelectOption(label="9. Winner Roles", value="winner_role", description="Roles given to winners."),
            discord.SelectOption(label="10. Blacklisted Roles", value="blacklist",
                                 description="Roles that cannot participate."),
            discord.SelectOption(label="11. Image", value="image",
                                 description="Provide a valid URL for the Embed image."),
            discord.SelectOption(label="12. Thumbnail", value="thumbnail",
                                 description="Provide a valid URL for the Embed thumbnail."),
            discord.SelectOption(label="13. Colour", value="color", description="Set embed color (Hex or Valid Name).")
        ]
        super().__init__(placeholder="Select a setting to customize...", options=options)

    async def callback(self, interaction: discord.Interaction):
        value = self.values[0]
        if value in ["prize", "winners", "duration"]:
            return await interaction.response.send_modal(GiveawayMetadataModal(value, self.draft, self.parent_view))

        if value in ["image", "thumbnail", "color"]:
            return await interaction.response.send_modal(GiveawayVisualsModal(value, self.draft, self.parent_view))

        new_view = None
        msg = ""

        if value == "channel":
            new_view = ChannelSelectView(self.draft, self.parent_view)
            msg = "Select the target channel:"

        elif value == "behavior":
            new_view = discord.ui.View(timeout=180)
            new_view.add_item(BehaviorSelect(self.draft, self.parent_view))
            msg = "Change required role behaviour:"

        elif value == "extra":
            new_view = RoleSelectView("extra", "Extra Entry Roles", self.draft, self.parent_view)
            msg = "Choose roles for extra entries:"

        elif value == "required":
            new_view = RoleSelectView("required", "Required Roles", self.draft, self.parent_view)
            msg = "Choose roles required to enter:"

        elif value == "winner_role":
            new_view = WinnerRoleSelectView("winner_role", "Winner Role", self.draft, self.parent_view)
            msg = "Choose roles to be given to winner(s):"

        elif value == "blacklist":
            new_view = RoleSelectView("blacklist", "Blacklisted Roles", self.draft, self.parent_view)
            msg = "Choose roles that can't participate:"

        elif value == "host":
            new_view = MemberSelectView(self.draft, self.parent_view)
            msg = "Choose the host for this giveaway:"

        if msg and new_view:
            await interaction.response.send_message(msg, view=new_view, ephemeral=True)


class GiveawayMetadataModal(discord.ui.Modal):
    def __init__(self, trait: str, draft: GiveawayDraft, parent_view):
        super().__init__(title=f"Edit Giveaway {trait.title()}")
        self.trait = trait
        self.draft = draft
        self.parent_view = parent_view

        current_value = ""
        if trait == "prize":
            current_value = str(self.draft.prize)
        elif trait == "winners":
            current_value = str(self.draft.winners)
        elif trait == "duration":
            current_value = str(self.draft.duration)

        placeholder = "e.g. 1d 12h" if trait == "duration" else "Type here..."
        self.input_field = discord.ui.TextInput(
            label=f"Enter {trait.title()}",
            placeholder=placeholder,
            default=current_value,
            required=True
        )
        self.add_item(self.input_field)

    async def on_submit(self, interaction: discord.Interaction):
        value = self.input_field.value

        if self.trait == "prize":
            self.draft.prize = value
        elif self.trait == "winners":
            if not value.isdigit():
                return await interaction.response.send_message("Winners must be a number!", ephemeral=True)
            self.draft.winners = int(value)
        elif self.trait == "duration":
            seconds = get_duration_to_seconds(value)
            if seconds <= 0:
                return await interaction.response.send_message("Invalid duration format!", ephemeral=True)
            self.draft.duration = value

        new_embed = self.parent_view.cog.create_giveaway_embed(self.draft)
        await self.parent_view.message.edit(embed=new_embed)
        await interaction.response.send_message(f"Updated **{self.trait}** successfully!", ephemeral=True)


class ChannelSelectView(discord.ui.View):
    def __init__(self, draft: GiveawayDraft, parent_view):
        super().__init__(timeout=300)
        self.draft = draft
        self.parent_view = parent_view
        self.select = discord.ui.ChannelSelect(
            placeholder="Choose a channel...",
            channel_types=[discord.ChannelType.text]
        )
        self.select.callback = self.callback
        self.add_item(self.select)

    async def callback(self, interaction: discord.Interaction):
        self.draft.channel_id = self.select.values[0].id
        new_embed = self.parent_view.cog.create_giveaway_embed(self.draft)
        await self.parent_view.message.edit(embed=new_embed)
        await interaction.response.send_message(f"Target channel updated to {self.select.values[0].mention}",
                                                ephemeral=True)


class GiveawayVisualsModal(discord.ui.Modal):
    def __init__(self, trait: str, draft: GiveawayDraft, parent_view):
        super().__init__(title=f"Edit Giveaway {trait.title()}")
        self.trait = trait
        self.draft = draft
        self.parent_view = parent_view
        self.input_field = discord.ui.TextInput(
            label=f"Enter {trait}",
            placeholder="Type here...",
            required=True
        )
        self.add_item(self.input_field)

    async def on_submit(self, interaction: discord.Interaction):
        value = self.input_field.value

        if self.trait == "image":
            self.draft.image = value
        elif self.trait == "thumbnail":
            self.draft.thumbnail = value
        elif self.trait == "color":
            self.draft.color = value

        new_embed = self.parent_view.cog.create_giveaway_embed(self.draft)
        await self.parent_view.message.edit(embed=new_embed)

        await interaction.response.send_message(f"Updated **{self.trait}** successfully!", ephemeral=True)


class GoToPageModalPaginator(discord.ui.Modal):
    def __init__(self, current_page: int, max_pages: int, parent_view):
        super().__init__(title="Go to Page")
        self.parent_view = parent_view
        self.max_pages = max_pages
        self.page_input = discord.ui.TextInput(
            label=f"Enter Page Number (1-{max_pages})",
            default=str(current_page + 1),
            min_length=1,
            max_length=len(str(max_pages))
        )
        self.add_item(self.page_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            page_num = int(self.page_input.value)
            if 1 <= page_num <= self.max_pages:
                self.parent_view.current_page = page_num - 1
                await interaction.response.edit_message(embed=self.parent_view.get_embed(), view=self.parent_view)
            else:
                await interaction.response.send_message(f"Please enter a number between 1 and {self.max_pages}.",
                                                        ephemeral=True)
        except ValueError:
            await interaction.response.send_message("Invalid number entered.", ephemeral=True)


class ParticipantPaginator(discord.ui.View):
    def __init__(self, bot, processed_participants: list, prize: str, guild: discord.Guild):
        super().__init__(timeout=120)
        self.bot = bot
        self.prize = prize
        self.guild = guild
        self.current_page = 0
        self.per_page = 10
        self.show_tags = False
        self.processed_participants = processed_participants

    async def get_embed(self):
        start = self.current_page * self.per_page
        end = start + self.per_page
        page_list = self.processed_participants[start:end]

        lines = []
        for item in page_list:
            user_id = item['id']
            user = self.bot.get_user(user_id) or self.guild.get_member(user_id)

            if not user:
                try:
                    user = await self.bot.fetch_user(user_id)
                except Exception:
                    user = None

            if user:
                name = user.name if self.show_tags else f"<@{user.id}>"
            else:
                name = f"Unknown({user_id})"

            lines.append(f"• **{name}** (**{item['entries']}** entries)")

        mentions = "\n".join(lines) or "No participants yet."
        total_pages = (len(self.processed_participants) - 1) // self.per_page + 1
        total_count = len(self.processed_participants)
        self.go_to_page.label = f"Page {self.current_page + 1} of {total_pages}"
        embed = discord.Embed(
            title=f"<:dopamine:1479037593537613945> Participants for **{self.prize}**",
            description=mentions,
            color=discord.Color(0x944ae8)
        )
        embed.set_footer(text=f"Total Participants: {total_count}")
        return embed

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.primary)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
            await interaction.response.edit_message(embed=await self.get_embed(), view=self)

    @discord.ui.button(label="Page 0 of 0", style=discord.ButtonStyle.gray)
    async def go_to_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        total_pages = (len(self.processed_participants) - 1) // self.per_page + 1
        await interaction.response.send_modal(GoToPageModal(self, total_pages))

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.primary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if (self.current_page + 1) * self.per_page < len(self.processed_participants):
            self.current_page += 1
            await interaction.response.edit_message(embed=await self.get_embed(), view=self)

    @discord.ui.button(label="Show User Tags", style=discord.ButtonStyle.gray)
    async def toggle_tags(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.show_tags = not self.show_tags
        button.label = "Show Usernames" if self.show_tags else "Show User Tags"
        await interaction.response.edit_message(embed=await self.get_embed(), view=self)


class BehaviorSelect(discord.ui.Select):
    def __init__(self, draft: GiveawayDraft, parent_view):
        options = [
            discord.SelectOption(label="All required roles", value="0",
                                 description="Participant must have every role listed."),
            discord.SelectOption(label="One of the required roles", value="1",
                                 description="Participant must have at least one role listed.")
        ]

        super().__init__(placeholder="Choose role requirement behaviour...", options=options)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        self.draft.required_behaviour = int(self.values[0])
        new_embed = self.parent_view.cog.create_giveaway_embed(self.draft)
        await self.parent_view.message.edit(embed=new_embed)
        await interaction.response.send_message("Role requirement behaviour updated successfully!", ephemeral=True)


class GiveawayPreviewView(PrivateView):
    def __init__(self, cog, user, draft: GiveawayDraft, template_mode: bool = False):
        super().__init__(user, timeout=900)
        self.cog = cog
        self.draft = draft
        self.message = None
        self.template_mode = template_mode

        if self.template_mode:
            self.start_button.label = "Save Template"
            self.start_button.style = discord.ButtonStyle.green
        else:
            self.start_button.label = "Start"
            self.start_button.style = discord.ButtonStyle.green

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(content="This giveaway preview has expired.", embed=None, view=None)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Start", style=discord.ButtonStyle.green)
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):

        if self.template_mode:
            await interaction.response.edit_message(content="Saving Giveaway Template...", embed=None, view=None)
            template_id = await self.cog.save_template(interaction, self.draft)

            await interaction.edit_original_response(f"Saved Giveaway Template successfully! ID: `{template_id}`")

            self.stop()
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        channel = self.cog.bot.get_channel(self.draft.channel_id) or await self.cog.bot.fetch_channel(self.draft.channel_id)
        if not channel:
            return await interaction.followup.send(
                "I can't find the channel chosen for the giveaway!\nEnsure that I have the necessary permissions.")

        giveaway_id = int(discord.utils.utcnow().timestamp()) + random.randint(1, 69)

        seconds = get_duration_to_seconds(self.draft.duration)
        end_time = get_now_plus_seconds_unix(seconds)

        view = GiveawayJoinView(self.cog, giveaway_id)

        embed = self.cog.create_giveaway_embed(self.draft, preview_active_end=end_time)
        embed.set_footer(text=f"ID: {giveaway_id}")

        msg = await channel.send(embed=embed, view=view)

        await self.cog.save_giveaway(self.draft, msg.id, giveaway_id, end_time)
        self.cog.bot.add_view(view)

        success_embed = discord.Embed(description=f"Giveaway started successfully in {channel.mention}!",
                                      colour=discord.Colour.green())
        embed.set_footer(text=f"ID: {giveaway_id}")

        await interaction.followup.send(embed=success_embed)

        try:
            await interaction.message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass

        self.stop()

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.primary)
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.message = interaction.message
        view = discord.ui.View()
        select = GiveawayEditSelect(cog=self.cog, draft=self.draft, parent_view=self)
        view.add_item(select)

        await interaction.response.send_message(
            embed=discord.Embed(title="Edit Giveaway", description="Select a setting...",
                                color=discord.Color(0x944ae8)),
            view=view,
            ephemeral=True
        )
        self.message = interaction.message

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content=None, embed=discord.Embed(title="Giveaway Creation Cancelled."), view=None)
        self.stop()


class MemberSelectView(discord.ui.View):
    def __init__(self, draft: GiveawayDraft, parent_view):
        super().__init__(timeout=300)
        self.draft = draft
        self.parent_view = parent_view
        self.select = discord.ui.UserSelect(placeholder="Pick a host...", min_values=1, max_values=1)
        self.select.callback = self.callback
        self.add_item(self.select)

    async def callback(self, interaction: discord.Interaction):
        self.draft.host_id = self.select.values[0].id
        new_embed = self.parent_view.cog.create_giveaway_embed(self.draft)
        await self.parent_view.message.edit(embed=new_embed)
        await interaction.response.send_message(f"Giveaway host updated to {self.select.values[0].mention}",
                                                ephemeral=True)


class RoleSelectView(discord.ui.View):
    def __init__(self, key: str, label: str, draft: GiveawayDraft, parent_view):
        super().__init__(timeout=300)
        self.key = key
        self.draft = draft
        self.parent_view = parent_view
        self.select = discord.ui.RoleSelect(placeholder=f"Pick {label}...", min_values=1, max_values=10)
        self.select.callback = self.callback
        self.add_item(self.select)

    async def callback(self, interaction: discord.Interaction):
        role_ids = [role.id for role in self.select.values]
        text = None
        if self.key == "extra":
            self.draft.extra_entries = role_ids
            text = "Extra Entry Roles"
        elif self.key == "required":
            self.draft.required_roles = role_ids
            text = "Required Roles"
        elif self.key == "blacklist":
            self.draft.blacklisted_roles = role_ids
            text = "Blacklisted Roles"

        new_embed = self.parent_view.cog.create_giveaway_embed(self.draft)
        await self.parent_view.message.edit(embed=new_embed)


        await interaction.response.send_message(f"Updated **{text}** successfully!", ephemeral=True)


class WinnerRoleSelectView(discord.ui.View):
    def __init__(self, key: str, label: str, draft: GiveawayDraft, parent_view):
        super().__init__(timeout=300)
        self.key = key
        self.draft = draft
        self.parent_view = parent_view
        self.select = discord.ui.RoleSelect(placeholder=f"Pick {label}...", min_values=1, max_values=10)
        self.select.callback = self.callback
        self.add_item(self.select)

    async def callback(self, interaction: discord.Interaction):
        role_ids = [role.id for role in self.select.values]
        text = None
        if self.key == "winner_role":
            self.draft.winner_role = role_ids
            text = "Winner Role"

        new_embed = self.parent_view.cog.create_giveaway_embed(self.draft)
        await self.parent_view.message.edit(embed=new_embed)

        await interaction.response.send_message(f"Updated **{text}** successfully!", ephemeral=True)


class GiveawayJoinView(discord.ui.View):
    def __init__(self, cog, giveaway_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.giveaway_id = giveaway_id

        self.join_button.custom_id = f"gw:join:{giveaway_id}"
        self.list_button.custom_id = f"gw:list:{giveaway_id}"

        self.update_button_label()

    def update_button_label(self):
        count = len(self.cog.participant_cache.get(self.giveaway_id, set()))
        self.join_button.label = f"{count}" if count > 0 else "0"

    @discord.ui.button(
        emoji="🎉",
        style=discord.ButtonStyle.blurple
    )
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.message.embeds:
            return await interaction.response.send_message(
                "Uh-oh! I'm afraid that the message you interacted with doesn't exist anymore :3", ephemeral=True)

        footer_text = interaction.message.embeds[0].footer.text
        try:
            giveaway_id = int(footer_text.split(": ")[1])
        except (IndexError, ValueError):
            return await interaction.response.send_message("Uh-oh! I couldn't find the Giveaway ID. Perhaps try again?",
                                                           ephemeral=True)

        g = self.cog.giveaway_cache.get(giveaway_id)

        if not g or g['ended'] == 1:
            return await interaction.response.send_message("Uh-oh! I'm afraid that this giveaway has already ended!",
                                                           ephemeral=True)

        if g['blacklisted_roles']:
            blacklisted_ids = [int(r) for r in g['blacklisted_roles'].split(",")]
            if any(role.id in blacklisted_ids for role in interaction.user.roles):
                return await interaction.response.send_message(
                    "Uh-oh! You cannot join this giveaway because you have a blacklisted role.", ephemeral=True)

        if g['required_roles']:
            req_ids = [int(r) for r in g['required_roles'].split(",")]
            user_role_ids = [role.id for role in interaction.user.roles]

            if g['req_behaviour'] == 0:
                if not all(r in user_role_ids for r in req_ids):
                    return await interaction.response.send_message(
                        "Uh-oh! You cannot join this giveaway because you don't have all the required roles.",
                        ephemeral=True)

            else:
                if not any(r in user_role_ids for r in req_ids):
                    return await interaction.response.send_message(
                        "Uh-oh! You cannot join this giveaway because you don't have one of the required roles.",
                        ephemeral=True)

        participants = self.cog.participant_cache.get(giveaway_id, set())

        async with self.cog.acquire_db() as db:
            if interaction.user.id in participants:
                participants.remove(interaction.user.id)
                await db.execute("DELETE FROM giveaway_participants WHERE giveaway_id = ? AND user_id = ?",
                                 (giveaway_id, interaction.user.id))
                msg = "You have successfully left the giveaway."
            else:
                participants.add(interaction.user.id)
                await db.execute("INSERT INTO giveaway_participants (guild_id, giveaway_id, user_id) VALUES (?, ?, ?)",
                                 (interaction.guild_id, giveaway_id, interaction.user.id))
                msg = "🎉 You have successfully entered the giveaway!"
            await db.commit()

        self.update_button_label()

        await interaction.response.send_message(msg, ephemeral=True)
        try:
            await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass

    @discord.ui.button(
        emoji="👤",
        label="Participants",
        style=discord.ButtonStyle.gray,
    )
    async def list_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.message.embeds:
            return await interaction.response.send_message(
                "Uh-oh! I'm afraid that the message you interacted with doesn't exist anymore :3", ephemeral=True)

        footer_text = interaction.message.embeds[0].footer.text

        try:
            giveaway_id = int(footer_text.split(": ")[1])
        except (IndexError, ValueError):
            return await interaction.response.send_message("Uh-oh! I couldn't parse Giveaway ID.", ephemeral=True)

        participant_set = self.cog.participant_cache.get(giveaway_id, set())
        participants = list(participant_set)

        g = self.cog.giveaway_cache.get(giveaway_id)
        if not g:
            return await interaction.response.send_message("This giveaway data seems to be missing :/", ephemeral=True)

        if not participants:
            return await interaction.response.send_message("There are currently no participants in this giveaway!",
                                                           ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        prize = g['prize']
        extra_roles_str = g.get('extra_entry_roles', '')
        extra_roles_list = [int(r) for r in extra_roles_str.split(',')] if extra_roles_str else []

        processed_data = []
        for uid in participants:
            entries = 1
            member = interaction.guild.get_member(uid)
            if not member:
                try:
                    member = await interaction.guild.fetch_member(uid)
                except discord.HTTPException:
                    member = None

            if member and extra_roles_list:
                for role_id in extra_roles_list:
                    if any(r.id == role_id for r in member.roles):
                        entries += 1
            processed_data.append({'id': uid, 'entries': entries})

        processed_data.sort(key=lambda x: (x['entries'], x['id']), reverse=True)

        view = ParticipantPaginator(
            bot=self.cog.bot,
            processed_participants=processed_data,
            prize=prize,
            guild=interaction.guild
        )

        await interaction.followup.send(embed=await view.get_embed(), view=view, ephemeral=True)


class TemplateHomepage(PrivateLayoutView):
    def __init__(self, cog, user):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        container.add_item((discord.ui.TextDisplay("## Giveaway Templates")))
        container.add_item(discord.ui.TextDisplay(
            "Giveaway Templates allow you to quicky start a giveaway without needing to manually make one from scratch. To create a new template, go to My Templates. To browse through the list of user-created templates, click on Browse Templates."))
        container.add_item(discord.ui.Separator())

        repo_btn = discord.ui.Button(label="Browse Templates", style=discord.ButtonStyle.primary)
        repo_btn.callback = self.browse_callback
        my_btn = discord.ui.Button(label="My Templates", style=discord.ButtonStyle.secondary)
        my_btn.callback = self.mystuff_callback
        row = discord.ui.ActionRow()

        row.add_item(repo_btn)
        row.add_item(my_btn)

        container.add_item(row)

        self.add_item(container)

    async def browse_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        templates = await self.cog.fetch_templates(guild_id=interaction.guild.id, mode="browse")
        view = BrowsePage(self.cog, self.user, templates, interaction.guild.id, is_th=True)
        await interaction.edit_original_response(view=view)

    async def mystuff_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        templates = await self.cog.fetch_templates(user_id=self.user.id, mode="mine")
        view = MystuffPage(self.cog, self.user, templates)
        await interaction.edit_original_response(view=view)


class MystuffPage(PrivateLayoutView):
    def __init__(self, cog, user, templates, page=1):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.templates = templates
        self.page = page
        self.per_page = 5
        self.total_pages = (len(self.templates) - 1) // self.per_page + 1 if self.templates else 1
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        container.add_item((discord.ui.TextDisplay("## My Templates")))
        container.add_item(
            discord.ui.TextDisplay("Manage all your templates here. Publish a template, edit it, or create a new one."))
        container.add_item(discord.ui.Separator())

        start = (self.page - 1) * self.per_page
        end = start + self.per_page
        current_templates = self.templates[start:end]

        if not current_templates:
            container.add_item(discord.ui.TextDisplay("You haven't created any templates yet."))

        for t in current_templates:
            edit_btn = discord.ui.Button(label="Edit", style=discord.ButtonStyle.secondary,
                                         custom_id=f"edit:{t['template_id']}")
            edit_btn.callback = self.create_edit_callback(t)

            desc = f"**Winners:** {t['winners']}\n**Duration:** {t['duration']}\n"
            if t['channel_id']: desc += f"**Channel:** <#{t['channel_id']}>\n"
            if t['host_id']: desc += f"**Giveaway Host:** <@{t['host_id']}>\n"
            if t['image']: desc += "**Embed Image:** Yes\n"
            if t['thumbnail']: desc += "**Embed Thumbnail:** Yes\n"

            container.add_item(discord.ui.Section(discord.ui.TextDisplay(
                f"### {t['prize']}\n{desc}"), accessory=edit_btn))

        container.add_item(discord.ui.Separator())

        left_btn = discord.ui.Button(emoji="◀️", style=discord.ButtonStyle.primary, disabled=self.page == 1)
        left_btn.callback = self.prev_callback

        go_btn = discord.ui.Button(label=f"Page {self.page} of {self.total_pages}", style=discord.ButtonStyle.secondary,
                                   disabled=self.total_pages <= 1)
        go_btn.callback = self.goto_callback

        right_btn = discord.ui.Button(emoji="▶️", style=discord.ButtonStyle.primary,
                                      disabled=self.page == self.total_pages)
        right_btn.callback = self.next_callback

        row = discord.ui.ActionRow()
        row.add_item(left_btn)
        row.add_item(go_btn)
        row.add_item(right_btn)
        container.add_item(row)

        container.add_item(discord.ui.Separator())
        create_btn = discord.ui.Button(label="Create New Template", style=discord.ButtonStyle.primary)
        create_btn.callback = self.create_new_callback
        row = discord.ui.ActionRow()
        row.add_item(create_btn)
        container.add_item(row)

        back_btn = discord.ui.Button(emoji=self.cog.bot.back_emoji, label="Back", style=discord.ButtonStyle.secondary)
        back_btn.callback = self.back_callback
        row = discord.ui.ActionRow()
        row.add_item(back_btn)
        container.add_item(discord.ui.Separator())
        container.add_item(row)
        self.add_item(container)

    def create_edit_callback(self, template_data):
        async def callback(interaction: discord.Interaction):
            view = EditPage(self.cog, self.user, template_data)
            await interaction.response.edit_message(view=view)

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

    async def create_new_callback(self, interaction: discord.Interaction):
        draft = GiveawayDraft(
            guild_id=interaction.guild.id,
            channel_id=interaction.channel.id,
            prize="Template Prize",
            winners=1,
            duration="24h"
        )
        embed = self.cog.create_giveaway_embed(draft)
        view = GiveawayPreviewView(self.cog, self.user, draft, template_mode=True)
        await interaction.response.send_message(embed=embed, view=view)

    async def back_callback(self, interaction: discord.Interaction):
        view = TemplateHomepage(self.cog, self.user)
        await interaction.response.edit_message(view=view)

class EditPage(PrivateLayoutView):
    def __init__(self, cog, user, template_data):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.data = template_data
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        container.add_item((discord.ui.TextDisplay(f"## Edit: {self.data['prize']}")))
        container.add_item(discord.ui.Separator())

        t = self.data
        desc = f"**Winners:** {t['winners']}\n**Duration:** {t['duration']}\n"
        if t['channel_id']: desc += f"**Channel:** <#{t['channel_id']}>\n"
        if t['host_id']: desc += f"**Giveaway Host:** <@{t['host_id']}>\n"
        if t['image']: desc += "**Embed Image:** Yes\n"
        if t['thumbnail']: desc += "**Embed Thumbnail:** Yes\n"
        if t['color']: desc += f"**Colour:** {t['color']}"

        container.add_item(discord.ui.TextDisplay(desc))

        container.add_item(discord.ui.Separator())
        edit_btn = discord.ui.Button(label="Edit", style=discord.ButtonStyle.secondary)
        edit_btn.callback = self.edit_callback

        is_pub = self.data['is_published'] == 1
        publish_btn = discord.ui.Button(label="Unpublish" if is_pub else "Publish",
                                        style=discord.ButtonStyle.secondary if is_pub else discord.ButtonStyle.primary)
        publish_btn.callback = self.publish_callback

        delete_btn = discord.ui.Button(label="Delete", style=discord.ButtonStyle.danger)
        delete_btn.callback = self.delete_callback

        row = discord.ui.ActionRow()
        row.add_item(publish_btn)
        row.add_item(edit_btn)
        row.add_item(delete_btn)

        container.add_item(row)

        back_btn = discord.ui.Button(emoji=self.cog.bot.back_emoji, label="Back", style=discord.ButtonStyle.secondary)
        back_btn.callback = self.back_callback
        row = discord.ui.ActionRow()
        row.add_item(back_btn)
        container.add_item(discord.ui.Separator())
        container.add_item(row)
        self.add_item(container)

    async def edit_callback(self, interaction: discord.Interaction):
        draft = self.cog.template_to_draft(self.data, interaction.guild.id, interaction)
        view = GiveawayPreviewView(self.cog, self.user, draft, template_mode=True)
        draft.template_id = self.data['template_id']

        embed = self.cog.create_giveaway_embed(draft)
        await interaction.response.send_message(embed=embed, view=view)

    async def publish_callback(self, interaction: discord.Interaction):
        if self.data['is_published'] == 1:
            await self.cog.set_publish_status(self.data['template_id'], False, interaction)
        else:
            view = ConfirmationView(self.user, self.data['template_id'], self.cog, self.data['creation_guild_id'],
                                    self.data['prize'])
            await interaction.response.send_message(view=view, ephemeral=True)

    async def delete_callback(self, interaction: discord.Interaction):
        view = DestructiveConfirmationView(self.user, self.data['template_id'], self.cog, self.data['creation_guild_id'],
                                           self.data['prize'])
        await interaction.response.send_message(view=view, ephemeral=True)

    async def back_callback(self, interaction: discord.Interaction):
        templates = await self.cog.fetch_templates(user_id=self.user.id, mode="mine")
        view = MystuffPage(self.cog, self.user, templates)
        await interaction.response.edit_message(view=view)

class BrowsePage(PrivateLayoutView):
    def __init__(self, cog, user, templates, guild_id, page=1, exclude_global=False, is_th: bool = False, container_header: str = None):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.user = user
        self.templates = templates
        self.guild_id = guild_id
        self.container_header = container_header
        self.page = page
        self.exclude_global = exclude_global
        self.per_page = 5
        self.is_th = is_th
        self.templates.sort(key=lambda x: x.get('usage_count', 0), reverse=True)
        self.current_sort = 'popular'
        self.filtered_templates = []
        self.filter_templates()
        self.total_pages = (len(self.filtered_templates) - 1) // self.per_page + 1 if self.filtered_templates else 1
        self.build_layout()

    def filter_templates(self):
        if self.exclude_global:
            self.filtered_templates = [t for t in self.templates if t['creation_guild_id'] == self.guild_id]
        else:
            self.filtered_templates = self.templates

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        count_text = f"{len(self.filtered_templates)} Total Templates"
        if self.exclude_global:
            count_text = f"{len(self.filtered_templates)} (Current Server Only)"
        if not self.container_header:
            text = f"## Browse — {count_text}"
        else:
            text = f"## {self.container_header}"

        container.add_item((discord.ui.TextDisplay(text)))
        container.add_item(discord.ui.TextDisplay(
            "Browse Giveaway templates here. Use the buttons and dropdowns below to search, or sort."))
        container.add_item(discord.ui.Separator())

        start = (self.page - 1) * self.per_page
        end = start + self.per_page
        current = self.filtered_templates[start:end]

        if not current:
            container.add_item(discord.ui.TextDisplay("*No templates found.*"))

        for t in current:
            use_btn = discord.ui.Button(label="Use", style=discord.ButtonStyle.primary,
                                        custom_id=f"use:{t['template_id']}")
            use_btn.callback = self.create_use_callback(t)

            is_local = t['creation_guild_id'] == self.guild_id

            if not is_local:
                desc = f"**Created by:** **{t['creator_name']}** in **{t['guild_name']}**\n**Template ID:** {t['template_id']}\n**Winners:** {t['winners']}\n**Duration:** {t['duration']}\n"
                if t['image']: desc += "**Embed Image:** Yes\n"
                if t['thumbnail']: desc += "**Embed Thumbnail:** Yes\n"
                title = f"### ➤ {t['prize']} - {t['usage_count']} uses"
            else:
                desc = \
                    f"**Created by:** **{t['creator_name']}** in **{t['guild_name']}**\n**Template ID:** {t['template_id']}\n**Winners:** {t['winners']}\n**Duration:** {t['duration']}\n"
                if t['channel_id']: desc += f"**Channel:** <#{t['channel_id']}>\n"
                if t['host_id']: desc += f"**Giveaway Host:** <@{t['host_id']}>\n"
                title = f"### ➤ {t['prize']}"

            container.add_item(discord.ui.Section(discord.ui.TextDisplay(f"{title}\n{desc}"), accessory=use_btn))

        container.add_item(discord.ui.Separator())

        left_btn = discord.ui.Button(emoji="◀️", style=discord.ButtonStyle.primary, disabled=self.page == 1)
        left_btn.callback = self.prev_callback
        go_btn = discord.ui.Button(label=f"Page {self.page} of {self.total_pages}", style=discord.ButtonStyle.secondary,
                                   disabled=self.total_pages <= 1)
        go_btn.callback = self.goto_callback
        right_btn = discord.ui.Button(emoji="▶️", style=discord.ButtonStyle.primary,
                                      disabled=self.page == self.total_pages)
        right_btn.callback = self.next_callback

        row = discord.ui.ActionRow()
        row.add_item(left_btn)
        row.add_item(go_btn)
        row.add_item(right_btn)
        container.add_item(row)
        container.add_item(discord.ui.Separator())

        searchprize_btn = discord.ui.Button(label="Search by Prize", style=discord.ButtonStyle.primary)
        searchprize_btn.callback = self.search_prize_callback
        searchID_btn = discord.ui.Button(label="Search by ID", style=discord.ButtonStyle.primary)
        searchID_btn.callback = self.search_id_callback

        exclude_label = "Include Global Templates" if self.exclude_global else "Exclude Global Templates"
        exclude_btn = discord.ui.Button(label=exclude_label, style=discord.ButtonStyle.secondary)
        exclude_btn.callback = self.exclude_callback

        row = discord.ui.ActionRow()
        row.add_item(searchprize_btn)
        row.add_item(searchID_btn)
        row.add_item(exclude_btn)
        container.add_item(row)

        sort_options = [
            discord.SelectOption(label='Most Popular', value='popular'),
            discord.SelectOption(label='Least Popular', value='unpopular'),
            discord.SelectOption(label='Alphabetical (A–Z)', value='alpha'),
            discord.SelectOption(label='Alphabetical (Z–A)', value='revalpha')
        ]

        for option in sort_options:
            if option.value == self.current_sort:
                option.default = True

        sort_dropdown = discord.ui.Select(
            placeholder="Sort by..." if not self.current_sort else None,
            options=sort_options
        )
        sort_dropdown.callback = self.sort_callback

        row = discord.ui.ActionRow()
        row.add_item(sort_dropdown)
        container.add_item(row)

        back_btn = discord.ui.Button(emoji=self.cog.bot.back_emoji, label="Back", style=discord.ButtonStyle.secondary)
        back_btn.callback = self.back_callback
        row = discord.ui.ActionRow()
        row.add_item(back_btn)
        container.add_item(discord.ui.Separator())
        container.add_item(row)

        self.add_item(container)

    def create_use_callback(self, t):
        async def callback(interaction: discord.Interaction):
            draft = self.cog.template_to_draft(t, interaction.guild.id, interaction)
            embed = self.cog.create_giveaway_embed(draft)
            view = GiveawayPreviewView(self.cog, self.user, draft)
            expires = get_now_plus_seconds_unix(900)
            await interaction.response.send_message(content=f"Loaded template! This is a preview of your giveaway. Configure it using the buttons below, then start it.\nThis preview expires **<t:{expires}:R>**!", embed=embed, view=view)
            await self.cog.increment_usage(t['template_id'])

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

    async def exclude_callback(self, interaction: discord.Interaction):
        self.exclude_global = not self.exclude_global
        self.page = 1
        self.filter_templates()
        self.total_pages = (len(self.filtered_templates) - 1) // self.per_page + 1 if self.filtered_templates else 1
        self.build_layout()
        await interaction.response.edit_message(view=self)

    async def sort_callback(self, interaction: discord.Interaction):
        val = interaction.data['values'][0]
        self.current_sort = val
        if val == 'popular':
            self.templates.sort(key=lambda x: x['usage_count'], reverse=True)
        elif val == 'unpopular':
            self.templates.sort(key=lambda x: x['usage_count'])
        elif val == 'alpha':
            self.templates = natsorted(self.templates, key=lambda x: x['prize'], alg=ns.IGNORECASE)
        elif val == 'revalpha':
            self.templates = natsorted(self.templates, key=lambda x: x['prize'], alg=ns.IGNORECASE, reverse=True)

        self.page = 1
        self.filter_templates()
        self.build_layout()
        await interaction.response.edit_message(view=self)

    async def search_prize_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SearchModal("prize", self))

    async def search_id_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SearchModal("id", self))

    async def back_callback(self, interaction: discord.Interaction):
        if self.is_th:
            view = TemplateHomepage(self.cog, self.user)
        else:
            view = CreatewithtemplatePage(self.cog, self.user)
        await interaction.response.edit_message(view=view)

class SearchModal(discord.ui.Modal):
    def __init__(self, mode, parent_view):
        super().__init__(title=f"Search by {mode.title()}")
        self.mode = mode
        self.parent_view = parent_view
        self.input = discord.ui.TextInput(label="Query", required=True)
        self.add_item(self.input)

    async def on_submit(self, interaction: discord.Interaction):
        query = self.input.value.lower().strip()

        if self.mode == "id_direct":
            cog = self.parent_view.cog

            async with cog.acquire_db() as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("SELECT * FROM templates WHERE template_id = ?", (query,)) as cursor:
                    row = await cursor.fetchone()

            if not row:
                return await interaction.response.send_message(
                    f"Uh-oh! I couldn't find a template with the ID `{query}`.",
                    ephemeral=True
                )

            t = dict(row)

            draft = cog.template_to_draft(t, interaction.guild.id, interaction)
            embed = cog.create_giveaway_embed(draft)
            view = GiveawayPreviewView(cog, interaction.user, draft)
            expires = get_now_plus_seconds_unix(900)

            await interaction.response.send_message(
                content=f"Loaded template! This is a preview of your giveaway. Configure it using the buttons below, then start it.\nThis preview expires **<t:{expires}:R>**!",
                embed=embed,
                view=view
            )
            return

        if self.mode == "prize":
            self.parent_view.filtered_templates = [t for t in self.parent_view.templates if query in t['prize'].lower()]
            self.parent_view.container_header = f"Search results for '{query}'"
        else:
            self.parent_view.filtered_templates = [t for t in self.parent_view.templates if
                                                   query in t['template_id'].lower()]
            self.parent_view.container_header = f"Search results for '{query}'"
        self.parent_view.page = 1
        self.parent_view.total_pages = (
                                                   len(self.parent_view.filtered_templates) - 1) // self.parent_view.per_page + 1 if self.parent_view.filtered_templates else 1
        self.parent_view.build_layout()
        await interaction.response.edit_message(view=self.parent_view)


class CreatewithtemplatePage(PrivateLayoutView):
    def __init__(self, cog, user):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.user = user
        self.templates = []
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        container.add_item((discord.ui.TextDisplay("### Choose an option below to continue creating with template.")))
        container.add_item(discord.ui.Separator())

        id_btn = discord.ui.Button(label="Enter Template ID", style=discord.ButtonStyle.primary)
        id_btn.callback = self.enter_id_callback
        browse_btn = discord.ui.Button(label="Browse Templates", style=discord.ButtonStyle.primary)
        browse_btn.callback = self.browse_callback
        my_btn = discord.ui.Button(label="My Templates", style=discord.ButtonStyle.secondary)
        my_btn.callback = self.my_callback
        row = discord.ui.ActionRow()

        row.add_item(id_btn)
        row.add_item(browse_btn)
        row.add_item(my_btn)

        container.add_item(row)

        back_btn = discord.ui.Button(emoji=self.cog.bot.back_emoji, label="Back", style=discord.ButtonStyle.secondary)
        back_btn.callback = self.back_callback
        row = discord.ui.ActionRow()
        row.add_item(back_btn)
        container.add_item(discord.ui.Separator())
        container.add_item(row)

        self.add_item(container)

    async def enter_id_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            SearchModal("id_direct", self))

    async def browse_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        templates = await self.cog.fetch_templates(guild_id=interaction.guild.id, mode="browse")
        view = BrowsePage(self.cog, self.user, templates, interaction.guild.id)
        await interaction.edit_original_response(view=view)

    async def my_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        templates = await self.cog.fetch_templates(user_id=self.user.id, mode="mine")
        view = MystuffUse(self.cog, self.user, templates)
        await interaction.edit_original_response(view=view)

    async def back_callback(self, interaction: discord.Interaction):
        view = CreateChoose(self.cog, self.user)
        await interaction.response.edit_message(view=view)

class MystuffUse(PrivateLayoutView):
    def __init__(self, cog, user, templates, page=1):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.user = user
        self.templates = templates
        self.page = page
        self.per_page = 5
        self.total_pages = (len(self.templates) - 1) // self.per_page + 1 if self.templates else 1
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        container.add_item((discord.ui.TextDisplay("## My Templates")))
        container.add_item(discord.ui.Separator())

        start = (self.page - 1) * self.per_page
        end = start + self.per_page
        current = self.templates[start:end]

        for t in current:
            use_btn = discord.ui.Button(label="Use", style=discord.ButtonStyle.secondary)
            use_btn.callback = self.create_use_callback(t)

            desc = f"**Winners:** {t['winners']}\n**Duration:** {t['duration']}\n"
            container.add_item(
                discord.ui.Section(discord.ui.TextDisplay(f"### {t['prize']}\n{desc}"), accessory=use_btn))

        container.add_item(discord.ui.Separator())

        left_btn = discord.ui.Button(emoji="◀️", style=discord.ButtonStyle.primary, disabled=self.page == 1)
        left_btn.callback = self.prev_callback
        go_btn = discord.ui.Button(label=f"Page {self.page} of {self.total_pages}", style=discord.ButtonStyle.secondary,
                                   disabled=self.total_pages <= 1)
        go_btn.callback = self.goto_callback
        right_btn = discord.ui.Button(emoji="▶️", style=discord.ButtonStyle.primary,
                                      disabled=self.page == self.total_pages)
        right_btn.callback = self.next_callback

        row = discord.ui.ActionRow()
        row.add_item(left_btn)
        row.add_item(go_btn)
        row.add_item(right_btn)
        container.add_item(row)

        back_btn = discord.ui.Button(emoji=self.cog.bot.back_emoji, label="Back", style=discord.ButtonStyle.secondary)
        back_btn.callback = self.back_callback
        row = discord.ui.ActionRow()
        row.add_item(back_btn)
        container.add_item(discord.ui.Separator())
        container.add_item(row)
        self.add_item(container)

    def create_use_callback(self, t):
        async def callback(interaction: discord.Interaction):
            draft = self.cog.template_to_draft(t, interaction.guild.id, interaction)
            embed = self.cog.create_giveaway_embed(draft)
            view = GiveawayPreviewView(self.cog, self.user, draft)
            expires = get_now_plus_seconds_unix(900)
            await interaction.response.send_message(content=f"Loaded template! This is a preview of your giveaway. Configure it using the buttons below, then start it.\nThis preview expires **<t:{expires}:R>**!", embed=embed, view=view)

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
        view = CreatewithtemplatePage(self.cog, self.user)
        await interaction.response.edit_message(view=view)


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


class DestructiveConfirmationView(PrivateLayoutView):
    def __init__(self, user, template_id, cog, guild_id, prize_name):
        super().__init__(user, timeout=30)
        self.template_id = template_id
        self.cog = cog
        self.guild_id = guild_id
        self.color = None
        self.value = None
        self.title_text = "Delete Giveaway Template"
        self.body_text = f"Are you sure you want to delete template for **{prize_name}**? This cannot be undone."
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
        await self.cog.delete_template(self.template_id)

    async def on_timeout(self, interaction: discord.Interaction):
        if self.value is None:
            self.value = False
            await self.update_view(interaction, "Timed Out", discord.Color(0xdf5046))


class ConfirmationView(PrivateLayoutView):
    def __init__(self, user, template_id, cog, guild_id, prize_name):
        super().__init__(user, timeout=30)
        self.template_id = template_id
        self.cog = cog
        self.guild_id = guild_id
        self.color = None
        self.value = None
        self.title_text = "Publish Giveaway Template"
        self.body_text = f"Are you sure you want to **globally** publish template for **{prize_name}**? Anyone will be able to search and use your template."
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container(accent_color=self.color)
        container.add_item(discord.ui.TextDisplay(f"### {self.title_text}"))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(self.body_text))

        is_disabled = self.value is not None
        action_row = discord.ui.ActionRow()
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary, disabled=is_disabled)
        confirm = discord.ui.Button(label="Confirm", style=discord.ButtonStyle.green, disabled=is_disabled)

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
        await self.cog.set_publish_status(self.template_id, True, interaction)
        await interaction.followup.send("To protect the template repository from malicious actors, your template is being reviewed before it will be published. You will recieve a response DM from Dopamine within **24 hours**.")

    async def on_timeout(self, interaction: discord.Interaction):
        if self.value is None:
            self.value = False
            await self.update_view(interaction, "Timed Out", discord.Color(0xdf5046))


class ReviewView(discord.ui.View):
    def __init__(self, cog, template_id, creator_id):
        super().__init__(timeout=None)
        self.cog = cog
        self.template_id = template_id
        self.creator_id = creator_id

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.green, custom_id="review:accept")
    async def accept_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_review(interaction, self.template_id, self.creator_id, True)

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.red, custom_id="review:reject")
    async def reject_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RejectReasonModal(self.cog, self.template_id, self.creator_id))


class RejectReasonModal(discord.ui.Modal):
    def __init__(self, cog, template_id, creator_id):
        super().__init__(title="Rejection Reason")
        self.cog = cog
        self.template_id = template_id
        self.creator_id = creator_id
        self.reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.handle_review(interaction, self.template_id, self.creator_id, False, self.reason.value)


class DestructiveConfirmationViewOld(PrivateLayoutView):
    def __init__(self, title_text: str, body_text: str, color: discord.Color = None):
        super().__init__(None, timeout=30)
        self.value = None
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


class ConfirmationViewOld(PrivateLayoutView):
    def __init__(self, title_text: str, body_text: str, user: discord.User):
        super().__init__(user, timeout=30)
        self.value = None
        self.title_text = title_text
        self.body_text = body_text
        self.color = None
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
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary, disabled=is_disabled)
        confirm = discord.ui.Button(label="Confirm", style=discord.ButtonStyle.green, disabled=is_disabled)

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
        self.value = True
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


class Giveaways(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.giveaway_cache: Dict[int, dict] = {}
        self.participant_cache: Dict[int, Set[int]] = {}
        self.db_pool: Optional[asyncio.Queue[aiosqlite.Connection]] = None
        self.check_giveaways.start()

    async def cog_load(self):
        await self.init_pools()
        await self.init_db()
        await self.populate_caches()
        for giveaway_id in self.giveaway_cache:
            self.bot.add_view(GiveawayJoinView(self, giveaway_id))

    async def cog_unload(self):
        self.check_giveaways.cancel()

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
                    GDB_PATH,
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
            await self._migrate_winner_role_to_text(db)
            await db.execute('''
                CREATE TABLE IF NOT EXISTS giveaways (
                    guild_id INTEGER,
                    giveaway_id INTEGER,
                    channel_id INTEGER,
                    message_id INTEGER,
                    prize TEXT,
                    winners_count INTEGER,
                    end_time INTEGER,
                    host_id INTEGER,
                    required_roles TEXT,
                    req_behaviour INTEGER,
                    blacklisted_roles TEXT,
                    extra_entry_roles TEXT,
                    winner_role_id TEXT,
                    image_url TEXT,
                    thumbnail_url TEXT,
                    color TEXT,
                    ended INTEGER DEFAULT 0,
                    PRIMARY KEY (guild_id, giveaway_id)
                )
            ''')

            await db.execute('''
                CREATE TABLE IF NOT EXISTS giveaway_participants (
                    guild_id INTEGER,
                    giveaway_id INTEGER,
                    user_id INTEGER,
                    PRIMARY KEY (guild_id, giveaway_id, user_id)
                )
            ''')

            await db.execute('''
                CREATE TABLE IF NOT EXISTS giveaway_winners (
                    giveaway_id INTEGER,
                    user_id INTEGER,
                    PRIMARY KEY (giveaway_id, user_id)
                )
            ''')

            await db.execute('''
                CREATE TABLE IF NOT EXISTS templates (
                    template_id TEXT PRIMARY KEY,
                    creator_id INTEGER,
                    creation_guild_id INTEGER,
                    prize TEXT,
                    winners INTEGER,
                    duration TEXT,
                    channel_id INTEGER,
                    host_id INTEGER,
                    required_roles TEXT,
                    req_behaviour INTEGER,
                    blacklisted_roles TEXT,
                    extra_entries TEXT,
                    winner_role_id TEXT,
                    image TEXT,
                    thumbnail TEXT,
                    color TEXT,
                    usage_count INTEGER DEFAULT 0,
                    is_published INTEGER DEFAULT 0,
                    review_status TEXT DEFAULT 'none'
                )
            ''')

            await db.execute('''
                CREATE TABLE IF NOT EXISTS review_config (
                    guild_id INTEGER PRIMARY KEY,
                    channel_id INTEGER
                )
            ''')

            await db.commit()

            await self._run_migrations(db)

    async def _run_migrations(self, db: aiosqlite.Connection):
        """
        Automatically fixes data type mismatches in the database.
        """
        async with db.execute("SELECT giveaway_id, guild_id, winners_count FROM giveaways") as cursor:
            async for row in cursor:
                g_id, guild_id, w_count = row


                if isinstance(w_count, str):
                    try:
                        new_count = int(w_count)
                    except ValueError:
                        new_count = 1

                    await db.execute(
                        "UPDATE giveaways SET winners_count = ? WHERE giveaway_id = ? AND guild_id = ?",
                        (new_count, g_id, guild_id)
                    )
                    print(
                        f"[Migration] Fixed winners_count for giveaway {g_id} in guild {guild_id}: '{w_count}' -> {new_count}")

        async with db.execute("SELECT template_id, winners FROM templates") as cursor:
            async for row in cursor:
                t_id, w_count = row
                if isinstance(w_count, str):
                    try:
                        new_count = int(w_count)
                    except ValueError:
                        new_count = 1

                    await db.execute(
                        "UPDATE templates SET winners = ? WHERE template_id = ?",
                        (new_count, t_id)
                    )
                    print(f"[Migration] Fixed template winners for {t_id}: '{w_count}' -> {new_count}")

        await db.commit()

    async def _migrate_winner_role_to_text(self, db: aiosqlite.Connection):
        """
        Migrates the 'winner_role_id' column from INTEGER to TEXT to support
        multiple role IDs.
        """
        async with db.execute("PRAGMA table_info(giveaways)") as cursor:
            columns = await cursor.fetchall()
            winner_role_col = next((c for c in columns if c[1] == "winner_role_id"), None)

            if not winner_role_col or winner_role_col[2].upper() != "TEXT":
                return

        self.bot.logger.info("[Migration] Detected winner_role_id is not TEXT. Starting migration...")

        await db.execute('''
            CREATE TABLE giveaways_new (
                guild_id INTEGER,
                giveaway_id INTEGER,
                channel_id INTEGER,
                message_id INTEGER,
                prize TEXT,
                winners_count INTEGER,
                end_time INTEGER,
                host_id INTEGER,
                required_roles TEXT,
                req_behaviour INTEGER,
                blacklisted_roles TEXT,
                extra_entry_roles TEXT,
                winner_role_id TEXT,  -- This is now TEXT
                image_url TEXT,
                thumbnail_url TEXT,
                color TEXT,
                ended INTEGER DEFAULT 0,
                PRIMARY KEY (guild_id, giveaway_id)
            )
        ''')

        await db.execute('''
            INSERT INTO giveaways_new (
                guild_id, giveaway_id, channel_id, message_id, prize, 
                winners_count, end_time, host_id, required_roles, 
                req_behaviour, blacklisted_roles, extra_entry_roles, 
                winner_role_id, image_url, thumbnail_url, color, ended
            )
            SELECT 
                guild_id, giveaway_id, channel_id, message_id, prize, 
                winners_count, end_time, host_id, required_roles, 
                req_behaviour, blacklisted_roles, extra_entry_roles, 
                winner_role_id, image_url, thumbnail_url, color, ended
            FROM giveaways
        ''')

        await db.execute("DROP TABLE giveaways")
        await db.execute("ALTER TABLE giveaways_new RENAME TO giveaways")

        await db.commit()
        print("[Migration] winner_role_id successfully migrated to TEXT.")

    async def populate_caches(self):
        self.giveaway_cache.clear()
        self.participant_cache.clear()

        async with self.acquire_db() as db:
            async with db.execute("SELECT * FROM giveaways WHERE ended = 0") as cursor:
                rows = await cursor.fetchall()
                columns = [column[0] for column in cursor.description]
                for row in rows:
                    data = dict(zip(columns, row))
                    giveaway_id = data["giveaway_id"]
                    self.giveaway_cache[giveaway_id] = data
                    self.participant_cache[giveaway_id] = set()

            if self.giveaway_cache:
                placeholders = ", ".join(['?'] * len(self.giveaway_cache))
                query = f"SELECT giveaway_id, user_id FROM giveaway_participants WHERE giveaway_id in ({placeholders})"
                async with db.execute(query, list(self.giveaway_cache.keys())) as cursor:
                    participant_rows = await cursor.fetchall()
                    for giveaway_id, user_id in participant_rows:
                        if giveaway_id in self.participant_cache:
                            self.participant_cache[giveaway_id].add(user_id)

    @tasks.loop(seconds=10)
    async def check_giveaways(self):
        now = int(datetime.now(timezone.utc).timestamp())

        to_end = [
            (g['giveaway_id'], g['guild_id'])
            for g in self.giveaway_cache.values()
            if g['end_time'] <= now and g['ended'] == 0
        ]

        for giveaway_id, guild_id in to_end:
            await self.end_giveaway(giveaway_id, guild_id)

    async def end_giveaway(self, giveaway_id: int, guild_id: int):
        g = self.giveaway_cache.get(giveaway_id)
        if not g:
            async with self.acquire_db() as db:
                async with db.execute("SELECT * FROM giveaways WHERE giveaway_id = ? AND guild_id = ?",
                                      (giveaway_id, guild_id)) as cursor:
                    rows = await cursor.fetchall()
                    if not rows: return
                    columns = [column[0] for column in cursor.description]
                    g = dict(zip(columns, rows))
                    self.giveaway_cache[giveaway_id] = g
        if g.get('ended') == 1:
            return
        whichone = "giveaway_cache"
        await self.mark_as_ended(giveaway_id, guild_id, whichone)

        raw_participants = list(self.participant_cache.get(giveaway_id, set()))

        if not raw_participants:
            async with self.acquire_db() as db:
                async with db.execute("SELECT user_id FROM giveaway_participants WHERE giveaway_id = ?",
                                      (giveaway_id,)) as cursor:
                    rows = await cursor.fetchall()
                    raw_participants = [r[0] for r in rows]

        pool = []

        extra_roles_str = g.get('extra_entry_roles', '')
        extra_roles_list = [int(r) for r in extra_roles_str.split(',')] if extra_roles_str else []

        guild = self.bot.get_guild(guild_id) or await self.bot.fetch_guild(guild_id)

        for user_id in raw_participants:
            pool.append(user_id)

            if guild and extra_roles_list:
                member = guild.get_member(user_id) or await guild.fetch_member(user_id)
                if not member:
                    pass
                if member:
                    for role_id in extra_roles_list:
                        if any(role.id == role_id for role in member.roles):
                            pool.append(user_id)

        if not pool:
            channel = self.bot.get_channel(g['channel_id'])
            try:
                if channel is None:
                    channel = await self.bot.fetch_channel(g['channel_id'])
            except Exception as e:
                if is_access_error(e):
                    await report_access_failure(self.bot, guild_id, "giveaway", f"giveaway:{giveaway_id}")
                await self.mark_as_ended(giveaway_id, guild_id, 'participant_cache')
                return
            if channel:
                try:
                    await channel.send(embed=discord.Embed(
                        title="Giveaway Ended",
                        description=f"Giveaway for **{g['prize']}** ended with no participants.",
                        colour=discord.Colour.red(),
                    ))
                except Exception as e:
                    if is_access_error(e):
                        await report_access_failure(self.bot, guild_id, "giveaway", f"giveaway:{giveaway_id}")
            await self.mark_as_ended(giveaway_id, guild_id, 'participant_cache')
            return

        winner_count = min(len(set(pool)), g['winners_count'])
        random.shuffle(pool)

        winners = []
        for winner_id in pool:
            if winner_id not in winners:
                winners.append(winner_id)

            if len(winners) == winner_count:
                break
        winner_data = [(giveaway_id, winner_id) for winner_id in winners]

        if winner_data:
            async with self.acquire_db() as db:
                await db.executemany(
                    "INSERT INTO giveaway_winners (giveaway_id, user_id) VALUES (?, ?)",
                    winner_data
                )
                await db.commit()
        whichone = "participant_cache"
        await self.mark_as_ended(giveaway_id, guild_id, whichone)

        guild = self.bot.get_guild(guild_id) or await self.bot.fetch_guild(guild_id)
        channel = guild.get_channel(g['channel_id']) if guild else None
        if channel:
            try:
                msg = await channel.fetch_message(g['message_id'])
                embed_embed = self.create_embed_from_cache(g, winners=winners)
                await msg.edit(embed=embed_embed, view=None)

                mention_str = ", ".join([f"<@{w}>" for w in winners])
                await channel.send(f"🎉 Congratulations to: {mention_str} for winning **{g['prize']}!**")

                winner_role_ids = [int(r) for r in g['winner_role_id'].split(",")] if g['winner_role_id'] else []


                async def chunk_list(lst, n):
                    for i in range(0, len(lst), n):
                        yield lst[i:i + n]

                if winner_role_ids:
                    for role_id in winner_role_ids:
                        role = guild.get_role(role_id)

                        if role:
                            for chunk in chunk_list(winners, 5):
                                for member_id in chunk:
                                    member = guild.get_member(member_id) or await guild.fetch_member(member_id)
                                    if member:
                                        try:
                                            await member.add_roles(role, reason="Giveaway Winner")
                                        except discord.HTTPException:
                                            pass

                                await asyncio.sleep(1.5)

            except Exception as e:
                if is_access_error(e):
                    await report_access_failure(self.bot, guild_id, "giveaway", f"giveaway:{giveaway_id}")
                return

    async def mark_as_ended(self, giveaway_id: int, guild_id: int, whichone: str):
        if whichone == 'giveaway_cache':
            if giveaway_id in self.giveaway_cache:
                self.giveaway_cache[giveaway_id]['ended'] = 1
            async with self.acquire_db() as db:
                await db.execute("UPDATE giveaways SET ended = 1 WHERE giveaway_id = ? and guild_id = ?",
                                 (giveaway_id, guild_id))
                await db.commit()
        if whichone == 'participant_cache':
            if giveaway_id in self.participant_cache:
                self.participant_cache.pop(giveaway_id, None)

    def create_embed_from_cache(self, row, winners=None):
        end_ts = row['end_time']

        embed = discord.Embed(
            title="GIVEAWAY ENDED",
            description=f"Ended: **<t:{end_ts}:R>**",
            colour=discord.Colour.red()
        )
        embed.add_field(name="Winners", value=", ".join([f"<@{w}>" for w in winners]), inline=False)
        return embed

    def create_giveaway_embed(self, draft: GiveawayDraft, ended: bool = False, preview_active_end: int = None):
        title_text = "GIVEAWAY ENDED" if ended else f"{draft.prize}"
        embed_color = discord.Color.red() if ended else discord.Color(0x944ae8)

        if not ended and draft.color:
            color_str = draft.color.lower()
            try:
                if color_str.startswith("#"):
                    embed_color = discord.Color.from_str(color_str)
                elif hasattr(discord.Color, color_str):
                    embed_color = getattr(discord.Color, color_str)()
            except (ValueError, TypeError):
                pass

        time_display = f"Duration: **{draft.duration}**"
        if preview_active_end:
            time_display = f"Ends: **<t:{preview_active_end}:R>**"

        embed = discord.Embed(
            title=f"{title_text}",
            description=f"Click the 🎉 button below to enter this giveaway!\n\n"
                        f"Winners: **{draft.winners}**\n"
                        f"{time_display}",
            colour=embed_color
        )
        if draft.host_id:
            embed = discord.Embed(
                title=f"{draft.prize}",
                description=f"Click the 🎉 button below to enter this giveaway!\n\n"
                            f"Hosted By: <@{draft.host_id}>\n"
                            f"Winners: **{draft.winners}**\n"
                            f"{time_display}",
                colour=embed_color)

        if draft.winner_role:
            role_mentions = ", ".join([f"<@&{r}>" for r in draft.winner_role])
            embed.add_field(name="Winners' Roles", value=f"Winner(s) will receive the following role(s): {role_mentions}", inline=False)
        if draft.required_roles:
            role_mentions = ", ".join([f"<@&{r}>" for r in draft.required_roles])
            mode = "all of the following" if draft.required_behaviour == 0 else "one of the following"
            embed.add_field(name="Requirements", value=f"Must have **{mode}**: {role_mentions}", inline=False)

        if draft.extra_entries:
            role_mentions = ", ".join([f"<@&{e}>" for e in draft.extra_entries])
            embed.add_field(name="Extra Entry Roles (+1 Entry Per Role)", value=f"{role_mentions}", inline=False)
        if draft.image:
            embed.set_image(url=draft.image)
        if draft.thumbnail:
            embed.set_thumbnail(url=draft.thumbnail)

        embed.set_footer(text="ID: [Giveaway ID/Template ID]")

        return embed

    async def save_giveaway(self, draft: GiveawayDraft, message_id: int, giveaway_id: int, end_time: int):
        req_roles = ",".join(map(str, draft.required_roles)) if draft.required_roles else ""
        black_roles = ",".join(map(str, draft.blacklisted_roles)) if draft.blacklisted_roles else ""
        extra_roles = ",".join(map(str, draft.extra_entries)) if draft.extra_entries else ""
        winner_roles = ",".join(map(str, draft.winner_role)) if draft.winner_role else ""

        data = {
            "guild_id": draft.guild_id,
            "giveaway_id": giveaway_id,
            "channel_id": draft.channel_id,
            "message_id": message_id,
            "prize": draft.prize,
            "winners_count": draft.winners,
            "end_time": end_time,
            "host_id": draft.host_id,
            "required_roles": req_roles,
            "req_behaviour": draft.required_behaviour,
            "blacklisted_roles": black_roles,
            "extra_entry_roles": extra_roles,
            "winner_role_id": winner_roles,
            "image_url": draft.image,
            "thumbnail_url": draft.thumbnail,
            "color": draft.color,
            "ended": 0
        }

        self.giveaway_cache[giveaway_id] = data
        self.participant_cache[giveaway_id] = set()

        async with self.acquire_db() as db:
            placeholders = ", ".join(["?"] * len(data))
            columns = ", ".join(data.keys())
            await db.execute(f"INSERT INTO giveaways ({columns}) VALUES ({placeholders})",
                             tuple(data.values()))
            await db.commit()

    async def fetch_templates(self, guild_id: int = None, user_id: int = None, mode: str = "browse"):
        async with self.acquire_db() as db:
            if mode == "mine":
                query = "SELECT * FROM templates WHERE creator_id = ? ORDER BY usage_count DESC"
                args = (user_id,)
            else:
                query = "SELECT * FROM templates WHERE is_published = 1 OR creation_guild_id = ?"
                args = (guild_id,)

            db.row_factory = aiosqlite.Row
            async with db.execute(query, args) as cursor:
                rows = await cursor.fetchall()

            results = []
            for row in rows:
                t = dict(row)
                if mode == "browse":
                    creator = self.bot.get_user(t['creator_id']) or await self.bot.fetch_user(t['creator_id'])
                    t['creator_name'] = f"{creator.display_name} ({creator.name})" if creator else "Unknown"
                    guild = self.bot.get_guild(t['creation_guild_id']) or await self.bot.fetch_guild(t['guild_id'])
                    t['guild_name'] = guild.name if guild else "Unknown Guild"
                results.append(t)
            return results

    async def save_template(self, interaction: discord.Interaction, draft: GiveawayDraft):
        template_id = getattr(draft, 'template_id', None)
        if not template_id:
            template_id = generate_template_id()

        req_roles = ",".join(map(str, draft.required_roles)) if draft.required_roles else ""
        black_roles = ",".join(map(str, draft.blacklisted_roles)) if draft.blacklisted_roles else ""
        extra_roles = ",".join(map(str, draft.extra_entries)) if draft.extra_entries else ""
        winner_roles = ",".join(map(str, draft.winner_role)) if draft.winner_role else ""

        data = {
            "template_id": template_id,
            "creator_id": interaction.user.id,
            "creation_guild_id": interaction.guild.id,
            "prize": draft.prize,
            "winners": draft.winners,
            "duration": draft.duration,
            "channel_id": draft.channel_id,
            "host_id": draft.host_id,
            "required_roles": req_roles,
            "req_behaviour": draft.required_behaviour,
            "blacklisted_roles": black_roles,
            "extra_entries": extra_roles,
            "winner_role_id": winner_roles,
            "image": draft.image,
            "thumbnail": draft.thumbnail,
            "color": draft.color
        }

        async with self.acquire_db() as db:
            async with db.execute(
                    "SELECT usage_count, is_published, review_status FROM templates WHERE template_id = ?",
                    (template_id,)) as cursor:
                existing = await cursor.fetchone()

            if existing:
                data['usage_count'] = existing[0]
                data['is_published'] = existing[1]
                data['review_status'] = existing[2]

                if data['is_published'] == 1:
                    await self.notify_review_channel_edit(data, interaction.guild.id)

            columns = ", ".join(data.keys())
            placeholders = ", ".join(["?"] * len(data))

            await db.execute(f"INSERT OR REPLACE INTO templates ({columns}) VALUES ({placeholders})",
                             tuple(data.values()))
            await db.commit()

        return template_id

    async def delete_template(self, template_id: str):
        async with self.acquire_db() as db:
            await db.execute("DELETE FROM templates WHERE template_id = ?", (template_id,))
            await db.commit()

    async def increment_usage(self, template_id: str):
        async with self.acquire_db() as db:
            await db.execute("UPDATE templates SET usage_count = usage_count + 1 WHERE template_id = ?", (template_id,))
            await db.commit()

    def template_to_draft(self, t: dict, current_guild_id: int, interaction: discord.Interaction) -> GiveawayDraft:
        is_same = t['creation_guild_id'] == current_guild_id

        req = [int(x) for x in t['required_roles'].split(',')] if t['required_roles'] else []
        blk = [int(x) for x in t['blacklisted_roles'].split(',')] if t['blacklisted_roles'] else []
        ext = [int(x) for x in t['extra_entries'].split(',')] if t['extra_entries'] else []
        win = [int(x) for x in t['winner_role_id'].split(',')] if t['winner_role_id'] else []
        return GiveawayDraft(
            guild_id=current_guild_id,
            channel_id=t['channel_id'] if is_same else interaction.channel.id,
            prize=t['prize'],
            winners=t['winners'],
            duration=t['duration'],
            host_id=t['host_id'] if is_same else None,
            required_roles=req if is_same else [],
            required_behaviour=t['req_behaviour'],
            blacklisted_roles=blk if is_same else [],
            extra_entries=ext if is_same else [],
            winner_role=win if is_same else [],
            image=t['image'],
            thumbnail=t['thumbnail'],
            color=t['color']
        )

    async def set_publish_status(self, template_id: str, publish: bool, interaction: discord.Interaction):
        status = 1 if publish else 0
        review = "pending" if publish else "none"

        async with self.acquire_db() as db:
            await db.execute("UPDATE templates SET is_published = ?, review_status = ? WHERE template_id = ?",
                             (status, review, template_id))
            await db.commit()

        if publish:
            await self.send_to_review(template_id, interaction)
            await interaction.response.send_message("Template submitted for review!", ephemeral=True)
        else:
            await interaction.response.send_message("Template unpublished.", ephemeral=True)

    async def send_to_review(self, template_id, interaction):
        async with self.acquire_db() as db:
            async with db.execute("SELECT channel_id FROM review_config LIMIT 1") as cursor:
                row = await cursor.fetchone()
                if not row: return
                channel_id = row[0]

        channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
        if not channel: return

        async with self.acquire_db() as db:
            async with db.execute("SELECT * FROM templates WHERE template_id = ?", (template_id,)) as cursor:
                row = await cursor.fetchone()
                if not row: return
                t = dict(zip([c[0] for c in cursor.description], row))

        creator = interaction.user
        embed = discord.Embed(title="Template Review Request", color=discord.Color.gold())
        embed.add_field(name="ID", value=t['template_id'])
        embed.add_field(name="Prize", value=t['prize'])
        embed.add_field(name="Creator", value=f"{creator.name} ({creator.id})")
        embed.add_field(name="Guild", value=f"{interaction.guild.name} ({interaction.guild.id})")

        view = ReviewView(self, template_id, creator.id)
        await channel.send(embed=embed, view=view)

    async def notify_review_channel_edit(self, t, guild_id):
        async with self.acquire_db() as db:
            async with db.execute("SELECT channel_id FROM review_config LIMIT 1") as cursor:
                row = await cursor.fetchone()
                if not row: return
                channel_id = row[0]
        channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
        if channel:
            await channel.send(f"⚠️ **Template Edited:** {t['template_id']} ({t['prize']}) was edited by its creator.")

    async def handle_review(self, interaction, template_id, creator_id, approved, reason=None):
        status = "approved!" if approved else "rejected."
        async with self.acquire_db() as db:
            await db.execute("UPDATE templates SET review_status = ? WHERE template_id = ?", (status, template_id))
            if not approved:
                await db.execute("UPDATE templates SET is_published = 0 WHERE template_id = ?", (template_id,))
            await db.commit()

        user = self.bot.get_user(creator_id) or await self.bot.fetch_user(creator_id)
        if user:
            embed = discord.Embed(title=f"Template {status.title()}",
                                  color=discord.Color.green() if approved else discord.Color.red())
            embed.description = f"Your template (ID: **{template_id}**) has been {status}"
            if not approved:
                embed.add_field(name="Reason", value=reason or "No reason provided")
            try:
                await user.send(embed=embed)
            except:
                pass

        await interaction.response.edit_message(content=f"Template {status} by {interaction.user.name}", view=None)

    async def giveaway_autocomplete(self, interaction: discord.Interaction, current: str, magic: bool = False):
        choices = []

        if magic:
            data_source = sorted(self.giveaway_cache.items())
        else:
            async with self.acquire_db() as db:
                async with db.execute("SELECT giveaway_id, prize FROM giveaways WHERE guild_id = ?",
                                      (interaction.guild_id,)) as cursor:
                    rows = await cursor.fetchall()
                    data_source = [(row[0], {"prize": row[1]}) for row in rows]

        for i, (giveaway_id, data) in enumerate(data_source, 1):
            label = f"{i}. {data['prize']}: {giveaway_id}"
            if current.lower() in label.lower():
                choices.append(app_commands.Choice(name=label, value=str(giveaway_id)))

        return choices[:25]

    giveaway = beacon_commands.Group(name="giveaway", description="Commands for Dopamine's giveaway features.", permissions_preset="giveaways")

    @giveaway.command(name="create", description="Start the giveaway creation process.")
    async def create(self, interaction: discord.Interaction):
        view = CreateChoose(self, interaction.user)
        await interaction.response.send_message(view=view)

    @giveaway.command(name="template", description="Open the Giveaway Template Homepage.")
    async def template_cmd(self, interaction: discord.Interaction):
        view = TemplateHomepage(self, interaction.user)
        await interaction.response.send_message(view=view)

    @beacon_commands.command(name="zr", description=".", permissions_preset="bot_owner")
    async def set_review_channel(self, interaction: discord.Interaction):
        async with self.acquire_db() as db:
            await db.execute("INSERT OR REPLACE INTO review_config (guild_id, channel_id) VALUES (?, ?)",
                             (interaction.guild.id, interaction.channel.id))
            await db.commit()
        await interaction.response.send_message(f"Set {interaction.channel.mention} as the review channel.",
                                                ephemeral=True)

    @giveaway.command(name="end", description="End an active giveaway (winners are also picked and mentioned).")
    @app_commands.describe(giveaway_id="The ID of the giveaway to end.")
    async def giveaway_end(self, interaction: discord.Interaction, giveaway_id: str):
        try:
            giveaway_id = int(giveaway_id)
        except ValueError:
            return await interaction.response.send_message("That is not a valid ID!", ephemeral=True)

        if giveaway_id not in self.giveaway_cache:
            return await interaction.response.send_message("That giveaway is not active or doesn't exist!",
                                                           ephemeral=True)

        body_content = f"Are you sure you want to end this giveaway right now and announce the winners?"
        view = ConfirmationViewOld("Pending Confirmation", body_content, interaction.user)
        await interaction.response.send_message(view=view)
        response = await interaction.original_response()
        view.message = response
        await view.wait()

        if view.value is True:
            await self.end_giveaway(giveaway_id, interaction.guild_id)

    @giveaway_end.autocomplete("giveaway_id")
    async def end_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.giveaway_autocomplete(interaction, current, magic=True)

    @giveaway.command(name="delete", description="Delete a giveaway permanently from the database.")
    @app_commands.describe(giveaway_id="The ID of the giveaway to delete.")
    async def giveaway_delete(self, interaction: discord.Interaction, giveaway_id: str):
        try:
            giveaway_id = int(giveaway_id)
        except ValueError:
            return await interaction.response.send_message("That is not a valid ID!", ephemeral=True)

        async with self.acquire_db() as db:
            async with db.execute("SELECT prize FROM giveaways WHERE giveaway_id = ? and guild_id = ?",
                                  (giveaway_id, interaction.guild.id,)) as cursor:
                row = await cursor.fetchone()
                prize = row[0] if row else "Unknown Prize"
            async with db.execute(
                    "SELECT channel_id, message_id, prize FROM giveaways WHERE giveaway_id = ? AND guild_id = ?",
                    (giveaway_id, interaction.guild.id,)) as cursor:
                row = await cursor.fetchone()

            if not row:
                return await interaction.response.send_message("Giveaway not found.", ephemeral=True)

            body_content = f"Are you sure you want to delete the giveaway for **{prize}** (ID: {giveaway_id}) permanently?"
            view = DestructiveConfirmationViewOld("Pending Confirmation", body_content)
            response = await interaction.response.send_message(view=view)
            view.message = await interaction.original_response()
            await view.wait()

            if view.value is True:
                async with self.acquire_db() as db:
                    await db.execute("DELETE FROM giveaways WHERE giveaway_id = ?", (giveaway_id,))
                    await db.execute("DELETE FROM giveaway_participants WHERE giveaway_id = ?", (giveaway_id,))
                    await db.execute("DELETE FROM giveaway_winners WHERE giveaway_id = ?", (giveaway_id,))
                    await db.commit()
                    try:
                        self.giveaway_cache.pop(giveaway_id)
                    except Exception:
                        pass

    @giveaway_delete.autocomplete("giveaway_id")
    async def delete_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.giveaway_autocomplete(interaction, current, magic=False)

    @giveaway.command(name="reroll", description="Reroll a giveaway.")
    @app_commands.describe(giveaway_id="The ID of the giveaway to reroll.", winners="Number of new winners to pick",
                           preserve_winners="Keep previous winners and just add new ones?")
    async def giveaway_reroll(self, interaction: discord.Interaction, giveaway_id: int, winners: int = 1,
                              preserve_winners: bool = False):
        try:
            giveaway_id = int(giveaway_id)
        except ValueError:
            return await interaction.response.send_message("That is not a valid ID!", ephemeral=True)

        async with self.acquire_db() as db:
            async with db.execute(
                    "SELECT prize, winner_role_id, channel_id, ended FROM giveaways WHERE giveaway_id = ?",
                    (giveaway_id,)) as cursor:
                g = await cursor.fetchone()

            if not g:
                return await interaction.response.send_message("Giveaway data not found.", ephemeral=True)

            if g[3] == 0:
                return await interaction.response.send_message(
                    "This giveaway hasn't ended yet! You can't reroll active giveaways.")
        line2 = None
        if preserve_winners and g[1]:
            line2 = "Preserve old winners and their winner roles"
        elif preserve_winners and not g[1]:
            line2 = "Preserve old winners"
        elif not preserve_winners and g[1]:
            line2 = f"Override **{winners}** old winners and remove their winner roles"
        elif not preserve_winners and not g[1]:
            line2 = f"Override **{winners}** old winners"
        body_content = (f"Are you sure you want to:\n"
                        f"* Re-roll this giveaway to pick **{winners}** new winners\n"
                        f"* {line2}\n"
                        f"{f'* Give **{winners}** the winner role' if g[1] else ''}")

        view = ConfirmationViewOld("Pending Confirmation", body_content, interaction.user)
        response = await interaction.response.send_message(view=view)
        view.message = await interaction.original_response()
        await view.wait()

        if view.value is True:
            async def chunk_list(self, lst, n):
                for i in range(0, len(lst), n):
                    yield lst[i:i + n]

            async with self.acquire_db() as db:
                async with db.execute(
                        "SELECT user_id FROM giveaway_participants WHERE giveaway_id = ? AND guild_id = ?",
                        (giveaway_id, interaction.guild_id,)) as cursor:
                    rows = await cursor.fetchall()

                    pool = [r[0] for r in rows]

                async with db.execute("SELECT user_id FROM giveaway_winners WHERE giveaway_id = ?",
                                      (giveaway_id,)) as cursor:
                    prev_rows = await cursor.fetchall()
                    if not prev_rows:
                        return await interaction.followup.send_response("This giveaway hasn't ended yet!",
                                                                        ephemeral=True)
                    prev_winners = [r[0] for r in prev_rows]

                eligible_pool = [uid for uid in pool if uid not in prev_winners]

                if not eligible_pool:
                    return await interaction.followup.send_response("No new participants available to pick from!",
                                                                    ephemeral=True)

                new_picks = random.sample(eligible_pool, min(len(eligible_pool), winners))

                if not preserve_winners:
                    if g[1]:
                        role = interaction.guild.get_role(g[1])
                        if not role:
                            role = await interaction.guild.fetch_role(g[1])

                        if role:
                            for chunk in chunk_list(self, prev_winners, 5):
                                for old_uid in chunk:
                                    member = interaction.guild.get_member(
                                        old_uid) or await interaction.guild.fetch_member(old_uid)
                                    if member and role in member.roles:
                                        try:
                                            await member.remove_roles(role, reason="Giveaway Reroll")
                                        except discord.HTTPException:
                                            pass
                                await asyncio.sleep(1.5)

                    await db.execute("DELETE FROM giveaway_winners WHERE giveaway_id = ?", (giveaway_id,))

                for new_uid in new_picks:
                    await db.execute("INSERT INTO giveaway_winners (giveaway_id, user_id) VALUES (?, ?)",
                                     (giveaway_id, new_uid))
                    if g[1]:
                        role = interaction.guild.get_role(g[1])
                        if not role:
                            role = interaction.guild.fetch_role(g[1])
                        if not role:
                            await interaction.followup_send("I can't find the role to give to the winners!",
                                                            ephemeral=True)
                        if role:
                            for chunk in chunk_list(new_picks, 5):
                                for new_uid in chunk:
                                    member = interaction.guild.get_member(
                                        new_uid) or await interaction.guild.fetch_member(new_uid)
                                    if member:
                                        try:
                                            await member.add_roles(role, reason="Giveaway Winner")
                                        except discord.HTTPException:
                                            pass

                                await asyncio.sleep(1.5)

                await db.commit()

                channel = self.bot.get_channel(g[2]) or await self.bot.fetch_channel(g[2])
                if not channel:
                    return await interaction.response.followup_send(
                        "I searched far and wide, but I can't find the channel chosen for the giveaway!\n\nEnsure that I have the necessary permissions so that I can announce the new winners.",
                        ephemeral=True)

                mention_str = ", ".join([f"<@{w}>" for w in new_picks])
                mode_text = "added to the pool of winners" if preserve_winners else "selected as the new winners"
                await channel.send(
                    f"🎉 Congratulations to: {mention_str} for being {mode_text} for **{g[0]}**!\nThis giveaway has been re-rolled by {interaction.user.mention}.")

    @giveaway_reroll.autocomplete("giveaway_id")
    async def reroll_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.giveaway_autocomplete(interaction, current, magic=False)

    @giveaway.command(name="list", description="List all giveaways in this server.")
    async def giveaway_list(self, interaction: discord.Interaction):
        await interaction.response.defer()
        async with self.acquire_db() as db:
            async with db.execute(
                    "SELECT prize, ended, end_time, giveaway_id FROM giveaways WHERE guild_id = ? ORDER BY giveaway_id ASC",
                    (interaction.guild.id,)) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            return await interaction.edit_original_response("No giveaways found for this server.")

        lines = []
        for i, (prize, ended, end_time, giveaway_id) in enumerate(rows, 1):
            status = "Ended" if ended == 1 else f"Ends **<t:{end_time}:R>**"
            lines.append(f"{i}. **{prize}**: {status} (`{giveaway_id}`)")

        full_list = "\n".join(lines)
        if len(full_list) > 1900:
            full_list = full_list[:1900] + "\n...and more."

        embed = discord.Embed(
            title=f"All Giveaways for {interaction.guild.name}",
            description=full_list,
            color=discord.Color(0x944ae8)
        )
        await interaction.edit_original_response(embed=embed)

    def data_features(self) -> list[DataFeatureMeta]:
        return [DataFeatureMeta(
            feature_id="giveaway",
            name="Giveaways",
            user_export=True,
            user_delete=True,
            guild_export=True,
            guild_delete=True,
        )]

    async def data_export_user(self, user_id: int, *, guild_ids: list[int] | None) -> DataExportChunk:
        chunk = DataExportChunk(feature_id="giveaway")
        async with self.acquire_db() as db:
            if guild_ids is None:
                participants = await export_table(
                    db, "SELECT * FROM giveaway_participants WHERE user_id = ?", (user_id,))
                winners = await export_table(
                    db,
                    """SELECT w.*, g.guild_id FROM giveaway_winners w
                       JOIN giveaways g ON g.giveaway_id = w.giveaway_id
                       WHERE w.user_id = ?""",
                    (user_id,),
                )
                hosted = await export_table(
                    db, "SELECT * FROM giveaways WHERE host_id = ?", (user_id,))
            else:
                placeholders = ",".join("?" * len(guild_ids))
                params = (user_id, *guild_ids)
                participants = await export_table(
                    db,
                    f"SELECT * FROM giveaway_participants WHERE user_id = ? AND guild_id IN ({placeholders})",
                    params,
                )
                winners = await export_table(
                    db,
                    f"""SELECT w.*, g.guild_id FROM giveaway_winners w
                        JOIN giveaways g ON g.giveaway_id = w.giveaway_id
                        WHERE w.user_id = ? AND g.guild_id IN ({placeholders})""",
                    params,
                )
                hosted = await export_table(
                    db,
                    f"SELECT * FROM giveaways WHERE host_id = ? AND guild_id IN ({placeholders})",
                    params,
                )
        for row in participants:
            gid = row.pop("guild_id")
            chunk.guild_data.setdefault(gid, {}).setdefault("participations", []).append(row)
        for row in winners:
            gid = row.pop("guild_id")
            chunk.guild_data.setdefault(gid, {}).setdefault("wins", []).append(row)
        for row in hosted:
            gid = row.pop("guild_id")
            chunk.guild_data.setdefault(gid, {}).setdefault("hosted_giveaways", []).append(row)
        return chunk

    async def data_export_guild(self, guild_id: int) -> DataExportChunk:
        chunk = DataExportChunk(feature_id="giveaway")
        async with self.acquire_db() as db:
            giveaways = await export_table(db, "SELECT * FROM giveaways WHERE guild_id = ?", (guild_id,))
            participants = await export_table(
                db, "SELECT * FROM giveaway_participants WHERE guild_id = ?", (guild_id,))
            winners = await export_table(
                db,
                """SELECT w.* FROM giveaway_winners w
                   JOIN giveaways g ON g.giveaway_id = w.giveaway_id
                   WHERE g.guild_id = ?""",
                (guild_id,),
            )
            templates = await export_table(
                db, "SELECT * FROM templates WHERE creation_guild_id = ?", (guild_id,))
            review = await export_table(db, "SELECT * FROM review_config WHERE guild_id = ?", (guild_id,))
        chunk.guild_data[guild_id] = {
            "giveaways": giveaways,
            "participants": participants,
            "winners": winners,
            "templates": templates,
            "review_config": review,
        }
        return chunk

    async def data_delete_user(self, user_id: int, *, guild_ids: list[int] | None, feature_id: str | None) -> DataDeleteResult:
        if feature_id and feature_id != "giveaway":
            return DataDeleteResult(feature_id="giveaway")
        rows_affected = 0
        async with self.acquire_db() as db:
            if guild_ids is None:
                cur = await db.execute("DELETE FROM giveaway_participants WHERE user_id = ?", (user_id,))
                rows_affected += cur.rowcount
                cur = await db.execute("DELETE FROM giveaway_winners WHERE user_id = ?", (user_id,))
                rows_affected += cur.rowcount
            else:
                placeholders = ",".join("?" * len(guild_ids))
                params = (user_id, *guild_ids)
                cur = await db.execute(
                    f"DELETE FROM giveaway_participants WHERE user_id = ? AND guild_id IN ({placeholders})",
                    params,
                )
                rows_affected += cur.rowcount
                cur = await db.execute(
                    f"""DELETE FROM giveaway_winners WHERE user_id = ? AND giveaway_id IN (
                        SELECT giveaway_id FROM giveaways WHERE guild_id IN ({placeholders}))""",
                    (user_id, *guild_ids),
                )
                rows_affected += cur.rowcount
            await db.commit()
        for giveaway_id, participants in list(self.participant_cache.items()):
            if user_id in participants:
                participants.discard(user_id)
        return DataDeleteResult(feature_id="giveaway", deleted=True, rows_affected=rows_affected)

    async def data_delete_guild(self, guild_id: int, feature_id: str | None) -> DataDeleteResult:
        if feature_id and feature_id != "giveaway":
            return DataDeleteResult(feature_id="giveaway")
        rows_affected = 0
        giveaway_ids = []
        async with self.acquire_db() as db:
            async with db.execute("SELECT giveaway_id FROM giveaways WHERE guild_id = ?", (guild_id,)) as cur:
                giveaway_ids = [r[0] async for r in cur]
            if giveaway_ids:
                placeholders = ",".join("?" * len(giveaway_ids))
                cur = await db.execute(
                    f"DELETE FROM giveaway_winners WHERE giveaway_id IN ({placeholders})", giveaway_ids)
                rows_affected += cur.rowcount
            cur = await db.execute("DELETE FROM giveaway_participants WHERE guild_id = ?", (guild_id,))
            rows_affected += cur.rowcount
            cur = await db.execute("DELETE FROM giveaways WHERE guild_id = ?", (guild_id,))
            rows_affected += cur.rowcount
            cur = await db.execute("DELETE FROM templates WHERE creation_guild_id = ?", (guild_id,))
            rows_affected += cur.rowcount
            cur = await db.execute("DELETE FROM review_config WHERE guild_id = ?", (guild_id,))
            rows_affected += cur.rowcount
            await db.commit()
        for gid in giveaway_ids:
            self.giveaway_cache.pop(gid, None)
            self.participant_cache.pop(gid, None)
        return DataDeleteResult(feature_id="giveaway", deleted=True, rows_affected=rows_affected)

    async def data_monitor_guild(self, guild: discord.Guild) -> DataMonitorResult:
        result = DataMonitorResult(feature_id="giveaway")
        async with self.acquire_db() as db:
            async with db.execute(
                "SELECT giveaway_id, channel_id FROM giveaways WHERE guild_id = ? AND ended = 0",
                (guild.id,),
            ) as cursor:
                rows = await cursor.fetchall()
        for giveaway_id, channel_id in rows:
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
                and channel.permissions_for(guild.me).read_message_history
            )
            if not accessible:
                await self.end_giveaway(giveaway_id, guild.id)
                result.actions.append(f"ended_giveaway:{giveaway_id}")
        return result


async def setup(bot):
    await bot.add_cog(Giveaways(bot))