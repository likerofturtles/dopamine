from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.machinery import all_suffixes
from typing import Optional, List, Dict, Set
import discord
from discord import app_commands, Interaction
import re
from discord.ext import commands, tasks
import random
import asyncio
import aiosqlite
from datetime import datetime, timezone

from config import GDB_PATH
from utils.time import get_duration_to_seconds, get_now_plus_seconds_unix

@dataclass
class GiveawayDraft:
    guild_id: int
    channel_id: int
    prize: str
    winners: int
    end_time: int # Unix timestamp
    host_id: Optional[int] = None
    required_roles: List[int] = None
    required_behaviour: int = 0 # 0 = All, 1 = One
    blacklisted_roles: List[int] = None
    extra_entries: List[int] = None
    winner_role: Optional[int] = None
    image: Optional[str] = None
    thumbnail: Optional[str] = None
    color: str = "discord.Color.blue()"

class GiveawayEditSelect(discord.ui.Select):
    def __init__(self, cog, draft: GiveawayDraft, parent_view):
        self.cog = cog
        self.draft = draft
        self.parent_view = parent_view
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

    async def callback(self, interaction: discord.Interaction, value: str):
        if value in ["image", "thumbnail", "color"]:
            await interaction.response.send_modal(GiveawayVisualsModal(value, self.draft))
        elif value == "behavior":
            new_view = discord.ui.View()
            new_view.add_item(BehaviorSelect(self.draft))
            await interaction.response.send_message("Change required role behaviour:", view=new_view, ephemeral=True)
        elif value == "extra":
            new_view = discord.ui.View()
            trait = "extra entries role"
            new_view.add_item(RoleSelectView(trait, self.draft))
            await interaction.response.send_message("Choose roles which will give extra entries:", view=new_view,
                                                    ephemeral=True)
        elif value == "required":
            new_view = discord.ui.View()
            trait = "Required Roles"
            new_view.add_item(RoleSelectView(trait, self.draft))
            await interaction.response.send_message("Choose required roles to participate:", view=new_view,
                                                    ephemeral=True)
        elif value == "winner_role":
            new_view = discord.ui.View()
            trait = "Winners' Role"
            new_view.add_item(WinnerRoleSelectView(trait, self.draft))
            await interaction.response.send_message("Choose role to be given to winner(s):", view=new_view,
                                                    ephemeral=True)
        elif value == "blacklist":
            new_view = discord.ui.View()
            trait = "Blacklisted Roles"
            new_view.add_item(RoleSelectView(trait, self.draft))
            await interaction.response.send_message("Choose roles that can't participate:", view=new_view,
                                                    ephemeral=True)
        elif value == "host":
            new_view = discord.ui.View()
            new_view.add_item(MemberSelectView(self.draft))
            await interaction.response.send_message("Choose the host for this giveaway:", view=new_view, ephemeral=True)

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
            title=f"👤 Participants for **{self.prize}**",
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
        self.draft.required_behaviour = int(self.values[0])
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
        embed = self.cog.create_giveaway_embed(self.draft)

        channel = self.cog.bot.get_channel(self.draft.channel_id)
        if not channel:
            try:
                channel = await self.cog.bot.fetch_channel(self.draft.channel_id)
            except (discord.Forbidden, discord.NotFound):
                return await interaction.response.send_message("I searched far and wide, but I can't find the channel chosen for the giveaway!\n\nEnsure that I have the necessary permissions.", ephemeral=True)

        view = GiveawayJoinView(self.cog)

        msg = await channel.send(embed=embed, view=view)

        giveaway_id = await self.cog.save_giveaway(self.draft, msg.id)

        embed.set_footer(text=f"ID: {giveaway_id}")
        await msg.edit(embed=embed)

        embed = discord.Embed(description=f"Giveaway started successfully in {channel.mention}!",
                              colour=discord.Colour.green())
        embed.set_footer(text=f"ID: {giveaway_id}")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        self.stop()

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.gray)
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = GiveawayEditSelect(self, self.cog, self.draft)
        await interaction.response.send_message(embed=discord.Embed(title="Edit Giveaway", description="Select what you want to edit using the dropdown below.", colour=discord.Colour.blue()), view=view, ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=discord.Embed(title="Giveaway Creation Cancelled."), view=None)
        self.stop()

class MemberSelectView(discord.ui.View):
    def __init__(self, draft: GiveawayDraft):
        super().__init__(timeout=300)
        self.draft = draft
        self.select = discord.ui.UserSelect(placeholder="Pick a host...", min_values=1, max_values=1)
        self.select.callback = self.callback
        self.add_item(self.select)

    async def callback(self, interaction: discord.Interaction):
        self.draft.host_id = self.select.values[0].id
        await interaction.response.send_message(f"Giveaway host updated to {self.select.values[0].mention}", ephemeral=True)


