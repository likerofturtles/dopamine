from dataclasses import dataclass
from typing import Optional, List
import discord
from discord import app_commands, Interaction
import re

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