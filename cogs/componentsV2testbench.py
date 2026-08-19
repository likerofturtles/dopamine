from __future__ import annotations

import discord
from beacon import PrivateLayoutView
from discord import app_commands
from discord.ext import commands


class BirthdayDashboard(PrivateLayoutView):
    def __init__(self, user: discord.User | discord.Member) -> None:
        super().__init__(user, timeout=None)
        self.build_layout()

    def build_layout(self) -> None:
        self.clear_items()
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("## Birthday Dashboard"))
        container.add_item(discord.ui.TextDisplay("Manage everything related to birthdays here."))
        container.add_item(discord.ui.Separator())
        create_btn = discord.ui.Button(label="Create", style=discord.ButtonStyle.primary)
        list_up_btn = discord.ui.Button(label="Upcoming Birthdays", style=discord.ButtonStyle.primary)
        manage_btn = discord.ui.Button(label="Manage Birthdays", style=discord.ButtonStyle.primary)
        settings_btn = discord.ui.Button(label="Settings", style=discord.ButtonStyle.secondary)

        row = discord.ui.ActionRow()
        row.add_item(create_btn)
        row.add_item(list_up_btn)
        row.add_item(manage_btn)
        row.add_item(settings_btn)

        container.add_item(row)

        self.add_item(container)


class BirthdaySettings(PrivateLayoutView):
    def __init__(self, user: discord.User | discord.Member) -> None:
        super().__init__(user, timeout=None)
        self.build_layout()

    def build_layout(self) -> None:
        self.clear_items()
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("## Birthday Settings"))
        container.add_item(discord.ui.TextDisplay("Configure the settings for birthdays here."))
        container.add_item(discord.ui.Separator())

        self.add_item(container)


class CV2TestCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="cv2test", description="Tests Discord Components V2 layout")
    async def cv2test(self, interaction: discord.Interaction) -> None:
        from cogs.autoresponse import AutoresponseDashboard
        view = AutoresponseDashboard(interaction.user)
        await interaction.response.send_message(
            view=view
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CV2TestCog(bot))
