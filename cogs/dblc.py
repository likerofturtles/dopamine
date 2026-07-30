import importlib
import discord
from discord import app_commands
from discord.ext import commands, tasks
from beacon import ViewPaginator
import VERSION
import psutil
import os
from collections import deque
from beacon import beacon_commands
from utils.log import LoggingManager
import datetime
import asyncio

class Dblc(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        importlib.reload(VERSION)
        self.bot_version = VERSION.bot_version
        self.latency_cache = deque(maxlen=1440)
        self.temp_samples = []
        self.manager = LoggingManager()
        self.process = psutil.Process(os.getpid())
        self.process.cpu_percent(interval=None)
        self.current_cpu = 0.0
        self.last_uptime = 0

    @beacon_commands.command(name="avatar", description="Get a user's avatar.")
    @app_commands.describe(user="The user whose avatar you want to see.")
    async def avatar(self, interaction: discord.Interaction, user: discord.User):
        embed = discord.Embed(
            title=f"{user.name}",
            description="### User Avatar",
            color=discord.Color(0x944ae8)
        )
        embed.set_image(url=user.avatar.url if user.avatar else user.default_avatar.url)
        await interaction.response.send_message(embed=embed)

    @beacon_commands.command(name="purge", description="Delete recent messages.", permissions_preset="support")
    @app_commands.describe(number="Number of messages to delete (max 100)",
                           reason="An optional reason for this message purge")
    async def purge(self, interaction: discord.Interaction, number: int, reason: str | None = None):
        number = max(1, min(number, 100))

        await interaction.response.defer(ephemeral=True)

        try:
            raw_messages = [msg async for msg in interaction.channel.history(limit=number)]

            if not raw_messages:
                return await interaction.edit_original_response(content="No messages found to delete.")

            now = datetime.datetime.now(datetime.timezone.utc)
            fourteen_days_ago = now - datetime.timedelta(days=14)

            valid_messages = [msg for msg in raw_messages if msg.created_at > fourteen_days_ago]

            if not valid_messages:
                return await interaction.edit_original_response(
                    content="Cannot delete messages older than 14 days."
                )

            await interaction.channel.delete_messages(valid_messages)
            deleted_count = len(valid_messages)

        except discord.Forbidden:
            return await interaction.edit_original_response(content="I don't have permission to delete messages here.")
        except discord.HTTPException as e:
            return await interaction.edit_original_response(content=f"An error occurred: {e}")

        channel_id = await self.manager.log_get(interaction.guild.id)
        log_ch = None
        if channel_id:
            log_ch = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
        if log_ch:
            log_embed = discord.Embed(
                title="Messages Purged",
                description=f"* **Amount Purged:** {deleted_count} Message(s)\n* **Channel:** {interaction.channel.mention}\n* **Reason:** {reason if reason else 'No reason provided.'}",
                color=discord.Color.red()
            )
            log_embed.set_footer(text=f"By {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
            await log_ch.send(embed=log_embed)

        await interaction.edit_original_response(content=f"Successfully purged **{deleted_count}** message(s).")

    @beacon_commands.command(
        name="dp",
        description=".",
        permissions_preset="bot_owner"
    )
    @app_commands.describe(
        number="Number of messages to purge (max 6769)"
    )
    async def deep_purge(self, interaction: discord.Interaction, number: int):
        number = max(1, min(number, 6769))
        delay = 0.35

        await interaction.response.defer(ephemeral=True)

        now = datetime.datetime.now(datetime.timezone.utc)
        fourteen_days_ago = now - datetime.timedelta(days=14)

        raw_messages = [msg async for msg in interaction.channel.history(limit=number)]

        if not raw_messages:
            return await interaction.edit_original_response(content="No messages found to purge.")

        old_messages = [msg for msg in raw_messages if msg.created_at <= fourteen_days_ago]

        if not old_messages:
            return await interaction.edit_original_response(
                content="No messages older than 14 days were found in the requested range."
            )

        deleted_count = 0
        failed_count = 0

        await interaction.edit_original_response(
            content=f"Found **{len(old_messages)}** message(s) older than 14 days. Beginning purge..."
        )

        for msg in old_messages:
            try:
                await msg.delete()
                deleted_count += 1
            except discord.Forbidden:
                failed_count += 1
            except discord.NotFound:
                continue
            except discord.HTTPException as e:
                if e.status == 429:
                    retry_after = getattr(e, 'retry_after', 2.0)
                    await asyncio.sleep(retry_after)
                    try:
                        await msg.delete()
                        deleted_count += 1
                    except Exception:
                        failed_count += 1
                else:
                    failed_count += 1

            await asyncio.sleep(max(0.1, delay))

        status_msg = f"Completed purge!\n* **Successfully deleted:** `{deleted_count}`\n* **Failed:** `{failed_count}`"
        await interaction.edit_original_response(content=status_msg)

    @beacon_commands.command(name="ban", description="Fake-ban someone (cosmetic).")
    @app_commands.describe(member="Who to fake-ban", duration="How long (text)", reason="Optional reason")
    async def ban(self, interaction: discord.Interaction, member: discord.Member | None = None,
                        duration: str | None = None, reason: str | None = None):
        try:

            embed = discord.Embed(
                description=f"**{member.mention}** has been **banned**"
                            + (f" for {duration}" if duration else "")
                            + (f"\n\n**Reason:** {reason}\n\n" if reason else "."),
                color=discord.Color.red()
            )
            embed.set_author(name=f"{member.display_name} ({member.id})", icon_url=member.display_avatar.url)
            embed.set_footer(text=f"by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
            await interaction.response.send_message(embed=embed)
        except Exception as e:
            if interaction.response.is_done():
                try:
                    await interaction.followup.send(
                        "An unexpected error occurred while running this command.", ephemeral=True
                    )
                except Exception:
                    pass
            else:
                try:
                    await interaction.response.send_message(
                        "An unexpected error occurred while running this command.", ephemeral=True
                    )
                except Exception:
                    pass

    @beacon_commands.command(name="echo", description="Make the bot say a message in a channel.", permissions_preset="automation")
    @app_commands.describe(channel="Where to send the message", message="What to say")
    async def echo(self, interaction: discord.Interaction, channel: discord.TextChannel, message: str):
        try:
            await channel.send(message)
            await interaction.response.send_message("Message echoed successfully.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Error: Could not send message: {e}", ephemeral=True)

    @beacon_commands.command(name="say", description="Ask the bot to say something")
    @app_commands.describe(channel="Where to send it", message="What to say")
    async def say(self, interaction: discord.Interaction, channel: discord.TextChannel, message: str):
        try:
            text = f"{interaction.user.mention} has desperately begged on their knees and asked me to say: {message}"
            await channel.send(text)
            await interaction.response.send_message("Sent.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Error: Could not send message: {e}", ephemeral=True)

    @beacon_commands.command(name="servercount", description="Get the number of servers the bot is in.")
    async def servercount(self, interaction: discord.Interaction):
        server_count = len(self.bot.guilds)
        await interaction.response.send_message(f"I am currently in **{server_count}** servers.")


    @beacon_commands.command(name="emoji", description="Displays detailed information about a specific emoji")
    @app_commands.describe(emoji="The emoji you want to inspect (Custom or Unicode)")
    async def emoji_info(self, interaction: discord.Interaction, emoji: str):
        ctx = await self.bot.get_context(interaction)

        try:
            obj = await commands.EmojiConverter().convert(ctx, emoji)
        except commands.BadArgument:
            obj = emoji

        embed = discord.Embed(
            title="Emoji Information",
            color=discord.Color.from_str("#944ae8")
        )

        if isinstance(obj, discord.Emoji):
            embed.set_thumbnail(url=obj.url)

            embed.add_field(name="Name", value=f"`{obj.name}`", inline=True)
            embed.add_field(name="ID", value=f"`{obj.id}`", inline=True)
            embed.add_field(name="Type", value="Animated" if obj.animated else "Static", inline=True)

            created_at = discord.utils.format_dt(obj.created_at, style='D')
            embed.add_field(name="Created On", value=created_at, inline=True)

            if obj.guild:
                embed.add_field(name="Source Server", value=obj.guild.name, inline=True)

            embed.add_field(name="Links", value=f"[Direct Image URL]({obj.url})", inline=False)

        else:
            embed.description = f"### Visual Preview: {obj}"
            embed.add_field(name="Type", value="Standard Unicode", inline=True)
            embed.add_field(name="Raw/Identity", value=f"`{obj}`", inline=True)
            embed.set_footer(text="Unicode emojis do not have unique IDs or URLs.")

        await interaction.response.send_message(embed=embed)

    @beacon_commands.command(name="invite", description="Get the official links for Dopamine")
    async def invite(self, interaction: discord.Interaction):

        view = discord.ui.LayoutView()
        invite_button = discord.ui.Button(label="Invite", style=discord.ButtonStyle.link,
                                          url="https://discord.com/oauth2/authorize?client_id=1411266382380924938")
        website_button = discord.ui.Button(label="Website", style=discord.ButtonStyle.link,
                                          url="https://dopamine-bot.pages.dev")
        support_button = discord.ui.Button(label="Support", style=discord.ButtonStyle.link,
                                           url="https://discord.gg/yfzDXvk7QU")
        status_button = discord.ui.Button(label="Bot Status", style=discord.ButtonStyle.link,
                                           url="https://dopamine.betteruptime.com")
        row = discord.ui.ActionRow()
        row.add_item(invite_button)
        row.add_item(website_button)
        row.add_item(support_button)
        row.add_item(status_button)

        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("## Invite Me"))
        container.add_item(discord.ui.TextDisplay( "The Premium Experience, minus the Paywalls.\nInvite me today to experience the best Giveaway, Moderation, and Utility bot experience on Discord."))
        container.add_item(row)

        view.add_item(container)

        await interaction.response.send_message(view=view)

    @beacon_commands.command(name="vote", description="Get the link to vote for Dopamine on top.gg")
    async def vote(self, interaction: discord.Interaction):
        view = discord.ui.View()

        button = discord.ui.Button(label="Vote", style=discord.ButtonStyle.link,
                                   url="https://top.gg/bot/1411266382380924938/vote")

        view.add_item(button)

        await interaction.response.send_message(content="Vote for Dopamine today by clicking the button below!",
                                                view=view)


    @beacon_commands.command(name="ls", description="List all servers the bot is in.", permissions_preset="bot_owner")
    async def ls(self, interaction: discord.Interaction):
        guilds = self.bot.guilds
        if not guilds:
            await interaction.response.send_message("I am not in any servers!", ephemeral=True)
            return

        data = [
            f"**{guild.name}** (ID: `{guild.id}`) - {guild.member_count} members"
            for guild in guilds
        ]

        view = ViewPaginator(
            user=interaction.user,
            title=f"Server List ({len(guilds)} total)",
            data=data,
            per_page=10,
            colour=discord.Colour(0x944ae8)
        )

        await interaction.response.send_message(
            embed=view.format_embed(),
            view=view,
            ephemeral=True
        )
async def setup(bot):
    await bot.add_cog(Dblc(bot))