from __future__ import annotations

from typing import Dict, Union

import discord
from beacon import beacon_commands
from discord.ext import commands

EMBED_COLOR = discord.Color(0x944ae8)
VOTE_URL = "https://top.gg/bot/1411266382380924938/vote"
SUPPORT_URL = "https://discord.gg/VWDcymz648"
VOTE_EMOJI = "🔒"


class PrivateView(discord.ui.View):
    def __init__(self, user: discord.User | None = None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.user and interaction.user.id != self.user.id:
            await interaction.response.send_message(
                "This isn't for you!",
                ephemeral=True
            )
            return False
        return True

class PrivateSelect(discord.ui.Select):
    def __init__(self, user: discord.User | None = None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.user and interaction.user.id != self.user.id:
            await interaction.response.send_message(
                "This isn't for you!",
                ephemeral=True
            )
            return False
        return True

class HelpSelect(PrivateSelect):

    def __init__(self, user: discord.User | None, parent_view: HelpView):
        options = [
            discord.SelectOption(label="Home", description="Introduction & links.", value="Home", emoji="🏠"),
            discord.SelectOption(label="Moderation",
                                 description="The core moderation system of Dopamine.", value="Moderation",
                                 emoji="🚨"),
            discord.SelectOption(label="Administration & Logs",
                                 description="Essential tools for maintaining server hygiene and diagnosing the bot.", value="Administration",
                                 emoji="⚙️"),
            discord.SelectOption(label="Engagement Tools",
                                 description="Giveaways, Starboards, LFG posts, automated reactions, Haikus.", value="Engagement1",
                                 emoji="✨"),
            discord.SelectOption(label="Automations",
                                 description="Set-and-forget tools for consistent channel messaging and flow control.", value="Automation",
                                 emoji="🤖"),
            discord.SelectOption(label="Member Tools & Misc",
                                 description="Private notes, growth tracking, and misc. fun.", value="Utilities",
                                 emoji="📦"),
        ]
        super().__init__(user=user, placeholder="Choose a feature category...", options=options, min_values=1, max_values=1, custom_id="help_select")
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

    def __init__(self, user: discord.User | None, embeds_map: Dict[str, discord.Embed], bot: commands.Bot | None = None):
        super().__init__(user=user, timeout=None)
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
        await self.bot.db.wait_ready()
        embeds_map = self._build_embeds()
        self.bot.add_view(HelpView(None, embeds_map, self.bot))

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
            "Dopamine replaces traditional mute/kick/ban commands with a completely customizable 5-strike escalation system. "
            "Moderators assign warnings, and the bot handles the math and the punishment automatically based on what punishment you've set for the specific warning count.\n\n"
            "**Punishment Logic (Customizable via `/moderation dashboard`):**\n"
            "• 1 Warning: Official Warning\n"
            "• 2 Warnings: 60-minute mute/timeout\n"
            "• 3 Warnings: 12-hour ban\n"
            "• 4 Warnings: 7-day Ban\n• 5 Warnings: Permanent Ban\n> The moderation system is completely customizable, and you can customize warnings amounts for each action, create new actions with custom punishment, durations, and warning counts, and delete existing ones.\n\n"
            
            "**Core Mechanics:**\n"
            "• **Decay:** Warnings drop by 1 every two weeks (can be customized) if no new infractions occur.\n"
            "• **Rejoin:** Users unbanned via the bot start at 4 warnings (can be customized) to prevent immediate repeat offenses."
        )
        page2 = create_base_embed("Automated Moderation", moderation_description)
        page2.add_field(name="Management Commands", value=(
            "`/warn` • Add warnings & trigger auto-punishment\n"
            "`/pardon` • Remove warnings/points from a user\n"
            "`/warnings` • View current warnings/points total\n"
            "`/case history` • View full infraction history for a user\n"
            "`/case view` • View a specific case by ID\n"
            "`/case all` • Browse all active warnings/points with live leaderboard\n"
            "`/case delete` • Delete a case and adjust warnings/points\n"
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
            "**Goodbyes:** Automated goodbye messages via `/goodbye`.\n"
            "**Maintenance:** Bulk delete messages using `/purge`.\n"
            "**Utility:** Use `/echo` to send messages as the bot."
        ), inline=False)
        page3.add_field(name="Bot Status", value=(
            "`/ping` - Real-time performance metrics\n"
            "`/latency graph` - See a graph of API latency\n"
            "`/servercount` - Current total server count"
        ), inline=False)

        page4 = create_base_embed(
            "Engagement Tools",
            "Features designed to surface the best content, organize player groups, and automated interactions for engagement."
        )
        page4.add_field(name="Giveaways", value=(
            "Create completely customizable giveaways. Create, store, and share giveaway templates.\n"
            "• `/giveaway create` | `/giveaway template`"
        ), inline=False)
        page4.add_field(name="Discordphone", value=(
            "Talk to users in other servers!\n"
            "• `/discordphone start` | `/discordphone hangup` | `/discordphone report`"
        ), inline=False)
        page4.add_field(name="Starboard & LFG", value=(
            "**Starboard:** Showcase high-quality posts based on ⭐ reactions.\n"
            "**Looking For Group:** Create posts that ping everyone who reacts once a group is full.\n"
            "• `/starboard` | `/lfg create` | `/lfg threshold`"
        ), inline=False)
        page4.add_field(name="Automated Interactions", value=(
            "**AutoReact:** React to new messages (or image-only posts) with up to 3 emojis.\n"
            "• `/autoreact`\n\n"
            "**Haiku Detection:** Automatically identifies 5-7-5 syllable patterns.\n"
            "• `/haiku detection enable/disable`"
        ), inline=False)
        page4.add_field(name="Accidentally Factorial", value=(
            "Detects factorials (any number n followed by exclamation mark `n!`)\n"
            "• `/factorial`"
        ), inline=False)


        page5 = create_base_embed(
            "Automations",
            "Set-and-forget tools for consistent channel messaging and flow control."
        )
        page5.add_field(name="Repeating & Sticky Messages", value=(
            "**Repeating Messages:** Post recurring announcements (e.g., every 3 days).\n"
            "• `/repeating message`\n\n"
            "**Sticky Messages:** Keep vital info pinned at the very bottom of a channel.\n"
            "• `/sticky message`"
        ), inline=False)
        page5.add_field(name="Slowmode Scheduler", value=(
            "Automate channel chat speed based on time of day.\n"
            "• `/slowmode schedule start` • Set active hours\n"
            "• `/slowmode configure` • Manual override"
        ), inline=False)
        page5.add_field(name="Autopublish", value=(
            "Automate publishing of messages in a Discord announcement channel.\n"
            "• `/autopublish enable` • Enable in a specific channel\n"
            "• `/autopublish disable` • Disable in a specific channel"
        ), inline=False)

        page6 = create_base_embed(
            "Member Tools & Misc",
            "Private notes, growth tracking, and miscellaneous fun."
        )
        page6.add_field(name="Tracking & Data", value=(
            "**Member Tracker:** Update a live channel message with server growth stats.\n"
            "• `/member tracker edit`\n\n"
            "**Private Notes:** Save private notes that follow you across all servers.\n"
            "• `/note create` | `/note list` | `/note get` | `/note edit`"
        ), inline=False)
        page6.add_field(name="Selfpurge", value=(
            "**Allow users to purge their own messages without mod permissions after a 24-hr buffer period after triggering the command (NOTE: Opt-in. Members can't use this by default. An admin has to enable it first.)**\n"
            "• `/selfpurge start` | `/selfpurge cancel` | `/selfpurge enable` | `/selfpurge modcancel` | `/selfpurge disable`"
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
        await ctx.send(embed=embeds_map["Home"], view=HelpView(ctx.author, embeds_map, self.bot))

    async def _send_help_message_slash(self, interaction: discord.Interaction):
        embeds_map = self._build_embeds()
        await interaction.response.send_message(embed=embeds_map["Home"], view=HelpView(interaction.user, embeds_map, self.bot))

    @commands.command(name="help")
    async def help_prefix(self, ctx: commands.Context):
        embeds_map = self._build_embeds()
        await ctx.send(embed=embeds_map["Home"], view=HelpView(ctx.author, embeds_map, self.bot))

    @beacon_commands.command(name="help", description="Show the bot help menu with category navigation.")
    async def help_slash(self, interaction: discord.Interaction):
        embeds_map = self._build_embeds()
        await interaction.response.send_message(embed=embeds_map["Home"], view=HelpView(interaction.user, embeds_map, self.bot))


async def setup(bot: commands.Bot):
    await bot.add_cog(HelpCog(bot))
