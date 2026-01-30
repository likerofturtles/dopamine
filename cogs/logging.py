import discord
from discord import app_commands
from discord.ext import commands
from utils.log import LoggingManager
from utils.checks import slash_mod_check
from discord.ui import Button, View, TextDisplay

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

class DestructiveConfirmationView(PrivateLayoutView):
    def __init__(self, title_text: str, body_text: str, color: discord.Color = None):
        super().__init__(timeout=30)
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

        if self.value is None:
            action_row = discord.ui.ActionRow()
            cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.gray)
            confirm = discord.ui.Button(label="Delete Permanently", style=discord.ButtonStyle.red)

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

class Logging(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.manager = LoggingManager()

    async def cog_load(self):
        await self.manager.init_pools()
        await self.manager.init_db()
        await self.manager.populate_cache()

    log = app_commands.Group(name="logging", description="Manage logging feature.")
    @log.command(name="set", description="Set the logging channel for logs.")
    @app_commands.check(slash_mod_check)
    @app_commands.describe(channel="Channel to use for logs")
    async def setlog(self, interaction: discord.Interaction, channel: discord.TextChannel):
        already = await self.manager.logging_get(interaction.guild.id)
        await self.manager.logging_set(interaction.guild.id, channel.id)

        embed = discord.Embed(
            title="This channel has been set as the log channel.",
            description=f"All moderation logs will now be sent here.",
            color=discord.Color.blue()
        )
        embed.set_footer(text=f"Set by {interaction.user}", icon_url=interaction.user.display_avatar.url)
        channel = self.bot.get_channel(channel.id)
        if not channel:
            channel = self.bot.fetch_channel(channel.id)
        if not channel:
            return await interaction.response.send_message("I can't find the channel that you set for logging! Please ensure I have the necessary permissions.", ephemeral=True)
        await channel.send_message(embed=embed)
        await interaction.response.send_message(embed=discord.Embed(
            title=f"{"Logging has been enabled" if already else "Logging Channel Updated"}",
            description=f"Log channel set to {channel.mention}",
            color=discord.Color.green()), ephemeral=True)

    @log.command(name="get", description="Check what channel is set as the logging channel.")
    @app_commands.check(slash_mod_check)
    async def getlog(self, interaction: discord.Interaction):
        channel_id = await self.manager.logging_get(interaction.guild.id)
        await interaction.response.send_message(f"The logging channel is currently set to <#{channel_id}>.", ephemeral=True)

    @log.command(name="test", description="Test whether the bot can access the logging channel or not.")
    @app_commands.check(slash_mod_check)
    async def testlog(self, interaction: discord.Interaction):
        channel_id = await self.manager.logging_get(interaction.guild.id)
        if not channel_id:
            return await interaction.response.send_message(f"No logging channel is set in **{interaction.guild}**.")
        channel = self.bot.get_channel(channel_id)
        if not channel:
            channel = self.bot.fetch_channel(channel_id)
        if not channel:
            return await interaction.response.send_message(
                "I can't find the channel that you set for logging! Please ensure I have the necessary permissions.",
                ephemeral=True)
        embed = discord.Embed(title="Test",
                              description=f"Beep, boop! This is a test message to test whether logging works or not.",
                              color=discord.Colour.blue())
        await channel.send_message(embed=embed, ephemeral=True)

    @log.command(name="disable", description="Disable logging and delete logging channel for this server from database.")
    @app_commands.check(slash_mod_check)
    async def deletelog(self, interaction: discord.Interaction):
        exists = await self.manager.logging_get(interaction.guild.id)
        if not exists:
            return await interaction.response.send_message("Logging is already disabled in this server.", ephemeral=True)

        body_content = f"Are you sure you want to:\n* Disable logging\n* Delete the logging channel from the database permanently."
        view = DestructiveConfirmationView("Pending Confirmation", body_content)
        response = await interaction.response.send_message(view=view)
        view.message = await interaction.original_response()
        await view.wait()

        if view.value is True:
            await self.manager.logging_delete(interaction.guild_id)
async def setup(bot):
    await bot.add_cog(Logging(bot))