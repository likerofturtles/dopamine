from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.machinery import all_suffixes
from typing import Optional, List, Dict, Set
import discord
from discord import app_commands, Interaction
import re
from discord.ext import commands, tasks
import random
import asyncio
import aiosqlite
from datetime import datetime, timezone

from config import GDB_PATH
from utils.time import get_duration_to_seconds, get_now_plus_seconds_unix

@dataclass
class GiveawayDraft:
    guild_id: int
    channel_id: int
    prize: str
    winners: int
    end_time: int # Unix timestamp
    host_id: Optional[int] = None
    required_roles: List[int] = None
    required_behaviour: int = 0 # 0 = All, 1 = One
    blacklisted_roles: List[int] = None
    extra_entries: List[int] = None
    winner_role: Optional[int] = None
    image: Optional[str] = None
    thumbnail: Optional[str] = None
    color: str = "discord.Color.blue()"

class GiveawayEditSelect(discord.ui.Select):
    def __init__(self, cog, draft: GiveawayDraft, parent_view):
        self.cog = cog
        self.draft = draft
        self.parent_view = parent_view
        options = [
            discord.SelectOption(label="1. Prize", value="prize", description="Change the prize being given away."),
            discord.SelectOption(label="2. Duration", value="duration", description="Change how long the giveaway lasts (e.g., 1h, 2d)."),
            discord.SelectOption(label="3. Winners Count", value="winners", description="Change the number of winners."),
            discord.SelectOption(label="4. Channel", value="channel", description="Change where the giveaway is posted."),
            discord.SelectOption(label="5. Giveaway Host", value="host", description="The host name to be shown in the giveaway Embed."),
            discord.SelectOption(label="6. Extra Entries Role", value="extra", description="Roles that will give extra entries. Each role gives +1 entries."),
            discord.SelectOption(label="7. Required Roles", value="required", description="Roles required to participate."),
            discord.SelectOption(label="8. Required Roles Behaviour", value="behavior", description="The behavior of the required roles feature."),
            discord.SelectOption(label="9. Winner Role", value="winner_role", description="Role given to winners."),
            discord.SelectOption(label="10. Blacklisted Roles", value="blacklist", description="Roles that cannot participate."),
            discord.SelectOption(label="11. Image", value="image", description="Provide a valid URL for the Embed image."),
            discord.SelectOption(label="12. Thumbnail", value="thumbnail", description="Provide a valid URL for the Embed thumbnail."),
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
            msg = "Choose role to be given to winner(s):"

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
            current_value = ""

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
            from utils.time import get_duration_to_seconds, get_now_plus_seconds_unix
            seconds = get_duration_to_seconds(value)
            if seconds <= 0:
                return await interaction.response.send_message("Invalid duration format!", ephemeral=True)
            self.draft.end_time = get_now_plus_seconds_unix(seconds)

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
        await interaction.response.send_message(f"Target channel updated to {self.select.values[0].mention}", ephemeral=True)

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

class GoToPageModal(discord.ui.Modal):
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
                await interaction.response.send_message(f"Please enter a number between 1 and {self.max_pages}.", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("Invalid number entered.", ephemeral=True)


class ParticipantPaginator(discord.ui.View):
    def __init__(self, bot, participants: list, prize: str, extra_roles: list, guild: discord.Guild):
        super().__init__(timeout=120)
        self.bot = bot
        self.prize = prize
        self.guild = guild
        self.current_page = 0
        self.per_page = 10
        self.show_tags = False

        self.processed_participants = self._process_participants(participants, extra_roles)

    def _process_participants(self, participants, extra_roles):
        data = []
        for uid in participants:
            entries = 1
            member = self.guild.get_member(uid)
            if member and extra_roles:
                for role_id in extra_roles:
                    if any(r.id == role_id for r in member.roles):
                        entries += 1
            data.append({'id': uid, 'entries': entries})

        return sorted(data, key=lambda x: (x['entries'], x['id']), reverse=True)

    def get_embed(self):
        start = self.current_page * self.per_page
        end = start + self.per_page
        page_list = self.processed_participants[start:end]

        lines = []
        for item in page_list:
            user = self.bot.get_user(item['id'])
            if user:
                name = user.name if self.show_tags else f"<@{user.id}>"
            else:
                name = f"Unknown({item['id']})"

            lines.append(f"• **{name}** (**{item['entries']}** entries)")

        mentions = "\n".join(lines) or "No participants yet."
        total_pages = (len(self.processed_participants) - 1) // self.per_page + 1
        total_count = len(self.processed_participants)

        embed = discord.Embed(
            title=f"<:dopamine:1445805701355012279> Participants for **{self.prize}**",
            description=mentions,
            color=discord.Color.blue()
        )
        embed.set_footer(text=f"Total Participants: {total_count} | Page {self.current_page + 1} of {total_pages}")
        return embed

    @discord.ui.button(label="◀️", style=discord.ButtonStyle.gray)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
            await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="Go To Page", style=discord.ButtonStyle.gray)
    async def go_to_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        total_pages = (len(self.processed_participants) - 1) // self.per_page + 1
        await interaction.response.send_modal(GoToPageModal(self.current_page, total_pages, self))

    @discord.ui.button(label="▶️", style=discord.ButtonStyle.gray)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if (self.current_page + 1) * self.per_page < len(self.processed_participants):
            self.current_page += 1
            await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="Show User Tags", style=discord.ButtonStyle.blurple)
    async def toggle_tags(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.show_tags = not self.show_tags
        button.label = "Show Usernames" if self.show_tags else "Show User Tags"
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

class BehaviorSelect(discord.ui.Select):
    def __init__(self, draft: GiveawayDraft, parent_view):
        options = [
            discord.SelectOption(label="All required roles", value="0", description="Participant must have every role listed."),
            discord.SelectOption(label="One of the required roles", value="1", description="Participant must have at least one role listed.")
        ]

        super().__init__(placeholder="Choose role requirement behaviour...", options=options)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        self.draft.required_behaviour = int(self.values[0])
        new_embed = self.parent_view.cog.create_giveaway_embed(self.draft)
        await self.parent_view.message.edit(embed=new_embed)
        await interaction.response.send_message("Role requirement behaviour updated successfully!", ephemeral=True)

class GiveawayPreviewView(discord.ui.View):
    def __init__(self, cog, draft: GiveawayDraft):
        super().__init__(timeout=900)
        self.cog = cog
        self.draft = draft
        self.message = Optional[discord.InteractionMessage]

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(title="Giveaway preview expired", descrption="This giveaway preview has expired.", view=None, colour=discord.Colour.red())
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=discord.Embed(title="Giveaway Creation Cancelled."), view=None)
        self.stop()

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.gray)
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.message = interaction.message
        view = discord.ui.View()
        select = GiveawayEditSelect(cog=self.cog, draft=self.draft, parent_view=self)
        view.add_item(select)

        await interaction.response.send_message(
            embed=discord.Embed(title="Edit Giveaway", description="Select a setting...", color=discord.Color.blue()),
            view=view,
            ephemeral=True
        )
        self.message = interaction.message

    @discord.ui.button(label="Start", style=discord.ButtonStyle.green)
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        channel = self.cog.bot.get_channel(self.draft.channel_id)
        if not channel:
            try:
                channel = await self.cog.bot.fetch_channel(self.draft.channel_id)
            except (discord.Forbidden, discord.NotFound):
                return await interaction.response.send_message(
                    "I searched far and wide, but I can't find the channel chosen for the giveaway!\n\nEnsure that I have the necessary permissions.",
                    ephemeral=True)

        giveaway_id = int(discord.utils.utcnow().timestamp()) + random.randint(1, 69)
        view = GiveawayJoinView(self.cog, giveaway_id)
        embed = self.cog.create_giveaway_embed(self.draft)
        embed.set_footer(text=f"ID: {giveaway_id}")
        msg = await channel.send(embed=embed, view=view)
        await self.cog.save_giveaway(self.draft, msg.id, giveaway_id)
        self.cog.bot.add_view(view)
        success_embed = discord.Embed(description=f"Giveaway started successfully in {channel.mention}!",
                                      colour=discord.Colour.green())
        embed.set_footer(text=f"ID: {giveaway_id}")
        await interaction.response.send_message(embed=success_embed, ephemeral=True)
        await interaction.message.delete()
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
        await interaction.response.send_message(f"Giveaway host updated to {self.select.values[0].mention}", ephemeral=True)


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
        if self.key == "extra":
            self.draft.extra_entries = role_ids
        elif self.key == "required":
            self.draft.required_roles = role_ids
        elif self.key == "blacklist":
            self.draft.blacklisted_roles = role_ids
        elif self.key == "winner_role":
            self.draft.winner_role = role_ids[0]

        new_embed = self.parent_view.cog.create_giveaway_embed(self.draft)
        await self.parent_view.message.edit(embed=new_embed)

        await interaction.response.send_message(f"Updated {self.key} successfully!", ephemeral=True)


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
        if self.key == "extra":
            self.draft.extra_entries = role_ids
        elif self.key == "required":
            self.draft.required_roles = role_ids
        elif self.key == "blacklist":
            self.draft.blacklisted_roles = role_ids
        elif self.key == "winner_role":
            self.draft.winner_role = role_ids[0]
        new_embed = self.parent_view.cog.create_giveaway_embed(self.draft)
        await self.parent_view.message.edit(embed=new_embed)
        await interaction.response.send_message(f"Updated {self.key} successfully!", ephemeral=True)



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
            return await interaction.response.send_message("Uh-oh! I'm afraid that the message you interacted with doesn't exist anymore :3", ephemeral=True)

        footer_text = interaction.message.embeds[0].footer.text
        try:
            giveaway_id = int(footer_text.split(": ")[1])
        except (IndexError, ValueError):
            return await interaction.response.send_message("Uh-oh! I couldn't find the Giveaway ID. Perhaps try again?", ephemeral=True)

        g = self.cog.giveaway_cache.get(giveaway_id)

        if not g or g['ended'] == 1:
            return await interaction.response.send_message("Uh-oh! I'm afraid that this giveaway has already ended!", ephemeral=True)

        if g['blacklisted_roles']:
            blacklisted_ids = [int(r) for r in g['blacklisted_roles'].split(",")]
            if any(role.id in blacklisted_ids for role in interaction.user.roles):
                return await interaction.response.send_message("Uh-oh! You cannot join this giveaway because you have a blacklisted role.", ephemeral=True)

        if g['required_roles']:
            req_ids = [int(r) for r in g['required_roles'].split(",")]
            user_role_ids = [role.id for role in interaction.user.roles]

            if g['req_behaviour'] == 0:
                if not all(r in user_role_ids for r in req_ids):
                    return await interaction.response.send_message("Uh-oh! You cannot join this giveaway because you don't have all the required roles.", ephemeral=True)

            else:
                if not any(r in user_role_ids for r in req_ids):
                    return await interaction.response.send_message("Uh-oh! You cannot join this giveaway because you don't have one of the required roles.", ephemeral=True)

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
        label="👤 Participants",
        style=discord.ButtonStyle.gray,
    )
    async def list_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.message.embeds:
            return await interaction.response.send_message("Uh-oh! I'm afraid that the message you interacted with doesn't exist anymore :3", ephemeral=True)

        footer_text = interaction.message.embeds[0].footer.text

        try:
            giveaway_id = int(footer_text.split(": ")[1])
        except (IndexError, ValueError):
            return await interaction.response.send_message("Uh-oh! I couldn't parse Giveaway ID. Maybe try again?", ephemeral=True)

        participant_set = self.cog.participant_cache.get(giveaway_id, set())
        participants = list(participant_set)

        g = self.cog.giveaway_cache.get(giveaway_id)
        if not g:
            return await interaction.response.send_message("This giveaway data seems to be missing :/", ephemeral=True)
        prize = g['prize']
        extra_roles_str = g.get('extra_entry_roles', '')
        extra_roles_list = [int(r) for r in extra_roles_str.split(',')] if extra_roles_str else []

        if not participants:
            return await interaction.response.send_message("There are currently no participants in this giveaway!",
                                                           ephemeral=True)
        view = ParticipantPaginator(bot=self.cog.bot, participants=participants, prize=prize, extra_roles=extra_roles_list, guild=interaction.guild)
        await interaction.response.send_message(embed=view.get_embed(), view=view, ephemeral=True)


