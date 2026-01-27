import time
from typing import List, Tuple, Dict, Any, Union

import discord
from discord import app_commands
from discord.ext import commands

EMBED_COLOR = discord.Color(0x337fd5)
VOTE_URL = "https://top.gg/bot/1411266382380924938/vote"
SUPPORT_URL = "https://discord.gg/VWDcymz648"
VOTE_EMOJI = "🔒"


class PrivateView(discord.ui.View):
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

class PrivateSelect(discord.ui.Select):
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

class HelpSelect(PrivateSelect):

    def __init__(self, user: discord.User, parent_view: "HelpView"):
        options = [
            discord.SelectOption(label="Home", description="Introduction & links.", value="Home", emoji="🏠"),
            discord.SelectOption(label="Moderation",
                                 description="The core moderation system of Dopamine.", value="Moderation",
                                 emoji="🚨"),
            discord.SelectOption(label="Administration & Logs",
                                 description="Essential tools for maintaining server hygiene and diagnosing the bot.", value="Administration",
                                 emoji="⚙️"),
            discord.SelectOption(label="Engagement Tools",
                                 description="Starboards, LFG posts, automated reactions, Haikus.", value="Engagement1",
                                 emoji="✨"),
            discord.SelectOption(label="Automations",
                                 description="Set-and-forget tools for consistent channel messaging and flow control.", value="Automation",
                                 emoji="🤖"),
            discord.SelectOption(label="Member Tools & Misc",
                                 description="Private notes, growth tracking, and misc. fun.", value="Utilities",
                                 emoji="📦"),
        ]
        super().__init__(user, placeholder="Choose a feature category...", options=options, min_values=1, max_values=1, custom_id="help_select")
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


class HelpView(PrivateView):

    def __init__(self, user: discord.User, embeds_map: Dict[str, discord.Embed], bot: commands.Bot = None):
        super().__init__(user, timeout=None)
        self.embeds_map = embeds_map
        self.bot = bot
        self.add_item(HelpSelect(user=user, parent_view=self))

    async def on_timeout(self):
        pass


