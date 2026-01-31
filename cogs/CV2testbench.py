import discord
from discord.ext import commands
from discord import app_commands

class PrivateLayoutView(discord.ui.LayoutView):
    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                "This isn't for you!",
                ephemeral=True
            )
            return False
        return True

class RepeatingMessagesDashboard(PrivateLayoutView):
    def __init__(self):
        super().__init__(timeout=None)
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("## Repeating Messages Dashboard"))
        container.add_item(discord.ui.TextDisplay(
            "This is the dashboard for Dopamine's Repeating Messages feature. Repeating Messages are repeatedly sent in a channel at a set frequency.\nUse the buttons below to create a new Repeating Message, or to Manage existing ones.")) # Do NOT change this text at all
        container.add_item(discord.ui.Separator())

        row = discord.ui.ActionRow()
        btn_create = discord.ui.Button(label="Create", style=discord.ButtonStyle.primary)
        btn_create.callback = print() # To be implemented
        btn_manage = discord.ui.Button(label="Manage & Edit", style=discord.ButtonStyle.secondary)
        btn_manage.callback = print() # To be implemented, this will edit the message and show the ManagePage.
        row.add_item(btn_create)
        row.add_item(btn_manage)
        container.add_item(row)
        self.add_item(container)

class ChannelSelectView(PrivateLayoutView):
    def __init__(self, user):
        super().__init__(user, timeout=None)
        self.build_layout()

    def build_layout(self):
        container = discord.ui.Container()

        self.select = discord.ui.ChannelSelect(
            placeholder="Select a channel...",
            channel_types=[discord.ChannelType.text],
            min_values=1, max_values=1
        )
        self.select.callback = self.select_callback # To be implemented

        row = discord.ui.ActionRow()
        row.add_item(self.select)
        container.add_item(discord.ui.TextDisplay("### Select a channel for the Repeating Message:"))
        container.add_item(row)
        self.add_item(container)

class ManagePage(PrivateLayoutView):
    def __init__(self):
        super().__init__( timeout=None)
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        panels = print() # placeholder
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("## Manage Repeating Messages"))
        container.add_item(discord.ui.TextDisplay(
            "List of all existing repeating messages. Click Edit to configure details or the channel."))
        container.add_item(discord.ui.Separator())

        if not panels:
            container.add_item(discord.ui.TextDisplay("*No Repeating Messages found.*"))
        else:
            for idx, panel in enumerate(panels, 1):
                p_title = panel['name']
                chan_id = panel['channel_id']

                btn_edit = discord.ui.Button(label="Edit", style=discord.ButtonStyle.secondary)
                btn_edit.callback = print() # To be implemented, this will send the user to the EditPage for the specific Repeating Message.
                display_text = f"{idx}. **{p_title}** in <#{chan_id}>"
                container.add_item(discord.ui.Section(discord.ui.TextDisplay(display_text), accessory=btn_edit))
                container.add_item(discord.ui.TextDisplay("-# Page [current page number] of [total pages]"))

            container.add_item(discord.ui.Separator())
            row = discord.ui.ActionRow() # Paginator system because users may have so many repeating messages that it cant fit into this one container.
            left_btn = discord.ui.Button(label="Re◀️", style=discord.ButtonStyle.primary)
            left_btn.callback = print()  # To be implemented
            row.add_item(left_btn)
            go_btn = discord.ui.Button(label="Return to Dashboard", style=discord.ButtonStyle.secondary)
            go_btn.callback = print()  # To be implemented
            row.add_item(go_btn)
            right_btn = discord.ui.Button(label="▶️", style=discord.ButtonStyle.primary)
            right_btn.callback = print()  # To be implemented
            row.add_item(right_btn)
            container.add_item(row)

        container.add_item(discord.ui.Separator())
        row = discord.ui.ActionRow()
        return_btn = discord.ui.Button(label="Return to Dashboard", style=discord.ButtonStyle.secondary)
        return_btn.callback = print() # To be implemented
        row.add_item(return_btn)
        container.add_item(row)
        self.add_item(container)

class EditPage(PrivateLayoutView):
    def __init__(self, user, cog, guild_id, panel_data):
        super().__init__(user, timeout=None)
        self.panel_data = panel_data
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay(f"## Edit: {['Name']}"))
        container.add_item(discord.ui.Separator())
        details = (
            f"**State:** Active or Inactive with Green or Red circle emoji\n"
            f"**Channel:** placeholder\n"
            f"**Frequency:** placeholder\n"
            f"**Frequency:** `placeholder\n"
            f"**Message Content:**\n```Message content```"
        )
        container.add_item(discord.ui.TextDisplay(details))
        container.add_item(discord.ui.Separator())

        row1 = discord.ui.ActionRow()
        btn_state = discord.ui.Button(label=f"Active or Inactive",
                                     style=discord.ButtonStyle.primary if 1==1 else discord.ButtonStyle.secondary) # 1==1 is placeholder for the toggle
        btn_state.callback = self.toggle_state_callback # To be implemented
        btn_edit_message = discord.ui.Button(label="Edit Message Content", style=discord.ButtonStyle.secondary)
        btn_edit_message.callback = self.edit_message_callback # To be Implemented
        btn_edit_channel = discord.ui.Button(label="Edit Channel", style=discord.ButtonStyle.secondary)
        btn_edit_channel.callback = self.edit_channel_callback # To be implemented, this shall send the channel edit select view above.
        btn_delete = discord.ui.Button(label="Delete", style=discord.ButtonStyle.danger) # Shows the destructive confirmation view.
        btn_delete.callback = self.delete_callback
        btn_frequency = discord.ui.Button(label="Edit Frequency", style=discord.ButtonStyle.secondary)
        btn_frequency.callback = self.edit_duration_callback

        row1.add_item(btn_state)
        row1.add_item(btn_edit_message)
        row1.add_item(btn_edit_channel)
        row1.add_item(btn_frequency)
        row1.add_item(btn_delete)
        container.add_item(row1)

        back_row = discord.ui.ActionRow()
        btn_back = discord.ui.Button(label="Return to Manage Menu", style=discord.ButtonStyle.secondary)
        btn_back.callback = self.back_callback
        back_row.add_item(btn_back)
        container.add_item(back_row)

        self.add_item(container)

class DestructiveConfirmationView(PrivateLayoutView):
    def __init__(self, user, name, cog, guild_id):
        super().__init__(user, timeout=30)
        self.name = name
        self.cog = cog
        self.guild_id = guild_id
        self.value = None
        self.title_text = "Delete Repeating Message"
        self.body_text = f"Are you sure you want to permanently delete the repeating message **{name}**? This cannot be undone."
        self.build_layout()
    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay(f"### {self.title_text}"))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(self.body_text))

        if self.value is None:
            action_row = discord.ui.ActionRow()
            cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.gray)
            confirm = discord.ui.Button(label="Reset to Default", style=discord.ButtonStyle.red)

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

    async def on_timeout(self, interaction: discord.Interaction):
        if self.value is None and self.message:
            await self.update_view(interaction, "Timed Out", discord.Color(0xdf5046))


class CV2TestCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="cv2test", description="Tests Discord Components V2 layout")
    async def cv2test(self, interaction: discord.Interaction):
        view = ManagePage()
        await interaction.response.send_message(
            view=view,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(CV2TestCog(bot))