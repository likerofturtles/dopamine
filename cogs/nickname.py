import asyncio
import aiosqlite
import discord
from discord import app_commands, Interaction
from discord._types import ClientT
from discord.ext import commands, tasks
from typing import Dict, List, Optional, Set, Tuple
import re
from config import NFDB_PATH, DB_PATH #DB_PATH is points.db
from utils.checks import slash_mod_check
from contextlib import asynccontextmanager
from utils.log import LoggingManager

LEET_MAP = str.maketrans({
    '4': 'a',
    '@': 'a',
    '8': 'b',
    '3': 'e',
    '1': 'i',
    '!': 'i',
    '0': 'o',
    '5': 's',
    '$': 's',
    '7': 't'
})

class PlaceholderModal(discord.ui.Modal, title="Change Placeholder Text"):
    def __init__(self, cog, guild_id):
        super().__init__()
        self.cog = cog
        self.guild_id = guild_id

        settings = self.cog.serversettingscache.get(self.guild_id, {})
        current_placeholder = settings.get('placeholder', 'Change your nickname')

        self.placeholder_input = discord.ui.TextInput(
            label="New Placeholder",
            default=current_placeholder,
            min_length=3,
            max_length=32)

        self.add_item(self.placeholder_input)

    async def on_submit(self, interaction: discord.Interaction):
        new_text = self.placeholder_input.value

        async with self.cog.acquire_db() as db:
            await db.execute('UPDATE serversettings SET placeholder = ? WHERE guild_id = ?', (new_text, self.guild_id))
            await db.commit()

        if self.guild_id in self.cog.serversettingscache:
            self.cog.serversettingscache[self.guild_id]['placeholder'] = new_text

        new_description = (
            "This feature moderates the display name / nickname of all members in the server, and changes it to the 'placeholder' if any parameter is triggered.\n\n"
            "__There are two modes that can be toggled independently toggled__:\n\n"
            "* **Profanity Filter**: Moderates names containing blacklisted words.\n"
            "* **Symbols Filter**: Moderates names containing non-standard symbols.\n\n"
            f"The placeholder is currently set to: `{new_text}`\n\n"
            "__Additional Features__:\n\n"
            "* **Manual Scan**: </nickname moderator scan:1456588612958425294> to initiate a manual, server-wide scan that will automatically update any member's display name based on your Nickname Moderator settings.\n"
            "* **Logging**: Use the command </setlog:1428731733477556224> to enable logging.\n"
            "* **Verification**: </nickname moderator verify:1456588612958425294> to verify a member to make them be ignored by the moderation, and </nickname moderator verified:1456588612958425294> to see the list of verified members.\n\n"
        )

        embed = interaction.message.embeds[0]
        embed.description = new_description
        embed.set_footer(text="Use the buttons below to toggle modes or to customize the placeholder.")
        await interaction.response.edit_message(embed=embed)
        await self.cog.log_placeholder_change(interaction.user, new_text)
        await interaction.followup.send(embed=discord.Embed(
            title="Placeholder Text Updated Successfully",
            description=f"The new place holder is: `{new_text}`",
            colour=discord.Colour.green()
        ), ephemeral=True)


