from dataclasses import dataclass
from typing import Optional, List
import discord
from discord import app_commands, Interaction

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
            discord.SelectOption(label="4. Required Roles Behavior", value="behavior", description="The behavior of the required roles feature."),
            discord.SelectOption(label="5. Winner Role", value="winner_role", description="Role given to winners."),
            discord.SelectOption(label="6. Blacklisted Roles", value="blacklist", description="Roles that cannot participate."),
            discord.SelectOption(label="7. Image", value="image", description="Provide a valid URL for the Embed image."),
            discord.SelectOption(label="8. Thumbnail", value="thumbnail", description="Provide a valid URL for the Embed thumbnail."),
            discord.SelectOption(label="9. Color", value="color", description="Set embed color (Hex or Valid Name).")
        ]
        super().__init__(placeholder="Select a setting to customize...", options=options)

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