class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.last_help_time: Dict[Union[int, str], float] = {}

    async def cog_load(self):
        embeds_map = self._build_embeds()
        self.bot.add_view(HelpView(embeds_map, self.bot))

    def _build_embeds(self) -> Dict[str, discord.Embed]:
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
            ).format(VOTE_URL=VOTE_URL, SUPPORT_URL=SUPPORT_URL)
        )

        moderation_description = (
            "Dopamine replaces traditional mute/kick/ban commands with a **12-point escalation system**. "
            "Moderators assign points, and the bot handles the math and the punishment automatically.\n\n"
            "**Punishment Logic (Customizable via `/pointvalues`):**\n"
            "• 1 Point: Warning\n"
            "• 2-5 Points: Incremental Timeouts (15m to 1h)\n"
            "• 6-11 Points: Incremental Bans (12h to 7d)\n"
            "• 12 Points: Permanent Ban\n> The points system is completely customizable, and you can customize point amounts for each action or disable an action comlpetely.\n\n"
            
            "**Core Mechanics:**\n"
            "• **Decay:** Points drop by 1 every two weeks (can be customized) if no new infractions occur.\n"
            "• **Rejoin:** Users unbanned via the bot start at 4 points (can be customized) to prevent immediate repeat offenses."
        )
        page2 = create_base_embed("Automated Moderation", moderation_description)
        page2.add_field(name="Management Commands", value=(
            "`/point` • Add points & trigger auto-punishment\n"
            "`/pardon` • Remove points from a user history\n"
            "`/points` • View current point total and history\n"
            "`/unban` • Unban a user."
        ), inline=False)
        page2.add_field(name="Nickname Moderator", value=(
            "Automatically flags and resets offensive display names to a pre-configured placeholder.\n"
            "`/nickname moderator panel` • Configure filters and placeholders\n"
            "`/nickname moderator verify` • Whitelist specific users from the filter"
        ), inline=False)

        page3 = create_base_embed(
            "Administration & Logs",
            "Essential tools for maintaining server hygiene and tracking bot activity."
        )
        page3.add_field(name="Configuration", value=(
            "**Logging:** Set your audit channel with `/logging enable`.\n"
            "**Welcoming:** Automated join messages via `/welcome`.\n"
            "**Maintenance:** Bulk delete messages using `/purge`.\n"
            "**Utility:** Use `/echo` to send messages as the bot."
        ), inline=False)
        page3.add_field(name="Bot Status", value=(
            "`/latency info` • Real-time performance metrics\n"
            "`/servercount` • Current global reach"
        ), inline=False)

        page4 = create_base_embed(
            "Engagement Tools",
            "Features designed to surface the best content, organize player groups, and automated interactions for engagement."
        )
        page4.add_field(name="Starboard & LFG", value=(
            "**Starboard:** Showcase high-quality posts based on ⭐ reactions.\n"
            "• `/starboard set_channel` | `/starboard threshold`\n\n"
            "**Looking For Group:** Create posts that ping everyone who reacts once a group is full.\n"
            "• `/lfg create` | `/lfg threshold`"
        ), inline=False)
        page4.add_field(name="Automated Interactions", value=(
            "**AutoReact:** React to new messages (or image-only posts) with up to 3 emojis.\n"
            "• `/autoreact panel setup` | `/autoreact member whitelist`\n\n"
            "**Haiku Detection:** Automatically identifies 5-7-5 syllable patterns.\n"
            "• `/haiku detection enable/disable`"
        ), inline=False)


        page5 = create_base_embed(
            "Automations",
            "Set-and-forget tools for consistent channel messaging and flow control."
        )
        page5.add_field(name="Scheduled & Sticky Messages", value=(
            "**Scheduled Messages:** Post recurring announcements (e.g., every 3 days).\n"
            "• `/scheduledmessage panel setup` | `/scheduledmessage panels`\n\n"
            "**Sticky Messages:** Keep vital info pinned at the very bottom of a channel.\n"
            "• `/sticky panel setup` | `/sticky panel modes`"
        ), inline=False)
        page5.add_field(name="Slowmode Scheduler", value=(
            "Automate channel chat speed based on time of day.\n"
            "• `/slowmode schedule start` • Set active hours\n"
            "• `/slowmode configure` • Manual override"
        ), inline=False)

        page6 = create_base_embed(
            "Member Tools & Misc",
            "Private notes, growth tracking, and miscellaneous fun."
        )
        page6.add_field(name="Tracking & Data", value=(
            "**Member Tracker:** Update a live channel message with server growth stats.\n"
            "• `/membertracker edit` | `/membertracker info`\n\n"
            "**Private Notes:** Save private notes that follow you across all servers.\n"
            "• `/note create` | `/note list` | `/note fetch`"
        ), inline=False)
        page6.add_field(name="Miscellaneous", value=(
            "`/alert` • Read developer updates and changelogs\n"
            "`/temphide` • Send encrypted (ROT13) messages that are hidden until you click a reveal button\n"
            "`/avatar` • View user profile pictures\n"
            "`/maxwithstrapon` • Transform anyone into Max Verstappen"
        ), inline=False)

        return {
            "Home": page1,
            "Moderation": page2,
            "Administration": page3,
            "Engagement": page4,
            "Automation": page5,
            "Utilities": page6,
        }

    async def _send_help_message_prefix(self, ctx: commands.Context):
        embeds_map = self._build_embeds()
        await ctx.send(embed=embeds_map["Home"], view=HelpView(embeds_map, self.bot))

    async def _send_help_message_slash(self, interaction: discord.Interaction):
        embeds_map = self._build_embeds()
        await interaction.response.send_message(embed=embeds_map["Home"], view=HelpView(embeds_map, self.bot))

    @commands.command(name="help")
    async def help_prefix(self, ctx: commands.Context):
        embeds_map = self._build_embeds()
        await ctx.send(embed=embeds_map["Home"], view=HelpView(ctx.author, embeds_map, self.bot))

    @app_commands.command(name="help", description="Show the bot help menu with category navigation.")
    async def help_slash(self, interaction: discord.Interaction):
        embeds_map = self._build_embeds()
        await interaction.response.send_message(embed=embeds_map["Home"], view=HelpView(interaction.user, embeds_map, self.bot))


async def setup(bot: commands.Bot):
    await bot.add_cog(HelpCog(bot))