class RoleSelectView(discord.ui.View):
    def __init__(self, key: str, label: str, draft: GiveawayDraft):
        super().__init__(timeout=300)
        self.key = key
        self.draft = draft
        self.select = discord.ui.RoleSelect(placeholder=f"Pick {label}...", min_values=1, max_values=10)
        self.select.callback = self.callback
        self.add_item(self.select)

    async def callback(self, interaction: discord.Interaction):
        role_ids = [role.id for role in self.select.values]
        if self.key == "extra":
            self.draft.extra_entries = role_ids
        elif self.key == "required":
            self.draft.required_roles = role_ids
        elif self.key == "blacklist":
            self.draft.blacklisted_roles = role_ids
        elif self.key == "winner_role":
            self.draft.winner_role = role_ids[0]

        await interaction.response.send_message(f"Updated {self.key} successfully!", ephemeral=True)


class WinnerRoleSelectView(discord.ui.View):
    def __init__(self, key: str, label: str, draft: GiveawayDraft):
        super().__init__(timeout=300)
        self.key = key
        self.draft = draft
        self.select = discord.ui.RoleSelect(placeholder=f"Pick {label}...", min_values=1, max_values=10)
        self.select.callback = self.callback
        self.add_item(self.select)

    async def callback(self, interaction: discord.Interaction):
        role_ids = [role.id for role in self.select.values]
        if self.key == "extra":
            self.draft.extra_entries = role_ids
        elif self.key == "required":
            self.draft.required_roles = role_ids
        elif self.key == "blacklist":
            self.draft.blacklisted_roles = role_ids
        elif self.key == "winner_role":
            self.draft.winner_role = role_ids[0]

        await interaction.response.send_message(f"Updated {self.key} successfully!", ephemeral=True)



class GiveawayJoinView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        emoji="🎉",
        label="TOBEIMPLEMENTED",
        style=discord.ui.ButtonStyle.blurple,
        custom_id="persistent_giveaway_join"
    )
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.message.embeds:
            return await interaction.response.send_message("Uh-oh! I'm afraid that the message you interacted with doesn't exist anymore :3", ephemeral=True)

        footer_text = interaction.message.embeds[0].footer.text
        try:
            giveaway_id = int(footer_text.split(": ")[1])
        except (IndexError, ValueError):
            return await interaction.response.send_message("Uh-oh! I couldn't find the Giveaway ID. Perhaps try again?", ephemeral=True)

        g = self.cog.giveaway_cache.get(giveaway_id)

        if not g or g['ended'] == 1:
            return await interaction.response.send_message("Uh-oh! I'm afraid that this giveaway has already ended!", ephemeral=True)

        if g['blacklisted_roles']:
            blacklisted_ids = [int(r) for r in g['blacklisted_roles'].split(",")]
            if any(role.id in blacklisted_ids for role in interaction.user.roles):
                return await interaction.response.send_message("Uh-oh! You cannot join this giveaway because you have a blacklisted role.", ephemeral=True)

        if g['required_roles']:
            req_ids = [int(r) for r in g['required_roles'].split(",")]
            user_role_ids = [role.id for role in interaction.user.roles]

            if g['req_behaviour'] == 0:
                if not all(r in user_role_ids for r in req_ids):
                    return await interaction.response.send_message("Uh-oh! You cannot join this giveaway because you don't have all the required roles.", ephemeral=True)

            else:
                if not any(r in user_role_ids for r in req_ids):
                    return await interaction.response.send_message("Uh-oh! You cannot join this giveaway because you don't have one of the required roles.", ephemeral=True)

        participants = self.cog.participant_cache.get(giveaway_id, set())

        async with self.cog.acquire_db() as db:
            if interaction.user.id in participants:
                participants.remove(interaction.user.id)
                await db.execute("DELETE FROM giveaway_participants WHERE giveaway_id = ? AND user_id = ?",
                                 (giveaway_id, interaction.user.id))
                msg = "You have successfully left the giveaway."
            else:
                participants.add(interaction.user.id)
                await db.execute("INSERT INTO giveaway_participants (guild_id, giveaway_id, user_id) VALUES (?, ?, ?)",
                                 (interaction.guild_id, giveaway_id, interaction.user.id))
                msg = "🎉 You have successfully entered the giveaway!"
            await db.commit()

        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(
        label="👤 Participants",
        style=discord.ButtonStyle.gray,
        custom_id="persistent_giveaway_list"
    )
    async def list_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.message.embeds:
            return await interaction.response.send_message("Uh-oh! I'm afraid that the message you interacted with doesn't exist anymore :3", ephemeral=True)

        footer_text = interaction.message.embeds[0].footer.text

        try:
            giveaway_id = int(footer_text.split(": ")[1])
        except (IndexError, ValueError):
            return await interaction.response.send_message("Uh-oh! I couldn't parse Giveaway ID. Maybe try again?", ephemeral=True)

        participant_set = self.cog.participant_cache.get(giveaway_id, set())
        participants = list(participant_set)

        prize = self.cog.giveaway_cache.get(giveaway_id)['prize']

        if not participants:
            return await interaction.response.send_message("There are currently no participants in this giveaway!",
                                                           ephemeral=True)
        view = ParticipantPaginator(participants, prize)
        await interaction.response.send_message(embed=view.get_embed(), view=view, ephemeral=True)

