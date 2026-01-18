from dataclasses import dataclass
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
    required_behavior: int = 0 # 0 = All, 1 = One
    blacklisted_roles: List[int] = None
    extra_entries: List[int] = None
    winner_role: Optional[int] = None
    image: Optional[str] = None
    thumbnail: Optional[str] = None
    color: str = "discord.Color.blue()"

class GiveawayEditSelect:
    def __init__(self):
        options = [
            discord.SelectOption(label="1. Giveaway Host", value="host", description="The host name to be shown in the giveaway Embed."),
            discord.SelectOption(label="2. Extra Entries Role", value="extra", description="Roles that will give extra entries. Each role gives +1 entries."),
            discord.SelectOption(label="3. Required Roles", value="required", description="Roles required to participate."),
            discord.SelectOption(label="4. Required Roles Behaviour", value="behavior", description="The behavior of the required roles feature."),
            discord.SelectOption(label="5. Winner Role", value="winner_role", description="Role given to winners."),
            discord.SelectOption(label="6. Blacklisted Roles", value="blacklist", description="Roles that cannot participate."),
            discord.SelectOption(label="7. Image", value="image", description="Provide a valid URL for the Embed image."),
            discord.SelectOption(label="8. Thumbnail", value="thumbnail", description="Provide a valid URL for the Embed thumbnail."),
            discord.SelectOption(label="9. Colour", value="color", description="Set embed color (Hex or Valid Name).")
        ]
        super().__init__(placeholder="Select a setting to customize...", options=options)
        # TO BE IMPLEMENTED
        async def callback(self, interaction: discord.Interaction):

            pass

class GiveawayVisualsModal(discord.ui.Modal):
    def __init__(self, trait: str, draft: GiveawayDraft):
        super().__init__(title=f"Edit Giveaway {trait.title()}")
        self.trait = trait
        self.draft = draft
        self.input_field = discord.ui.TextInput(
            label=f"Enter {trait}",
            placeholder="Type here...",
            required=True
        )

    async def on_submit(self, interaction: discord.Interaction):
        value = self.input_field.value

        if self.trait == "image":
            self.draft.image = value
        elif self.trait == "thumbnail":
            self.draft.thumbnail = value
        elif self.trait == "color":
            self.draft.color = value

        await interaction.response.send_message(f"Updated **{self.trait}** successfully!", ephemeral=True)

class ParticipantPaginator(discord.ui.View):
    def __init__(self, participants: list, prize: str):
        super().__init__(timeout=120)
        self.participants = participants
        self.prize = prize
        self.current_page = 0
        self.per_page = 10

    def get_embed(self):
        start = self.current_page * self.per_page
        end = start + self.per_page

        page_list = self.participants[start:end]
        mentions = "\n".join([f"<@{uid}>" for uid in page_list]) or "No participants yet."

        total_pages = (len(self.participants) - 1) // self.per_page + 1

        embed = discord.Embed(
            title="Participants for {self.prize}",
            description=mentions,
            color=discord.Color.blue()
        )
        embed.set_footer(text=f"Page {self.current_page + 1}/{total_pages}")
        return embed

    @discord.ui.button(label="◀️", style=discord.ButtonStyle.gray)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
            await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="▶️", style=discord.ButtonStyle.gray)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if (self.current_page +1) * self.per_page < len(self.participants):
            self.current_page += 1
            await interaction.response.edit_message(embed=self.get_embed(), view=self)

class BehaviorSelect(discord.ui.Select):
    def __init__(self, draft: GiveawayDraft):
        options = [
            discord.SelectOption(label="All required roles", value="0", description="Participant must have every role listed."),
            discord.SelectOption(label="One of the required roles", value="1", description="Participant must have at least one role listed.")
        ]

        super().__init__(placeholder="Choose role requirement behaviour...", options=options)

        async def callback(self, interaction: discord.Interaction):
            self.draft.required_behavior = int(self.values[0])
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
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Start", style=discord.ButtonStyle.green)
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        #TO BE IMPLEMENTED
        await interaction.response.send_message(embed=discord.Embed(description="Giveaway started successfully!", colour=discord.Colour.green()), ephemeral=True)
        self.stop()

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.gray)
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = GiveawayEditSelect(self.cog, self.draft, self)

        await interaction.response.send_message(embed=discord.embed(title="Edit Giveaway", description="Select what you want to edit using the dropdown below.", colour=discord.Colour.blue()), view=view, ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=discord.Embed(title="Giveaway Creation Cancelled."), view=None)
        self.stop()

class MemberSelectView(discord.ui.View):
    def __init__(self, draft: GiveawayDraft):
        super().__init__()
        self.add_item(discord.ui.Select(placeholder="Select a host...", min_values=1, max_values=1))
        self.draft = draft

    #TO BE IMPLEMENTED

class RoleSelectView(discord.ui.View):
    def __init__(self, trait: str, draft: GiveawayDraft):
        super().__init__()
        self.add_item(discord.ui.Select(placeholder=f"Select {trait}...", min_values=1, max_values=20))
        self.trait = trait
        self.draft = draft