class DestructiveConfirmationView(discord.ui.LayoutView):
    def __init__(self, title_text: str, body_text: str, color: discord.Color):
        super().__init__(timeout=30)
        self.value = None
        self.title_text = title_text
        self.body_text = body_text
        self.color = color
        self.build_layout()

    def build_layout(self):
        self.clear_items()

        container = discord.ui.Container(accent_color=self.color)

        container.add_item(discord.ui.Section(content=f"## {self.title_text}"))

        container.add_item(discord.ui.Separator())

        container.add_item(discord.ui.Section(content=self.body_text))

        container.add_item(discord.ui.Separator())

        actions = discord.ui.ActionRow(
            discord.ui.Button(label="Cancel", style=discord.ButtonStyle.grey, custom_id="cancel_btn"),
            discord.ui.Button(label="Delete Permanently", style=discord.ButtonStyle.red, custom_id="confirm_btn")
        )
        container.add_item(actions)

        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        custom_id = interaction.data.get("custom_id")
        if custom_id == "confirm_btn":
            self.value = True
        elif custom_id == "cancel_btn":
            self.value = False

        if self.value is not None:
            self.stop()
            return True
        return False

class ConfirmationView(discord.ui.LayoutView):
    def __init__(self, title_text: str, body_text: str, color: discord.Color):
        super().__init__(timeout=30)
        self.value = None
        self.title_text = title_text
        self.body_text = body_text
        self.color = color
        self.build_layout()

    def build_layout(self):
        self.clear_items()

        container = discord.ui.Container(accent_color=self.color)

        container.add_item(discord.ui.Section(content=f"## {self.title_text}"))

        container.add_item(discord.ui.Separator())

        container.add_item(discord.ui.Section(content=self.body_text))

        container.add_item(discord.ui.Separator())

        actions = discord.ui.ActionRow(
            discord.ui.Button(label="Cancel", style=discord.ButtonStyle.grey, custom_id="cancel_btn"),
            discord.ui.Button(label="Confirm", style=discord.ButtonStyle.red, custom_id="confirm_btn")
        )
        container.add_item(actions)

        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        custom_id = interaction.data.get("custom_id")
        if custom_id == "confirm_btn":
            self.value = True
        elif custom_id == "cancel_btn":
            self.value = False

        if self.value is not None:
            self.stop()
            return True
        return False

