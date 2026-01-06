import time
from typing import List, Tuple, Dict, Any, Union

import discord
from discord import app_commands
from discord.ext import commands

EMBED_COLOR = discord.Color(0x337fd5)
VOTE_URL = "https://top.gg/bot/1411266382380924938/vote"
SUPPORT_URL = "https://discord.gg/VWDcymz648"
VOTE_EMOJI = "🔒"


class HelpSelect(discord.ui.Select):
    """Dropdown menu for navigating the multi-page help system."""

    def __init__(self, parent_view: "HelpView"):
        options = [
            discord.SelectOption(label="Home", description="Introduction & links.", value="Home", emoji="🏠"),
            discord.SelectOption(label="Moderation",
                                 description="The core moderation system of Dopamine.", value="Moderation",
                                 emoji="🚨"),
            discord.SelectOption(label="Server Management",
                                 description="Utilities for logging, purging, and diagnostics.", value="ServerManagement",
                                 emoji="⚙️"),
            discord.SelectOption(label="Engagement",
                                 description="Starboards, LFG posts, reactions, Haikus.", value="Engagement1",
                                 emoji="✨"),
            discord.SelectOption(label="Automations",
                                 description="Scheduled & Sticky Messages.", value="Automation",
                                 emoji="🤖"),
            discord.SelectOption(label="Utilities",
                                 description="Member tracker, notes, and more.", value="Personal",
                                 emoji="📦"),
        ]
        super().__init__(placeholder="Choose a feature category...", options=options, min_values=1, max_values=1, custom_id="help_select")
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        selection = self.values[0]
        if not self.parent_view.embeds_map or selection not in self.parent_view.embeds_map:
            if self.parent_view.bot:
                help_cog = self.parent_view.bot.get_cog('HelpCog')
                if help_cog:
                    self.parent_view.embeds_map = help_cog._build_embeds()

        embed = self.parent_view.embeds_map.get(selection)
        if not embed:
            if self.parent_view.bot:
                help_cog = self.parent_view.bot.get_cog('HelpCog')
                if help_cog:
                    self.parent_view.embeds_map = help_cog._build_embeds()
                    embed = self.parent_view.embeds_map.get(selection, self.parent_view.embeds_map.get("Home"))
        
        if embed:
            await interaction.response.edit_message(embed=embed, view=self.parent_view)
        else:
            await interaction.response.send_message("Error loading help page. Please use /help again.", ephemeral=True)


class HelpView(discord.ui.View):
    """The main view containing the navigation dropdown."""

    def __init__(self, embeds_map: Dict[str, discord.Embed], bot: commands.Bot = None):
        super().__init__(timeout=None)
        self.embeds_map = embeds_map
        self.bot = bot
        self.add_item(HelpSelect(self))

    async def on_timeout(self):
        pass


