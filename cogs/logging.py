import discord
from beacon import PrivateLayoutView, beacon_commands
from discord import app_commands
from discord.ext import commands

from utils.data_handlers import export_table
from utils.data_protocol import DataDeleteResult, DataExportChunk, DataFeatureMeta, DataMonitorResult
from utils.discord_health import is_access_error, report_access_failure
from utils.log import LoggingManager


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

    async def on_timeout(self, interaction: discord.Interaction):
        if self.value is None and self.message:
            self.value = False
            await self.update_view(interaction, "Timed Out", discord.Color(0xdf5046))

class Logging(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.manager = LoggingManager(bot.db)

    async def cog_load(self):
        await self.bot.db.wait_ready()
        await self.manager.populate_cache()

    async def cog_unload(self):
        pass

    log = beacon_commands.Group(name="logging", description="Manage logging feature.", permissions_preset="security")
    @log.command(name="set", description="Set the logging channel for logs.")
    @app_commands.describe(channel="Channel to use for logs")
    async def setlog(self, interaction: discord.Interaction, channel: discord.TextChannel):
        already = await self.manager.log_get(interaction.guild.id)
        await self.manager.log_set(interaction.guild.id, channel.id)

        embed = discord.Embed(
            title="This channel has been set as the log channel.",
            description=f"All moderation logs will now be sent here.",
            color=discord.Color(0x944ae8)
        )
        embed.set_footer(text=f"Set by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        channel = self.bot.get_channel(channel.id) or await self.bot.fetch_channel(channel.id)
        if not channel:
            return await interaction.response.send_message("I can't find the channel that you set for logging! Please ensure I have the necessary permissions.", ephemeral=True)
        try:
            await channel.send(embed=embed)
        except Exception as e:
            if is_access_error(e):
                await report_access_failure(self.bot, interaction.guild.id, "logging")
            return await interaction.response.send_message(
                "I can't send messages in that channel. Please check my permissions.",
                ephemeral=True,
            )
        await interaction.response.send_message(embed=discord.Embed(
            title=f"{"Logging has been enabled" if already else "Logging Channel Updated"}",
            description=f"Log channel set to {channel.mention}",
            color=discord.Color.green()), ephemeral=True)

    @log.command(name="get", description="Check what channel is set as the logging channel.")
    async def getlog(self, interaction: discord.Interaction):
        channel_id = await self.manager.log_get(interaction.guild.id)
        await interaction.response.send_message(f"The logging channel is currently set to <#{channel_id}>.", ephemeral=True)

    @log.command(name="test", description="Test whether the bot can access the logging channel or not.")
    async def testlog(self, interaction: discord.Interaction):
        channel_id = await self.manager.log_get(interaction.guild.id)
        if not channel_id:
            return await interaction.response.send_message(f"No logging channel is set in **{interaction.guild}**.")
        channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
        if not channel:
            return await interaction.response.send_message(
                "I can't find the channel that you set for logging! Please ensure I have the necessary permissions.",
                ephemeral=True)
        embed = discord.Embed(title="Beep, boop!",
                              description=f"This is a test message to test whether logging works or not.",
                              color=discord.Colour.blue())
        try:
            await channel.send(embed=embed)
        except Exception as e:
            if is_access_error(e):
                await report_access_failure(self.bot, interaction.guild.id, "logging")
            return await interaction.response.send_message(
                "I can't send messages in the logging channel. Please check my permissions.",
                ephemeral=True,
            )
        await interaction.response.send_message("Test message has been sent successfully!", ephemeral=True)

    @log.command(name="disable", description="Disable logging and delete logging channel for this server from database.")
    async def deletelog(self, interaction: discord.Interaction):
        exists = await self.manager.log_get(interaction.guild.id)
        if not exists:
            return await interaction.response.send_message("Logging is already disabled in this server.", ephemeral=True)

        body_content = f"Are you sure you want to:\n* Disable logging\n* Delete the logging channel from the database permanently."
        view = DestructiveConfirmationView("Pending Confirmation", body_content)
        response = await interaction.response.send_message(view=view)
        view.message = await interaction.original_response()
        await view.wait()

        if view.value is True:
            await self.manager.log_remove(interaction.guild_id)

    def data_features(self) -> list[DataFeatureMeta]:
        return [DataFeatureMeta(
            feature_id="logging",
            name="Logging",
            guild_export=True,
            guild_delete=True,
        )]

    async def data_export_user(self, user_id: int, *, guild_ids: list[int] | None) -> DataExportChunk:
        return DataExportChunk(feature_id="logging")

    async def data_export_guild(self, guild_id: int) -> DataExportChunk:
        chunk = DataExportChunk(feature_id="logging")
        rows = await export_table(
            self.bot.db,
            "SELECT guild_id, channel_id FROM log_channels WHERE guild_id = ?",
            (guild_id,),
        )
        if rows:
            chunk.guild_data[guild_id] = rows[0]
        return chunk

    async def data_delete_user(self, user_id: int, *, guild_ids: list[int] | None, feature_id: str | None) -> DataDeleteResult:
        return DataDeleteResult(feature_id="logging")

    async def data_delete_guild(self, guild_id: int, feature_id: str | None) -> DataDeleteResult:
        if feature_id and feature_id != "logging":
            return DataDeleteResult(feature_id="logging")
        existed = await self.manager.log_get(guild_id)
        if not existed:
            return DataDeleteResult(feature_id="logging")
        await self.manager.log_remove(guild_id)
        return DataDeleteResult(feature_id="logging", deleted=True, rows_affected=1)

    async def data_monitor_guild(self, guild: discord.Guild) -> DataMonitorResult:
        result = DataMonitorResult(feature_id="logging")
        channel_id = await self.manager.log_get(guild.id)
        if not channel_id:
            return result
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
            and channel.permissions_for(guild.me).send_messages
        )
        if not accessible:
            await self.manager.log_remove(guild.id)
            result.actions.append("disabled_logging")
        return result

async def setup(bot):
    await bot.add_cog(Logging(bot))