class Giveaways(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.giveaway_cache: Dict[int, dict] = {}
        self.participant_cache: Dict[int, Set[int]] = {}
        self.db_pool: Optional[asyncio.Queue[aiosqlite.connection]] = None
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
                conn = await self.db_pool.get()
                await conn.close()

    async def init_pools(self, pool_size: int = 5):
        if self.db_pool is None:
            self.db_pool = asyncio.Queue(maxsize = pool_size)
            for _ in range (pool_size):
                conn = await aiosqlite.connect(
                    GDB_PATH,
                    timeout=5,
                )
                await conn.execute("PRAGMA busy_timeout=5000")
                await conn.execute("PRAGMA journal_mode=WAL")
                await conn.execute ("PRAGMA synchronous = NORMAL")
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
                    winner_role_id INTEGER,
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

            await db.commit()

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
                async with db.execute("SELECT * FROM giveaways WHERE giveaway_id = ? AND guild_id = ?", (giveaway_id, guild_id)) as cursor:
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

        guild = self.bot.get_guild(guild_id)

        for user_id in raw_participants:
            pool.append(user_id)

            if guild and extra_roles_list:
                member = guild.get_member(user_id)
                if not member:
                    member = guild.fetch_member(user_id)
                if not member: #how would this even happen? ugh not my problem to worry about
                    pass
                if member:
                    for role_id in extra_roles_list:
                        if any(role.id == role_id for role in member.roles):
                            pool.append(user_id)

        if not pool:
            channel = self.bot.get_channel(g['channel_id'])
            if channel:
                await channel.send(embed=discord.Embed(title="Giveaway Ended", description=f"Giveaway for **{g['prize']}** ended with no participants.", colour=discord.Colour.red()))
            await self.mark_as_ended(giveaway_id, guild_id)
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
        whichone = "participant_cache" # Why separately? because we still need participant cache until this point. however, if we delay giveaway cache until here, the complex winner calculations can take time and the task loop check_giveaways may be triggered a second time, which won't be good!
        await self.mark_as_ended(giveaway_id, guild_id, whichone)

        guild = self.bot.get_guild(guild_id)
        channel = guild.get_channel(g['channel_id']) if guild else None
        if channel:
            try:
                msg = await channel.fetch_message(g['message_id'])
                embed_embed = self.create_embed_from_cache(g, winners=winners)
                await msg.edit(embed=embed_embed, view=None)

                mention_str = ", ".join([f"<@{w}>" for w in winners])
                await channel.send (f"🎉 Congratulations to: {mention_str} for winning **{g['prize']}!**")

                winner_role_id = g.get('winner_role_id')
                if winner_role_id:
                    role = guild.get_role(winner_role_id)
                    async def chunk_list(self, lst, n):
                        """Split a list into chunks of size n."""
                        for i in range(0, len(lst), n):
                            yield lst[i:i + n]
                    if role:
                        for chunk in chunk_list(winners, 5):
                            for member_id in chunk:
                                member = guild.get_member(member_id)
                                if member:
                                    try:
                                        await member.add_roles(role, reason="Giveaway Winner")
                                    except discord.HTTPException:
                                        pass

                            await asyncio.sleep(1.5)

            except Exception:
                    pass

    async def mark_as_ended(self, giveaway_id: int, guild_id: int, whichone: str):
        if whichone == 'giveaway_cache':
            if giveaway_id in self.giveaway_cache:
                self.giveaway_cache[giveaway_id]['ended'] = 1
            async with self.acquire_db() as db:
                await db.execute("UPDATE giveaways SET ended = 1 WHERE giveaway_id = ? and guild_id = ?", (giveaway_id, guild_id))
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

    def create_giveaway_embed(self, draft: GiveawayDraft, ended: bool = False):
        title_text = "GIVEAWAY ENDED" if ended else f"{draft.prize}"
        embed_color = discord.Color.red() if ended else discord.Color.blue()

        if not ended and draft.color:
            color_str = draft.color.lower()
            try:
                if color_str.startswith("#"):
                    embed_color = discord.Color.from_str(color_str)
                elif hasattr(discord.Color, color_str):
                    embed_color = getattr(discord.Color, color_str)()
            except (ValueError, TypeError):
                pass

        embed = discord.Embed(
            title=f"{title_text}",
            description=f"Click the 🎉 button below to enter this giveaway!\n\n"
                        f"Winners: **{draft.winners}**\n"
                        f"Ends: **<t:{draft.end_time}:R>**",
            colour=embed_color
        )
        if draft.host_id:
            embed = discord.Embed(
                title=f"{draft.prize}",
                description=f"Click the 🎉 button below to enter this giveaway!\n\n"
                            f"Hosted By: <@{draft.host_id}>\n"
                            f"Winners: **{draft.winners}**\n"
                            f"Ends: **<t:{draft.end_time}:R>**",
                colour=embed_color)

        if draft.required_roles:
            role_mentions = ", ".join([f"<@&{r}>" for r in draft.required_roles])
            mode = "all of the following" if draft.required_behaviour == 0 else "one of the following"
            embed.add_field(name="Requirements", value=f"Must have **{mode}**: {role_mentions}", inline=False)

        if draft.image:
            embed.set_image(url=draft.image)
        if draft.thumbnail:
            embed.set_thumbnail(url=draft.thumbnail)

        embed.set_footer(text="ID: [Giveaway ID will be generated and shown here once you click start.]")

        return embed

    async def save_giveaway(self, draft: GiveawayDraft, message_id: int, giveaway_id: int):
        req_roles = ",".join(map(str, draft.required_roles)) if draft.required_roles else ""
        black_roles = ",".join(map(str, draft.blacklisted_roles)) if draft.blacklisted_roles else ""
        extra_roles = ",".join(map(str, draft.extra_entries)) if draft.extra_entries else ""

        data = {
            "guild_id": draft.guild_id,
            "giveaway_id": giveaway_id,
            "channel_id": draft.channel_id,
            "message_id": message_id,
            "prize": draft.prize,
            "winners_count": draft.winners,
            "end_time": draft.end_time,
            "host_id": draft.host_id,
            "required_roles": req_roles,
            "req_behaviour": draft.required_behaviour,
            "blacklisted_roles": black_roles,
            "extra_entry_roles": extra_roles,
            "winner_role_id": draft.winner_role,
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

    async def giveaway_autocomplete(self, interaction: discord.Interaction, current: str, magic: bool = False):
        choices = []

        if magic:
            data_source = sorted(self.giveaway_cache.items())
        else:
            async with self.acquire_db() as db:
                async with db.execute("SELECT giveaway_id, prize FROM giveaways WHERE guild_id = ?", (interaction.guild_id,)) as cursor:
                    rows = await cursor.fetchall()
                    data_source = [(row[0], {"prize": row[1]}) for row in rows]

        for i, (giveaway_id, data) in enumerate(data_source, 1):
            label = f"{i}. {data['prize']}: {giveaway_id}"
            if current.lower() in label.lower():
                choices.append(app_commands.Choice(name=label, value=str(giveaway_id)))

        return choices[:25]

    giveaway = app_commands.Group(name="giveaway", description="Commands for Dopamine's giveaway features.")

    @giveaway.command(name="create", description="Start the giveaway creation process.")
    @app_commands.describe(
        prize="What is being given away",
        duration="How long the giveaway should last (eg. 6d, 7h, 6m, 7mon)",
        winners="The number of winners of the giveaway (defaults to one)",
        channel="The channel where the giveaway will be hosted (defaults to current channel)"
    )
    async def giveaway_create(
        self,
        interaction: discord.Interaction,
        prize: Optional[str] = "Unspecified Prize",
        duration: Optional[str] = "24h",
        winners: app_commands.Range[int, 1, 50] = 1,
        channel: Optional[discord.TextChannel] = None):
        seconds = get_duration_to_seconds(duration)
        if seconds <= 0:
            return await interaction.response.send_message('Invalid duration format! Use things like "6d", "7h", or "21m".', ephemeral=True)

        end_timestamp = get_now_plus_seconds_unix(seconds)

        channel = channel or interaction.channel

        draft = GiveawayDraft(
            guild_id=interaction.guild.id,
            channel_id=channel.id,
            prize=prize,
            winners=winners,
            end_time=end_timestamp
        )

        embed = self.create_giveaway_embed(draft)

        view = GiveawayPreviewView(self, draft)

        expires = get_now_plus_seconds_unix(900)

        await interaction.response.send_message(
            content=f"This is a preview of your giveaway. Configure it using the buttons below, then start it.\nThis preview expires **<t:{expires}:R>**!",
            embed=embed,
            view=view
        )

    @giveaway.command(name="end", description="End an active giveaway (winners are also picked and mentioned).")
    @app_commands.describe(giveaway_id="The ID of the giveaway to end.")
    async def giveaway_end(self, interaction: discord.Interaction, giveaway_id: str):
        try:
            giveaway_id = int(giveaway_id)
        except ValueError:
            return await interaction.response.send_message("That is not a valid ID!", ephemeral=True)

        if giveaway_id not in self.giveaway_cache:
            return await interaction.response.send_message("That giveaway is not active or doesn't exist!", ephemeral=True)

        body_content = f"Are you sure you want to end this giveaway right now and announce the winners??"
        view = ConfirmationView("Pending Confirmation", body_content, discord.Color.from_rgb(0, 0, 0))
        await interaction.response.send_message(view=view)
        await view.wait()

        if view.value is None:
            view.title_text = "Timed Out"
            view.body_text = f"~~{body_content}~~"
            view.color = discord.Color.red()
            view.build_layout()
            await interaction.edit_original_response(view=view)

        elif view.value is True:
            await self.end_giveaway(giveaway_id, interaction.guild_id)
            view.title_text = "Action Confirmed"
            view.body_text = f"~~{body_content}~~"
            view.color = discord.Color.green()
            view.build_layout()
            await interaction.edit_original_response(view=view)

        else:
            view.title_text = "Action Canceled"
            view.body_text = f"~~{body_content}~~"
            view.color = discord.Color.red()
            view.build_layout()
            await interaction.edit_original_response(view=view)

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
            prize = await db.execute("SELECT prize FROM giveaways WHERE giveaway_id = ? and guild_id = ?", (giveaway_id, interaction.guild.id,))
            async with db.execute("SELECT channel_id, message_id, prize FROM giveaways WHERE giveaway_id = ? AND guild_id = ?", (giveaway_id, interaction.guild.id,)) as cursor:
                row = await cursor.fetchone()

            if not row:
                return await interaction.response.send_message("Giveaway not found.", ephemeral=True)

            body_content = f"Are you sure you want to delete the giveaway for **{prize}** (ID: {giveaway_id}) permanently?"
            view = DestructiveConfirmationView("Pending Confirmation", body_content, discord.Color.from_rgb(0, 0, 0))
            await interaction.response.send_message(view=view)
            await view.wait()

            if view.value is None:
                view.title_text = "Timed Out"
                view.body_text = f"~~{body_content}~~"
                view.color = discord.Color.red()
                view.build_layout()
                await interaction.edit_original_response(view=view)

            elif view.value is True:
                async with self.acquire_db() as db:
                    await db.execute("DELETE FROM giveaways WHERE giveaway_id = ?", (giveaway_id,))
                    await db.execute("DELETE FROM giveaway_participants WHERE giveaway_id = ?", (giveaway_id,))
                    await db.execute("DELETE FROM giveaway_winners WHERE giveaway_id = ?", (giveaway_id,))
                    await db.commit()
                    try:
                        self.giveaway_cache.pop(giveaway_id)
                    except Exception:
                        pass
                view.title_text = "Action Confirmed"
                view.body_text = f"~~{body_content}~~"
                view.color = discord.Color.green()
                view.build_layout()
                await interaction.edit_original_response(view=view)

            else:
                view.title_text = "Action Canceled"
                view.body_text = f"~~{body_content}~~"
                view.color = discord.Color.red()
                view.build_layout()
                await interaction.edit_original_response(view=view)

    @giveaway_delete.autocomplete("giveaway_id")
    async def delete_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.giveaway_autocomplete(interaction, current, magic=False)

    @giveaway.command(name="reroll", description="Reroll a giveaway.")
    @app_commands.describe(giveaway_id="The ID of the giveaway to reroll.", winners="Number of new winners to pick", preserve_winners="Keep previous winners and just add new ones?")
    async def giveaway_reroll(self, interaction: discord.Interaction, giveaway_id: int, winners: int = 1, preserve_winners: bool = False):
        try:
            giveaway_id = int(giveaway_id)
        except ValueError:
            return await interaction.response.send_message("That is not a valid ID!", ephemeral=True)

        async with self.acquire_db() as db:
            async with db.execute("SELECT prize, winner_role_id, channel_id, ended FROM giveaways WHERE giveaway_id = ?", (giveaway_id,)) as cursor:
                g = await cursor.fetchone()

            if not g:
                return await interaction.response.send_message("Giveaway data not found.", ephemeral=True)

            if g[3] == 0:
                return await interaction.response.send_message("This giveaway hasn't ended yet! You can't reroll active giveaways.")

        body_content = (f"Are you sure you want to:\n\n"
                        f"* Re-roll this giveaway to pick **{winners}** new winners\n"
                        f"* {'Preserve old winners and their roles' if preserve_winners else f'over-write **{winners}** old winners and remove their winner role'}\n"
                        f"{f'* Give **{winners}** the winner role' if g[1] else ''}")

        view = ConfirmationView("Pending Confirmation", body_content, discord.Color.from_rgb(0, 0, 0))
        await interaction.response.send_message(view=view)
        await view.wait()

        if view.value is None:
            view.title_text = "Timed Out"
            view.body_text = f"~~{body_content}~~"
            view.color = discord.Color.red()
            view.build_layout()
            await interaction.edit_original_response(view=view)

        if view.value is False:
            view.title_text = "Action Canceled"
            view.body_text = f"~~{body_content}~~"
            view.color = discord.Color.red()
            view.build_layout()
            await interaction.edit_original_response(view=view)
        else:
            async def chunk_list(self, lst, n):
                for i in range(0, len(lst), n):
                    yield lst[i:i + n]

            async with self.acquire_db() as db:
                async with db.execute("SELECT user_id FROM giveaway_participants WHERE giveaway_id = ? guild_id = ?", (giveaway_id, interaction.guild_id,)) as cursor:
                    rows = await cursor.fetchall()

                    pool = [r[0] for r in rows]

                async with db.execute("SELECT user_id FROM giveaway_winners WHERE giveaway_id = ?", (giveaway_id,)) as cursor:
                    prev_rows = await cursor.fetchall()
                    if not prev_rows:
                        return await interaction.edit_original_response("This giveaway hasn't ended yet!", ephemeral=True)
                    prev_winners = [r[0] for r in prev_rows]

                eligible_pool = [uid for uid in pool if uid not in prev_winners]

                if not eligible_pool:
                    return await interaction.edit_original_response("No new participants available to pick from!", ephemeral=True)

                new_picks = random.sample(eligible_pool, min(len(eligible_pool), winners))

                if not preserve_winners:
                    if g[1]:
                        role = interaction.guild.get_role(g[1])
                        if not role:
                            role = interaction.guild.fetch_role(g[1])
                        if not role:
                            await interaction.followup_send("I can't find the role to remove from the previous winners!", ephemeral=True)
                        if role:
                            for chunk in chunk_list(self, prev_winners, 5):
                                for old_uid in chunk:
                                    member = interaction.guild.get_member(old_uid)
                                    if member and role in member.roles:
                                        try:
                                            await member.remove_roles(role, reason="Giveaway Reroll")
                                        except discord.HTTPException:
                                            pass
                                await asyncio.sleep(1.5)
                    await db.execute("DELETE FROM giveaway_winners WHERE giveaway_id = ? user_id = ?", (giveaway_id, old_uid))

                for new_uid in new_picks:
                    await db.execute("INSERT INTO giveaway_winners (giveaway_id, user_id) VALUES (?, ?)", (giveaway_id, new_uid))
                    if g[1]:
                        role = interaction.guild.get_role(g[1])
                        if not role:
                            role = interaction.guild.fetch_role(g[1])
                        if not role:
                            await interaction.followup_send("I can't find the role to give to the winners!", ephemeral=True)
                        if role:
                            for chunk in chunk_list(new_picks, 5):
                                for new_uid in chunk:
                                    member = interaction.guild.get_member(new_uid)
                                    if member:
                                        try:
                                            await member.add_roles(role, reason="Giveaway Winner")
                                        except discord.HTTPException:
                                            pass

                                await asyncio.sleep(1.5)

                await db.commit()

                channel = self.bot.get_channel(g[2])
                if not channel:
                    try:
                        channel = await self.bot.fetch_channel(g[2])
                    except (discord.Forbidden, discord.NotFound):
                        return await interaction.response.followup_send(
                            "I searched far and wide, but I can't find the channel chosen for the giveaway!\n\nEnsure that I have the necessary permissions so that I can announce the new winners.",
                            ephemeral=True)

                mention_str = ", ".join([f"<@{w}>" for w in new_picks])
                mode_text = "added to the pool of winners" if preserve_winners else "selected as the new winners"
                await channel.send (f"🎉 Congratulations to: {mention_str} for being {mode_text} for **{g[0]}**!\n\nThis giveaway has been re-rolled by {interaction.user.mention}")

                view.title_text = "Action Confirmed"
                view.body_text = f"~~{body_content}~~"
                view.color = discord.Color.green()
                view.build_layout()
                await interaction.edit_original_response(view=view)

    @giveaway_reroll.autocomplete("giveaway_id")
    async def reroll_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.giveaway_autocomplete(interaction, current, magic=False)

    @giveaway.command(name="list", description="List all giveaways in this server.")
    async def giveaway_list(self, interaction: discord.Interaction):
        await interaction.response.defer()
        async with self.acquire_db() as db:
            async with db.execute("SELECT prize, ended, end_time, giveaway_id FROM giveaways WHERE guild_id = ? ORDER BY giveaway_id ASC", (interaction.guild.id,)) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            return await interaction.edit_original_response("No giveaways found for this server.", ephemeral=True)

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
            color=discord.Color.blue()
        )
        await interaction.edit_original_response(embed=embed)

async def setup(bot):
    await bot.add_cog(Giveaways(bot))