class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.last_help_time: Dict[Union[int, str], float] = {}

    async def cog_load(self):
        """Register persistent views when cog loads"""
        embeds_map = self._build_embeds()
        self.bot.add_view(HelpView(embeds_map, self.bot))

    @staticmethod
    def _cleanup_old_cooldowns(cooldown_dict: Dict[Union[int, str], float], max_age_seconds: int):
        """Remove cooldown entries older than max_age_seconds to prevent memory leaks."""
        current_time = time.time()
        to_remove = [key for key, timestamp in cooldown_dict.items() if current_time - timestamp > max_age_seconds]
        for key in to_remove:
            del cooldown_dict[key]

    def _build_embeds(self) -> Dict[str, discord.Embed]:
        """Create and return all help embeds keyed by their selection value."""
        icon_url = self.bot.user.display_avatar.url if self.bot.user else None

        def create_base_embed(title: str, description: str) -> discord.Embed:
            embed = discord.Embed(
                title=title,
                description=description,
                color=EMBED_COLOR
            )
            embed.set_author(name=f"Dopamine Help | {title}", icon_url=icon_url)
            embed.set_footer(text=f"Navigate using the dropdown below.")
            return embed

        page1 = create_base_embed(
            "Help Menu",
            (
                "**Welcome to Dopamine,** the Discord bot that hits just as good as the real thing! 😉 "
                "I'm your all-in-one moderation and utility bot, here to help keep your server running smoothly. ^_^\n\n"
                "-# [**__Vote__**]({VOTE_URL}) • [**__Support Server__**]({SUPPORT_URL})"
            ).format(VOTE_EMOJI=VOTE_EMOJI, VOTE_URL=VOTE_URL, SUPPORT_URL=SUPPORT_URL)
        )

        moderation_description = (
            "Dopamine is a bot that offers moderation with a customizable, 12-point system. Instead of individual "
            "commands for mute, kick, and ban, moderators simply assign points, and the bot automatically "
            "applies the correct punishment based on your server's configured thresholds.\n\n"
            "### Current Default Point Actions (Customizable via `/pointsvalue`)\n"
            "Punishments escalate as points increase:\n"
            "* **1 Point:** Warning\n"
            "* **2 Points:** 15-Minute Timeout\n"
            "* **3 Points:** 30-Minute Timeout\n"
            "* **4 Points:** 45-Minute Timeout\n"
            "* **5 Points:** 60-Minute Timeout\n"
            "* **6 Points:** 12-Hour Ban\n"
            "* **7 Points:** 12-Hour Ban\n"
            "* **8 Points:** 1-Day Ban\n"
            "* **9 Points:** 3-Day Ban\n"
            "* **10 Points:** 7-Day Ban\n"
            "* **11 Points:** 7-Day Ban\n"
            "* **12 Points:** Permanent Ban\n\n"
            "**Decay System:** Points automatically decay (subtract 1) every two weeks if a user receives no new punishments.\n"
            "**Rejoin Policy:** Permanently banned users who are unbanned and rejoin start with **4 points** instead of zero."
        )

        page2 = create_base_embed("Core Moderation & Points", moderation_description)
        page2.add_field(name="Commands", value="""
            `/point <user> <points> [reason]` - Add points and trigger auto-punishment.
            `/pardon <user> <points> [reason]` - Remove points from a user.
            `/points <user>` - Inspect a user's current point total and history.
            `/pointsvalue` - View and customize the punishment thresholds for your server.
            `/unban <user>` - Unban a user.
        """, inline=False)

        page3 = create_base_embed(
            "Server Management",
            "Essential utilities for server management, logging, and general administration."
        )
        page3.add_field(name="Administration", value="""
            `/purge <amount>` - Delete messages in bulk (logs to log channel).
            `/setlog <channel>` - Configure the channel for moderation and bot action logs.
            `/welcome <channel>` - Configure/disable welcome messages for new members.
            `/echo <message>` - Make the bot send a message (Mod Only).
        """, inline=False)
        page3.add_field(name="Fun & Miscellaneous", value=f"""
            `/latency info` - Display advanced, real-time latency and related info for the bot.
            `/avatar <user>` - Display the full avatar of a user.
        """, inline=False)

        page4 = create_base_embed(
            "Engagement (Starboard & LFG)",
            "Interactive features designed to highlight great content, organize groups, and interact with messages."
        )
        page4.add_field(name="Starboard & LFG", value="""
            **Starboard:** Watches for ⭐ reactions and posts high-engagement messages.
            `/starboard setchannel <channel>` - Set the Starboard destination channel.
            `/starboard threshold <count>` - Set the minimum number of ⭐ reactions required to post.

            **LFG:** Create group posts that mention all reactors once a threshold is met.
            `/lfg create <post_details>` - Create a new LFG post.
            `/lfg threshold <count>` - Set the reaction count required to trigger the group ping.
        """, inline=False)
        page4.add_field(name=f"({VOTE_EMOJI}) Reactions and Haikus", value=f"""
            **AutoReact Panels:** Automatically react to new messages with up to 3 defined emojis.
            `/autoreact panel setup` - Create/Edit the panel.
            `/autoreact member whitelist` - Only react to messages from specific whitelisted users.
            `/autoreact image only` - Only react if the message contains an image/embed.

            **Haiku Detection:** Detects Haikus. Yep, that's what it does.
            `/haiku detection enable/disable` - Turn detection on or off in a single channel.
        """, inline=False)

        page5 = create_base_embed(
            "Automations (Scheduled & Sticky)",
            "Set-and-forget tools for keeping channels active and information visible."
        )
        page5.add_field(name="Scheduled Messages", value="""
            Set up messages to post repeatedly (e.g., every 3 days, every 2 weeks).
            `/scheduledmessage panel setup` - Open the UI to create a new message (name, frequency, content).
            `/scheduledmessage panel delete` - Delete a scheduled message (uses autocomplete).
            `/scheduledmessage panel start/stop` - Toggle activity.
            `/scheduledmessage panels` - List all configured messages.
        """, inline=False)
        page5.add_field(name=f"({VOTE_EMOJI}) Sticky Messages", value=f"""
            Keeps a message pinned to the bottom of the channel, reposting it whenever a new message is sent.
            `/sticky panel setup` - Open the UI to define the message content (title, description, image).
            `/sticky panel start/stop` - Activate/deactivate the sticky message in a channel.
            `/sticky panel modes` - Toggle Image-Only Mode and Member Whitelist Mode.
            `/sticky panel delete` - Delete a configuration.
        """, inline=False)

        page6 = create_base_embed(
            f"Utilities",
            "Tools to enhance the Discord user/server experience."
        )

        page6.add_field(name=f"({VOTE_EMOJI}) Member Tracker", value=f"""
                    Tracks server member count, updating a channel message every 5 minutes (useful for growth goals).
                    `/membertracker enable/disable` - Turn the tracker on/off.
                    `/membertracker edit` - **Modal UI** to set goals, customize format, and embed color.
                    `/membertracker info` - View format token documentation.
                """, inline=False)
        page6.add_field(name=f"({VOTE_EMOJI}) Notes", value=f"""
            Securely store personal notes accessible globally across all servers I'm in.
            `/note create` - Use a **Modal UI** to save a new note.
            `/note fetch/list/delete` - Retrieve, list, or remove notes (uses dynamic autocomplete).
        """, inline=False)
        page6.add_field(name="Fun & Misc.", value=f"""
                    `/temphide <message>` - Send a hidden, ROT13-encoded message that only the recipient can reveal using a button.
                    ({VOTE_EMOJI}) `/maxwithstrapon` - Ignore the command's name. Turn anyone into Max Verstappen!
                """, inline=False)

        return {
            "Home": page1,
            "Moderation": page2,
            "ServerManagement": page3,
            "Engagement1": page4,
            "Automation": page5,
            "Personal": page6,
        }

    async def _send_help_message_prefix(self, ctx: commands.Context):
        embeds_map = self._build_embeds()
        await ctx.send(embed=embeds_map["Home"], view=HelpView(embeds_map, self.bot))

    async def _send_help_message_slash(self, interaction: discord.Interaction):
        embeds_map = self._build_embeds()
        await interaction.response.send_message(embed=embeds_map["Home"], view=HelpView(embeds_map, self.bot))

    @commands.command(name="help")
    async def help_prefix(self, ctx: commands.Context):
        """Prefix help command with per-guild cooldown."""
        cooldown_seconds = 30
        now = time.time()
        guild_id_or_user_id = ctx.guild.id if ctx.guild else ctx.author.id
        last_time = self.last_help_time.get(guild_id_or_user_id, 0)

        if now - last_time < cooldown_seconds:
            remaining = int(cooldown_seconds - (now - last_time))
            cooldown_embed = discord.Embed(
                title="Slow down!",
                description=f"This command is on cooldown. Try again in **{remaining}** seconds.",
                color=discord.Color.red()
            )

            try:
                await ctx.message.delete()
            except discord.Forbidden:
                pass

            await ctx.send(embed=cooldown_embed, delete_after=5)
            return

        self._cleanup_old_cooldowns(self.last_help_time, 60)
        self.last_help_time[guild_id_or_user_id] = now

        await self._send_help_message_prefix(ctx)

    @app_commands.command(name="help", description="Show the bot help menu with category navigation.")
    async def help_slash(self, interaction: discord.Interaction):
        cooldown_seconds = 30
        now = time.time()
        guild_id_or_user_id = interaction.guild.id if interaction.guild else interaction.user.id
        last_time = self.last_help_time.get(guild_id_or_user_id, 0)

        if now - last_time < cooldown_seconds:
            remaining = int(cooldown_seconds - (now - last_time))
            cooldown_embed = discord.Embed(
                title="Slow down!",
                description=f"This command is on cooldown. Try again in **{remaining}** seconds.",
                color=discord.Color.red()
            )

            await interaction.response.send_message(
                embed=cooldown_embed,
                ephemeral=True
            )
            return

        self._cleanup_old_cooldowns(self.last_help_time, 60)
        self.last_help_time[guild_id_or_user_id] = now

        await self._send_help_message_slash(interaction)


async def setup(bot: commands.Bot):
    await bot.add_cog(HelpCog(bot))