class NicknameModeratorView(discord.ui.View):
    def __init__(self, cog, guild_id):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.update_buttons()


    @discord.ui.button(label="Profanity Filter", style=discord.ButtonStyle.gray)
    async def toggle_profanity(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = self.cog.serversettingscache.get(self.guild_id, {})
        new_state = not settings.get('profanity_filter', False)

        async with self.cog.acquire_db() as db:
            await db.execute('UPDATE serversettings SET profanity_filter = ? WHERE guild_id = ?', (int(new_state), self.guild_id))
            await db.commit()

        settings['profanity_filter'] = new_state

        button.style = discord.ButtonStyle.green if new_state else discord.ButtonStyle.red
        emoji = "✅" if new_state else "❌"
        button.label = f"{emoji} Profanity Filter"
        await self.cog.log_profanity_toggle(interaction.user, new_state)
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Symbol Filter", style=discord.ButtonStyle.gray)
    async def toggle_symbol(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = self.cog.serversettingscache.get(self.guild_id, {})
        new_state = not settings.get('symbol_filter', False)

        async with self.cog.acquire_db() as db:
            await db.execute('UPDATE serversettings SET symbol_filter = ? WHERE guild_id = ?', (int(new_state), self.guild_id))
            await db.commit()

        settings['symbol_filter'] = new_state

        button.style = discord.ButtonStyle.green if new_state else discord.ButtonStyle.red
        emoji = "✅" if new_state else "❌"
        button.label = f"{emoji} Symbol Filter"
        await self.cog.log_symbol_toggle(interaction.user, new_state)
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Placeholder Text", style=discord.ButtonStyle.gray)
    async def open_placeholder_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PlaceholderModal(self.cog, self.guild_id))

    def update_buttons(self):
        settings = self.cog.serversettingscache.get(self.guild_id, {})

        prof_on = settings.get('profanity_filter', False)
        self.toggle_profanity.style = discord.ButtonStyle.green if prof_on else discord.ButtonStyle.red
        self.toggle_profanity.label = f"{'✅' if prof_on else '❌'} Profanity Filter"

        sym_on = settings.get('symbol_filter', False)
        self.toggle_symbol.style = discord.ButtonStyle.green if sym_on else discord.ButtonStyle.red
        self.toggle_symbol.label = f"{'✅' if sym_on else '❌'} Symbol Filter"


class Nickname(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.serversettingscache: Dict[int, Dict] = {}
        self.profanitycache: Set[str] = set()
        self.verifiedcache: Dict[int, Set[int]] = {}
        self.db_pool: Optional[asyncio.Queue[aiosqlite.Connection]] = None

    async def cog_load(self):
        await self.init_pools()
        await self.init_db()
        await self.load_profanity_cache()
        await self.load_serversettings_cache()
        await self.load_verified_cache()


    async def cog_unload(self):
        if self.db_pool is not None:
            while not self.db_pool.empty():
                conn = await self.db_pool.get()
                await conn.close()

    async def createpooledconnection(self, path: str) -> aiosqlite.Connection:
        max_retries = 5
        for attempt in range(max_retries):
            try:
                conn = await aiosqlite.connect(
                    path,
                    timeout=5,
                    isolation_level=None,
                )
                await conn.execute("PRAGMA busy_timeout=5000")
                await conn.execute("PRAGMA journal_mode=WAL")
                await conn.execute("PRAGMA wal_autocheckout=1000")
                await conn.execute("PRAGMA synchronous=NORMAL")
                await conn.execute("PRAGMA cache_size=-64000")
                await conn.execute("PRAGMA optimize")
                await conn.commit()
                return conn
            except Exception:
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.1 * (2 ** attempt))
                    continue
                raise
    async def init_pools(self, pool_size: int = 5):
        if self.db_pool is None:
            self.db_pool = asyncio.Queue(maxsize=pool_size)
            for _ in range(pool_size):
                conn = await self.createpooledconnection(NFDB_PATH)
                await self.db_pool.put(conn)

    @asynccontextmanager
    async def acquire_db(self) -> aiosqlite.Connection:
        assert self.db_pool is not None
        conn = await self.db_pool.get()
        try:
            yield conn
        finally:
            await self.db_pool.put(conn)

    async def init_db(self):
        async with self.acquire_db() as db:
            await db.execute(
                '''
                CREATE TABLE IF NOT EXISTS serversettings (
                    guild_id INTEGER NOT NULL PRIMARY KEY,
                    symbol_filter INTEGER DEFAULT 0,
                    profanity_filter INTEGER DEFAULT 0,
                    placeholder TEXT DEFAULT 'Change your nickname',
                    last_scan INTEGER DEFAULT 0
                )
                '''
            )
            await db.execute(
                '''
                CREATE TABLE IF NOT EXISTS profanity (
                    word TEXT NOT NULL PRIMARY KEY
                )
                '''

            )
            await db.execute(
                '''
                CREATE TABLE IF NOT EXISTS verified (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, user_id)
                )
                '''

            )
            await db.commit()

    async def load_profanity_cache(self):
        async with self.acquire_db() as db:
            async with db.execute('SELECT word FROM profanity') as cursor:
                rows = await cursor.fetchall()
                self.profanitycache = {row[0] for row in rows}

    async def load_serversettings_cache(self):
        async with self.acquire_db() as db:
            async with db.execute('SELECT guild_id, symbol_filter, profanity_filter, placeholder, last_scan FROM serversettings ORDER BY guild_id') as cursor:
                rows = await cursor.fetchall()
                self.serversettingscache = {row[0]: {"symbol_filter": row[1], "profanity_filter": row[2], "placeholder": row[3], "last_scan": row[4]} for row in rows}

    async def load_verified_cache(self):
        temp_cache = {}
        async with self.acquire_db() as db:
            async with db.execute('SELECT guild_id, user_id FROM verified') as cursor:
                async for guild_id, user_id in cursor:
                    if guild_id not in temp_cache:
                        temp_cache[guild_id] = set()
                    temp_cache[guild_id].add(user_id)
        self.verifiedcache = temp_cache

    def isbadname(self, name: str, guild: discord.Guild, member_id: int) -> bool:
        settings = self.serversettingscache.get(guild.id)
        if not settings:
            return False
        verified = self.verifiedcache.get(guild.id, set())
        if member_id in verified:
            return False
        member = guild.get_member(member_id)
        if not member:
            return False
        if member.top_role >= guild.me.top_role or member == guild.owner:
            return False

        if settings.get("symbol_filter"):
            if not name.replace(" ", "").isalnum():
                if re.search(r'[^\w\s\-_.]', name, re.UNICODE):
                    return "Non-Standard Symbol Filter"

        if settings.get("profanity_filter"):
            words = re.findall(r'\w+', name.lower())
            if any(word in self.profanitycache for word in words):
                return "Profanity Filter"

            normalized = re.sub(r'[^a-z]', '', name.lower().translate(LEET_MAP))

            if any(bad_word in normalized for bad_word in self.profanitycache if len(bad_word) > 3):
                return "Profanity Filter"

        return False

    async def log_nickname_reset(self, member: discord.Member, old_name: str, reason: str):
        channel_id = await self.manager.logging_get(member.guild.id)
        if not channel_id:
            return
        log_ch = self.bot.get_channel(channel_id)
        if not log_ch:
            log_ch = self.bot.fetch_channel(channel_id)
        bot_user = member.guild.me

        description = (
            f"User's nickname has been moderated by **{reason}**.\n\n"
            f"Old Name: `{old_name}`\n"
        )

        embed = discord.Embed(
            description=description,
            color=discord.Color.orange()
        )
        embed.set_author(
            name=f"{member} ({member.id})",
            icon_url=member.display_avatar.url
        )

        try:
            await log_ch.send(embed=embed)
        except discord.Forbidden:
            pass

    async def log_verify(self, member: discord.Member, author: discord.Member, status):
        channel_id = await self.manager.logging_get(member.guild.id)
        if not channel_id:
            return
        log_ch = self.bot.get_channel(channel_id)
        if not log_ch:
            log_ch = self.bot.fetch_channel(channel_id)

        action_text = "Verified" if status else "Unverified"
        footer_text = "verified" if status else "unverified"

        embed = discord.Embed(
            title=f"{member} ({member.id})",
            description = (
                f"{member.mention} has been **{action_text}**.\n\n"
                f"User is now {'ignored' if status else 'no longer ignored'} by the nickname moderator."),
                colour=discord.Colour(0x337fd5)
                          )
        embed.set_footer(
            text=f"{footer_text} by {author.name}",
            icon_url=author.display_avatar.url
        )
        try:
            await log_ch.send(embed=embed)
        except discord.Forbidden:
            pass

    async def log_scan(self, author: discord.Member):
        channel_id = await self.manager.logging_get(author.guild.id)
        if not channel_id:
            return
        log_ch = self.bot.get_channel(channel_id)
        if not log_ch:
            log_ch = self.bot.fetch_channel(channel_id)

        embed = discord.Embed(
            title="A full server-wide scan for Nickname Moderator has been triggered.",
            description="This process will scan all member's display name and appropriately update as needed based on your server's Nickname Moderator settings. This process may take several minutes.",
            colour=discord.Colour.orange()
        )
        embed.set_footer(
            text=f"triggered by {author.name}",
            icon_url=author.display_avatar.url
        )
        try:
            await log_ch.send(embed=embed)
        except discord.Forbidden:
            pass

    async def log_profanity_toggle(self, member: discord.Member, new_state):
        channel_id = await self.manager.logging_get(member.guild.id)
        if not channel_id:
            return
        log_ch = self.bot.get_channel(channel_id)
        if not log_ch:
            log_ch = self.bot.fetch_channel(channel_id)

        embed = discord.Embed(
            title=f"Profanity Filter for Nickname Moderator has been **{'enabled' if new_state else 'disabled'}**.",
            description=f"Dopamine will {'now' if new_state else 'no longer'} scan and moderate all display names for profanity.",
            colour=discord.Colour.yellow()
        )
        embed.set_footer(
            text=f"{'enabled' if new_state else 'disabled'} by {member.name}",
            icon_url=member.display_avatar.url
        )
        try:
            await log_ch.send(embed=embed)
        except discord.Forbidden:
            pass

    async def log_symbol_toggle(self, member: discord.Member, new_state):
        log_ch = await self.manager.logging_get(member.guild.id)
        if not log_ch:
            return

        embed = discord.Embed(
            title=f"Symbol Filter for Nickname Moderator has been **{'enabled' if new_state else 'disabled'}**.",
            description=f"Dopamine will {'now' if new_state else 'no longer'} scan and moderate all display names for non-standard symbols.",
            colour=discord.Colour.yellow()
        )
        embed.set_footer(
            text=f"{'enabled' if new_state else 'disabled'} by {member.name}",
            icon_url=member.display_avatar.url
        )
        try:
            await log_ch.send(embed=embed)
        except discord.Forbidden:
            pass

    async def log_placeholder_change(self, member: discord.Member, new_state):
        channel_id = await self.manager.logging_get(member.guild.id)
        if not channel_id:
            return
        log_ch = self.bot.get_channel(channel_id)
        if not log_ch:
            log_ch = self.bot.fetch_channel(channel_id)

        embed = discord.Embed(
            title="Nickname Moderator placeholder has been changed",
            description=f"The new placeholder is: `{new_state}`",
            colour=discord.Colour.yellow()
        )
        embed.set_footer(
            text=f"changed by {member.name}",
            icon_url=member.display_avatar.url
        )

        try:
            await log_ch.send(embed=embed)
        except discord.Forbidden:
            pass

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.display_name == after.display_name:
            return

        if after.bot:
            return

        guild = after.guild
        user_id = after.id
        trigger_reason = self.isbadname(after.display_name, guild, user_id)
        if trigger_reason:
            settings = self.serversettingscache.get(after.guild.id, {})
            placeholder = settings.get("placeholder", "Change your nickname")

            try:
                old_name = after.display_name
                reason = trigger_reason
                await after.edit(nick=placeholder, reason=f"Dopamine: {trigger_reason}")

                await self.log_nickname_reset(after, old_name, reason)
            except (discord.Forbidden, discord.HTTPException):
                pass

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return

        guild = member.guild
        guild_id = member.guild.id
        user_id = member.id

        trigger_reason = self.isbadname(member.display_name, guild, user_id)

        if trigger_reason:
            settings = self.serversettingscache.get(guild_id, {})
            placeholder = settings.get("placeholder", "Change your nickname")

            try:
                old_name = member.display_name
                reason = trigger_reason
                await member.edit(nick=placeholder, reason=f"Dopamine: {trigger_reason}")

                await self.log_nickname_reset(member, old_name, reason)
            except (discord.Forbidden, discord.HTTPException):
                pass

    nickname_group = app_commands.Group(name="nickname", description="Nickname commands")

    moderator_group = app_commands.Group(name="moderator", description="Nickname Moderator commands group", parent=nickname_group)

    @moderator_group.command(name="verify", description="Verify a user's nickname to make them immune to the moderation.")
    @app_commands.check(slash_mod_check)
    async def verify_user(self, interaction: discord.Interaction, member: discord.Member):
        guild_id = interaction.guild.id
        user_id = member.id
        user = member

        is_verified = user_id in self.verifiedcache.get(guild_id, set())
        new_status = not is_verified

        async with self.acquire_db() as db:
            if is_verified:
                await db.execute(
                    'DELETE FROM verified WHERE guild_id = ? AND user_id = ?',
                    (guild_id, user_id),
                )
                self.verifiedcache[guild_id].discard(user_id)
                status_embed = discord.Embed(
                    title=f"Unverified {user.display_name} Successfully",
                    description="User will not be ignored by the moderation anymore.",
                    colour=discord.Color.green()
                )

            else:
                await db.execute('INSERT OR IGNORE INTO verified (guild_id, user_id) VALUES (?, ?)',
                                 (guild_id, user_id),
                                 )
                self.verifiedcache.setdefault(guild_id, set()).add(user_id)
                status_embed = discord.Embed(
                    title=f"Verified {user.display_name} Successfully",
                    description="User will now be ignored by the moderation.",
                    colour=discord.Color.green()
                )
            await db.commit()
        await self.log_verify(user, interaction.user, new_status)
        await interaction.response.send_message(embed=status_embed, ephemeral=True)

    @moderator_group.command(name="panel", description="Open the Nickname Moderator settings and info panel.")
    @app_commands.check(slash_mod_check)


    async def nickname_panel(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id

        if guild_id not in self.serversettingscache:
            async with self.acquire_db() as db:
                await db.execute('INSERT OR IGNORE INTO serversettings (guild_id) VALUES (?)', (guild_id,))
                await db.commit()
            self.serversettingscache[guild_id] = {
                "symbol_filter": 0,
                "profanity_filter": 0,
                "placeholder": "Change your nickname",
                "last_scan": 0
            }


        settings = self.serversettingscache.get(guild_id, {})
        placeholder = settings.get("placeholder", "Change your nickname")
        embed_description = (
            "This feature moderates the display name / nickname of all members in the server, and changes it to the 'placeholder' if any parameter is triggered.\n\n"
            "__There are two modes that can be toggled independently toggled__:\n\n"
            "* **Profanity Filter**: Moderates names containing blacklisted words.\n"
            "* **Symbols Filter**: Moderates names containing non-standard symbols.\n\n"
            f"The placeholder is currently set to: `{placeholder}`\n\n"
            "__Additional Features__:\n\n"
            "* **Manual Scan**: </nickname moderator scan:1456588612958425294> to initiate a manual, server-wide scan that will automatically update any member's display name based on your Nickname Moderator settings.\n"
            "* **Logging**: Use the command </setlog:1428731733477556224> to enable logging.\n"
            "* **Verification**: </nickname moderator verify:1456588612958425294> to verify a member to make them be ignored by the moderation, and </nickname moderator verified:1456588612958425294> to see the list of verified members.\n\n"
        )

        embed = discord.Embed(
            title="Nickname Moderation",
            description=embed_description,
            color=discord.Colour(0x337fd5)
        )
        embed.set_footer(text="Use the buttons below to toggle modes or to customize the placeholder.")
        view = NicknameModeratorView(self, guild_id)

        await interaction.response.send_message(embed=embed, view=view)

    @moderator_group.command(name="verified", description="List all verified members.")
    @app_commands.check(slash_mod_check)
    async def list_verified(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        verified_ids = list(self.verifiedcache.get(guild_id, set()))

        if not verified_ids:
            await interaction.response.send_message(embed=discord.Embed(
                title="No verified members found",
                description="We searched our caches and databases far and wide but unfortunately, no members were found.\n\nTo verify a member, use </nickname moderator verify:1456588612958425294>"
            ), ephemeral=True)
            return

        mentions = [f"<@{user_id}>" for user_id in verified_ids]

        chunks = [mentions[i:i + 30] for i in range(0, len(mentions), 30)]

        first_description = "\n".join(chunks[0])
        await interaction.response.send_message(embed=discord.Embed(
            title="Verified Members for Nickname Moderator",
            description=first_description,
            color=discord.Colour(0x337fd5)
        ), ephemeral=True)

        for chunk in chunks[1:]:
            extra_description = "\n".join(chunk)
            await interaction.followup.send(embed=discord.Embed(
                description=extra_description,
                color=discord.Colour(0x337fd5)
            ), ephemeral=True)

    @commands.command("update_profanity_database")
    @commands.is_owner()
    async def update_profanity_database(self, ctx: commands.Context, *, words_string: str):
        new_words = [word.strip().lower() for word in words_string.split(", ")]

        if not new_words:
            return await ctx.send("No words detected. Format: `word1, word2, word3`")

        async with self.acquire_db() as db:
            await db.executemany(
                'INSERT OR IGNORE INTO profanity (word) VALUES (?)',
                [(word,) for word in new_words]
            )
            await db.commit()

        self.profanitycache.update(new_words)

        await ctx.send(embed=discord.Embed(title="Updated Database Successfully", description=f"Successfully added `{len(new_words)}` to the profanity database."))

    @moderator_group.command(name="scan", description="Scan all members and reset names. (3-day cooldown)")
    @app_commands.check(slash_mod_check)
    async def force_scan(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        now = int(discord.utils.utcnow().timestamp())
        three_days = 3 * 24 * 60 * 60
        guild = interaction.guild

        settings = self.serversettingscache.get(guild_id, {})
        last_scan = settings.get("last_scan", 0)

        if now - last_scan < three_days:
            remaining = last_scan + three_days - now
            days = remaining // 86400
            hours = (remaining % 86400) // 3600
            return await interaction.response.send_message(embed=discord.Embed(
                title="Slow down!",
                description=f"This command is on cooldown. Try again in **{days}** days and **{hours}** hours. .",
                color=discord.Color.red()
            ))

        async with self.acquire_db() as db:
            await db.execute(
                'UPDATE serversettings SET last_scan = ? WHERE guild_id = ?',
                (now, guild_id)
            )
            await db.commit()

        if guild_id in self.serversettingscache:
            self.serversettingscache[guild_id]["last_scan"] = now

        await interaction.response.send_message(embed=discord.Embed(title="Starting server-wide scan...", description="This process will scan all member's display name and appropriately update as needed based on your server's Nickname Moderator settings. This process may take several minutes."), ephemeral=True)
        await self.log_scan(interaction.user)
        placeholder = settings.get("placeholder", "Change your nickname")
        count = 0

        async for member in interaction.guild.fetch_members(limit=None):
            if member.bot: continue

            reason = self.isbadname(member.display_name, guild, member.id)
            if reason:
                try:
                    old_name = member.display_name
                    await member.edit(nick=placeholder, reason=f"Dopamine Scan: {reason}")
                    await self.log_nickname_reset(member, old_name, f"Force Scan: {reason}")
                    count += 1
                    await asyncio.sleep(0.2)
                except (discord.Forbidden, discord.HTTPException):
                    continue

        await interaction.followup.send(embed=discord.Embed(title="Scan Complete", description=f"**{count}** nicknames have been moderated.", colour=discord.Colour.green()), ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Nickname(bot))