class Giveaways(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.giveaway_cache: Dict[int, dict] = {}
        self.participant_cache: Dict[int, Set[int]] = {}
        self.db_pool: Optional[asyncio.Queue[aiosqlite.connection]] = None
        self.check_giveaways.start()

    async def cog_load(self):
        await self.init_pools()
        await self.init_db()
        await self.populate_caches()
        self.bot.add_view(GiveawayJoinView(self))

    async def cog_unload(self):
        self.check_giveaways.cancel()
        if self.db_pool is not None:
            while not self.db_pool.empty():
                conn = await self.db_pool.get()
                await conn.close()

    async def init_pools(self, pool_size: int = 5):
        if self.db_pool is None:
            self.db_pool = asyncio.Queue(maxsize = pool_size)
            for _ in range (pool_size):
                conn = await aiosqlite.connect(
                    GDB_PATH,
                    timeout=5,
                    isolation_level=None,
                )
                await conn.execute("PRAGMA busy_timeout=5000")
                await conn.execute("PRAGMA journal_mode=WAL")
                await conn.execute ("PRAGMA synchronous = NORMAL")
                await conn.commit()
                await self.db_pool.put(conn)

    @asynccontextmanager
    async def acquire_db(self):
        conn = await self.db_pool.get()
        try:
            yield conn
        finally:
            await self.db_pool.put(conn)

    async def init_db(self):
        async with self.acquire_db() as db:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS giveaways (
                    guild_id INTEGER,
                    giveaway_id INTEGER,
                    channel_id INTEGER,
                    message_id INTEGER,
                    prize TEXT,
                    winners_count INTEGER,
                    end_time INTEGER,
                    host_id INTEGER,
                    required_roles TEXT,
                    req_behaviour INTEGER,
                    blacklisted_roles TEXT,
                    extra_entry_roles TEXT,
                    winner_role_id INTEGER,
                    image_url TEXT,
                    thumbnail_url TEXT,
                    color TEXT,
                    ended INTEGER DEFAULT 0,
                    PRIMARY KEY (guild_id, giveaway_id)
                )
            ''')

            await db.execute('''
                CREATE TABLE IF NOT EXISTS giveaway_participants (
                    guild_id INTEGER,
                    giveaway_id INTEGER,
                    user_id INTEGER,
                    PRIMARY KEY (guild_id, giveaway_id, user_id)
                )
            ''')

            await db.execute('''
                CREATE TABLE IF NOT EXISTS giveaway_winners (
                    giveaway_id INTEGER,
                    user_id INTEGER,
                    PRIMARY KEY (giveaway_id, user_id)
                )
            ''')

            await db.commit()

    async def populate_caches(self):
        self.giveaway_cache.clear()
        self.participant_cache.clear()

        async with self.acquire_db() as db:
            async with db.execute("SELECT * FROM giveaways WHERE ended = 0") as cursor:
                rows = await cursor.fetchall()
                columns = [column[0] for column in cursor.description]
                for row in rows:
                    data = dict(zip(columns, row))
                    giveaway_id = data["giveaway_id"]
                    self.giveaway_cache[giveaway_id] = data
                    self.participant_cache[giveaway_id] = set()

            if self.giveaway_cache:
                placeholders = ", ".join(['?'] * len(self.giveaway_cache))
                query = f"SELECT giveaway_id, user_id FROM giveaway_participants WHERE giveaway_id in ({placeholders})"
                async with db.execute(query, list(self.giveaway_cache.keys())) as cursor:
                    participant_rows = await cursor.fetchall()
                    for giveaway_id, user_id in participant_rows:
                        if giveaway_id in self.participant_cache:
                            self.participant_cache[giveaway_id].add(user_id)

    @tasks.loop(seconds=10)
    async def check_giveaways(self):
        now = int(datetime.now(timezone.utc).timestamp())

        to_end = [
            (g['giveaway_id'], g['guild_id'])
            for g in self.giveaway_cache.values()
            if g['end_time'] <= now and g['ended'] == 0
        ]

        for giveaway_id, guild_id in to_end:
            await self.end_giveaway(giveaway_id, guild_id)

    async def end_giveaway(self, giveaway_id: int, guild_id: int):
        g = self.giveaway_cache.get(giveaway_id)
        if not g:
            async with self.acquire_db() as db:
                async with db.execute("SELECT * FROM giveaways WHERE giveaway_id = ? AND guild_id = ?", (giveaway_id, guild_id)) as cursor:
                    rows = await cursor.fetchall()
                    if rows: return
                    columns = [column[0] for column in cursor.description]
                    g = dict(zip(columns, rows))

        raw_participants = list(self.participant_cache.get(giveaway_id, set()))

        if not raw_participants:
            async with self.acquire_db() as db:
                async with db.execute("SELECT user_id FROM giveaway_participants WHERE giveaway_id = ?",
                                      (giveaway_id,)) as cursor:
                    rows = await cursor.fetchall()
                    raw_participants = [r[0] for r in rows]

        pool = []

        extra_roles_str = g.get('extra_entry_roles', '')
        extra_roles_list = [int(r) for r in extra_roles_str.split(',')] if extra_roles_str else []

        guild = self.bot.get_guild(guild_id)

        for user_id in raw_participants:
            pool.append(user_id)

            if guild and extra_roles_list:
                member = guild.get_member(user_id)
                if member:
                    for role_id in extra_roles_list:
                        if any(role.id == role_id for role in member.roles):
                            pool.append(user_id)

        if not pool:
            channel = self.bot.get_channel(g['channel_id'])
            if channel:
                await channel.send(embed=discord.Embed(title="Giveaway Ended", description=f"Giveaway for **{g['prize']}** ended with no participants.", colour=discord.Colour.red()))
            await self.mark_as_ended(giveaway_id, guild_id)
            return

        winner_count = min(len(pool), g['winner_count'])
        random.shuffle(pool)

        winners = []
        for winner_id in pool:
            if winner_id not in winners:
                winners.append(winner_id)

            if len(winners) == winner_count:
                break

        await self.mark_as_ended(giveaway_id, guild_id)
        async with self.acquire_db() as db:
            for winner_id in winners:
                await db.execute("INSERT INTO giveaway_winners (giveaway_id, user_id) VALUES (?, ?)", (giveaway_id, winner_id))
                await db.commit()

        guild = self.bot.get_guild(guild_id)
        channel = guild.get_channel(g['channel_id']) if guild else None
        if channel:
            try:
                msg = await channel.fetch_message(g['message_id'])
                embed_embed = self.create_embed_from_cache(g, winners=winners)
                await msg.edit(embed=embed_embed, view=None)

                mention_str = ", ".join([f"<@{w}>" for w in winners])
                await channel.send (f"🎉 Congratulations to: {mention_str} for winning **{g['prize']}!**")

                winner_role_id = g.get('winner_role_id')
                if winner_role_id:
                    role = guild.get_role(winner_role_id)
                    if role:
                        for winner_id in winners:
                            member = guild.get_member(winner_id)
                            if member: await member.add_roles(role)

            except Exception:
                    pass

    async def mark_as_ended(self, giveaway_id: int, guild_id: int):
        if giveaway_id in self.giveaway_cache:
            self.giveaway_cache[giveaway_id]['ended'] = 1
        if giveaway_id in self.participant_cache:
            self.participant_cache.pop(giveaway_id, None)
        async with self.acquire_db() as db:
            await db.execute("UPDATE giveaways SET ended = 1 WHERE giveaway_id = ? and guild_id = ?", (giveaway_id, guild_id))
            await db.commit()

    def create_embed_from_cache(self, row, winners=None):
        end_ts = row['end_time']

        embed = discord.Embed(
            title="GIVEAWAY ENDED",
            description=f"Ended at: **<t:{end_ts}:R>**",
            colour=discord.Colour.red()
        )
        embed.add_field(name="Winners", value=", ".join([f"<@{w}>" for w in winners]), inline=False)
        return embed

    def create_giveaway_embed(self, draft: GiveawayDraft, ended: bool = False):
        title_text = "GIVEAWAY ENDED" if ended else f"{draft.prize}"
        embed_color = discord.Color.red() if ended else discord.Color.blue()

        if not ended and draft.color:
            color_str = draft.color.lower()
            try:
                if color_str.startswith("#"):
                    embed_color = discord.Color.from_str(color_str)
                elif hasattr(discord.Color, color_str):
                    embed_color = getattr(discord.Color, color_str)()
            except (ValueError, TypeError):
                pass

        embed = discord.Embed(
            title=f"{title_text}",
            description=f"Click the 🎉 button below to enter this giveaway!\n\n"
                        f"Winners: **{draft.winners}**"
                        f"Ends: **<t:{draft.end_time}:R>**",
            colour=embed_color
        )
        if draft.host_id:
            embed = discord.Embed(
                title=f"{draft.prize}",
                description=f"Click the 🎉 button below to enter this giveaway!\n\n"
                            f"Hosted By: <@{draft.host_id}>\n"
                            f"Winners: **{draft.winners}**\n"
                            f"Ends: **<t:{draft.end_time}:R>**",
                colour=embed_color)

        if draft.required_roles:
            role_mentions = ", ".join([f"<@&{r}>" for r in draft.required_roles])
            mode = "all of the following" if draft.required_behaviour == 0 else "one of the following"
            embed.add_field(name="Requirements", value=f"Must have **{mode}**: {role_mentions}", inline=False)

        if draft.image:
            embed.set_image(url=draft.image)
        if draft.thumbnail:
            embed.set_thumbnail(url=draft.thumbnail)

        embed.set_footer(text="ID: [Giveaway ID will be generated and shown here once you click start.]")

        return embed

    async def save_giveaway(self, draft: GiveawayDraft, message_id: int):
        req_roles = ",".join(map(str, draft.required_roles)) if draft.required_roles else ""
        black_roles = ",".join(map(str, draft.blacklisted_roles)) if draft.blacklisted_roles else ""
        extra_roles = ",".join(map(str, draft.extra_entries)) if draft.extra_entries else ""

        giveaway_id = int(discord.utils.utcnow().timestamp()) + random.randint(1, 69)

        data = {
            "guild_id": draft.guild_id,
            "giveaway_id": giveaway_id,
            "channel_id": draft.channel_id,
            "message_id": message_id,
            "prize": draft.prize,
            "winners_count": draft.winners,
            "end_time": draft.end_time,
            "host_id": draft.host_id,
            "required_roles": req_roles,
            "req_behaviour": draft.required_behaviour,
            "blacklisted_roles": black_roles,
            "extra_entry_roles": extra_roles,
            "winner_role_id": draft.winner_role,
            "image_url": draft.image,
            "thumbnail_url": draft.thumbnail,
            "color": draft.color,
            "ended": 0
        }

        self.giveaway_cache[giveaway_id] = data
        self.participant_cache[giveaway_id] = set()

        async with self.acquire_db() as db:
            await db.execute('''
                             INSERT INTO giveaways (guild_id, giveaway_id, channel_id, message_id, prize, winners_count,
                                                    end_time, host_id, required_roles, req_behaviour, blacklisted_roles,
                                                    extra_entry_roles, winner_role_id, image_url, thumbnail_url, color,
                                                    ended)
                             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                             ''', tuple(data.values()))
            await db.commit()
        return giveaway_id

    giveaway = app_commands.Group(name="giveaway", description="Commands for Dopamine's giveaway features.")

    @giveaway.command(name="create", description="Start the giveaway creation process.")
    @app_commands.describe(
        prize="What is being given away",
        duration="How long the giveaway should last (eg. 6d, 7h, 6m, 7mon)",
        winners="The number of winners of the giveaway"
    )
    async def giveaway_create(
        self,
        interaction: discord.Interaction,
        prize: str, duration: str,
        winners: app_commands.Range[int, 1, 50]
    ):
        seconds = get_duration_to_seconds(duration)
        if seconds <= 0:
            return await interaction.response.send_message('Invalid duration format! Use things like "6d", "7h", or "21m".', ephemeral=True)

        end_timestamp = get_now_plus_seconds_unix(seconds)

        draft = GiveawayDraft(
            guild_id=interaction.guild.id,
            channel_id=interaction.channel.id,
            prize=prize,
            winners=winners,
            end_time=end_timestamp
        )

        embed = self.create_giveaway_embed(draft)

        view = GiveawayPreviewView(self, draft)

        expires = get_now_plus_seconds_unix(900)

        await interaction.response.send_message(
            content=f"This is a preview of your giveaway. Configure it using the buttons below, then start it.\nThis preview expires **<t:{expires}:R>**!",
            embed=embed,
            view=view
        )

        view.message = await interaction.response.original_response()