class WinnerRoleSelectView(discord.ui.View):
    def __init__(self, trait: str, draft: GiveawayDraft):
        super().__init__()
        self.add_item(discord.ui.Select(placeholder=f"Select {trait}...", min_values=1, max_values=1))
        self.trait = trait
        self.draft = draft

class GiveawayEditView(discord.ui.View):
    def __init__(self, cog, draft: GiveawayDraft, parent_view: GiveawayPreviewView):
        super().__init__()
        self.cog = cog
        self.draft = draft
        self.parent_view = parent_view

        self.select_menu = GiveawayEditSelect()
        self.add_item(self.select_menu)

        async def handle_selection(self, interaction: discord.Interaction, value: str):
            if value in ["image", "thumbnail", "color"]:
                await interaction.response.send_modal(GiveawayVisualsModal(value, self.draft))
            elif value == "behavior":
                new_view = discord.ui.View()
                new_view.add_item(BehaviorSelect(self.draft))
                await interaction.response.send_message("Change required role behaviour:", view=new_view, ephemeral=True)
            elif value == "extra":
                new_view = discord.ui.View()
                trait = "extra entries role"
                new_view.add_item(RoleSelectView(trait, self.draft))
                await interaction.response.send_message("Choose roles which will give extra entries:", view=new_view, ephemeral=True)
            elif value == "required":
                new_view = discord.ui.View()
                trait = "Required Roles"
                new_view.add_item(RoleSelectView(trait, self.draft))
                await interaction.response.send_message("Choose required roles to participate:", view=new_view, ephemeral=True)
            elif value == "winner_role":
                new_view = discord.ui.View()
                trait = "Winners' Role"
                new_view.add_item(WinnerRoleSelectView(trait, self.draft))
                await interaction.response.send_message("Choose role to be given to winner(s):", view=new_view, ephemeral=True)
            elif value == "blacklist":
                new_view = discord.ui.View()
                trait = "Blacklisted Roles"
                new_view.add_item(RoleSelectView(trait, self.draft))
                await interaction.response.send_message("Choose roles that can't participate:", view=new_view, ephemeral=True)
            elif value == "host":
                new_view = discord.ui.View()
                new_view.add_item(MemberSelectView(self.draft))
                await interaction.response.send_message("Choose the host for this giveaway:", view=new_view, ephemeral=True)

def create_giveaway_embed(self, draft: GiveawayDraft, ended: bool = False):
    if ended:
        embed_color = discord.Color.red()
        title_text = "GIVEAWAY ENDED"
    else:
        embed_color = discord.Color.blue()
        title_text = f"{draft.prize}"
        if draft.color:
            try:
                if draft.color.startswith("#"):
                    embed_color = discord.Color.from_str(draft.color)
                else:
                    embed_color = getattr(discord.Color, draft.color.lower())()
            except (ValueError, AttributeError):
                pass

    embed = discord.Embed(
        title=f"{title_text}",
        description=f"Click the 🎉 button below to enter this giveaway!\n\n"
                    f"Winners: **{draft.winners}**"
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
        mode = "all of the following" if draft.required_behavior == 0 else "one of the following"
        embed.add_field(name="Requirements", value=f"Must have **{mode}**: {role_mentions}", inline=False)

    if draft.image:
        embed.set_image(url=draft.image)
    if draft.thumbnail:
        embed.set_thumbnail(url=draft.thumbnail)

    return embed

async def save_giveaway(self, draft: GiveawayDraft, message_id: int):
    req_roles = ",".join(map(str, draft.required_roles)) if draft.required_roles else ""
    black_roles = ",".join(map(str, draft.blacklisted_roles)) if draft.blacklisted_roles else ""
    extra_roles = ",".join(map(str, draft.extra_entries)) if draft.extra_entries else ""

    giveaway_id = int(discord.utils.utcnow().timestamp())

    async with self.acquire_dv() as db:
        await db.execute('''
        INSERT INTO giveaways (
        guild_id, giveaway_id, channel_id, message_id, prize, winners_count, end_time, host_id, required_roles, req_behaviour, blacklisted_roles, extra_entry_roles, winner_role_id, image_url, thumbnail_url, color, ended
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        ''', (
            draft.guild_id, giveaway_id, draft.channel_id, message_id, draft.prize, draft.winners, draft.end_time, draft.host_id, req_roles, draft.required_behavior, black_roles, extra_roles, draft.winner_role, draft.image, draft.thumbnail, draft.color
        ))
        await db.commit()
    return giveaway_id

async def end_giveaway(self, giveaway_id: int, guild_id: int):
    # TO BE IMPLEMENTED
    pass

async def reroll_giveaway(self, giveaway_id: int, winners_to_pick: int, include_previous: bool, remove_old_roles: bool):
     # TO BE IMPLEMENTED
    pass