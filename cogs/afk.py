import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Set, Any, AsyncGenerator

import aiosqlite
import discord
from aiosqlite import Connection
from discord import app_commands
from discord.ext import commands

from config import AFKDB_PATH
from beacon import ViewPaginator, PrivateView
from utils.data_handlers import export_table
from utils.data_protocol import DataDeleteResult, DataExportChunk, DataFeatureMeta, DataMonitorResult
import datetime

AFK_BUFFER_SECONDS = 30
AFK_MAX_SECONDS = 72 * 60 * 60


@dataclass
class AFKState:
    user_id: int
    status: Optional[str]
    is_global: bool
    save_missed_pings: bool
    started_at: int
    buffer_until: int
    origin_guild_id: Optional[int]
    old_nick: Optional[str]


@dataclass
class MissedPing:
    id: int
    user_id: int
    author_id: int
    guild_id: Optional[int]
    channel_id: Optional[int]
    message_id: Optional[int]
    content: str
    timestamp: int


class GoToPageModal(discord.ui.Modal, title="Go to Page"):
    page_input = discord.ui.TextInput(
        label="Enter page number",
        placeholder="e.g. 2",
        min_length=1,
        max_length=5
    )

    def __init__(self, paginator: "MissedPingsPaginator"):
        super().__init__()
        self.paginator = paginator
        self.page_input.label = f"Enter page number (1-{paginator.total_pages})"

    async def on_submit(self, interaction: discord.Interaction):
        try:
            page = int(self.page_input.value)
            if 1 <= page <= self.paginator.total_pages:
                self.paginator.current_page = page
                await self.paginator.update_message(interaction)
            else:
                await interaction.response.send_message(
                    f"Invalid page number. Please enter a value between 1 and {self.paginator.total_pages}.",
                    ephemeral=True
                )
        except ValueError:
            await interaction.response.send_message(
                "Please enter a valid whole number for the page number.",
                ephemeral=True
            )


class MissedPingsPaginator(discord.ui.View):
    def __init__(self, interaction: discord.Interaction, entries: list[MissedPing], cog: "AFK"):
        super().__init__(timeout=180)
        self.interaction = interaction
        self.entries = entries
        self.cog = cog
        self.current_page = 1
        self.per_page = 5
        self.total_pages = (len(entries) + self.per_page - 1) // self.per_page
        self.message: discord.Message | None = None
        self.update_buttons()

    def update_buttons(self):
        self.prev_page.disabled = self.current_page == 1
        self.go_to_page.disabled = self.total_pages == 1
        self.go_to_page.label = f"Page {self.current_page} of {self.total_pages}"
        self.next_page.disabled = self.current_page == self.total_pages

    async def get_page_embeds(self) -> list[discord.Embed]:
        start = (self.current_page - 1) * self.per_page
        end = start + self.per_page
        page_entries = self.entries[start:end]
        embeds = []

        for entry in page_entries:
            guild = self.interaction.client.get_guild(entry.guild_id) if entry.guild_id else None
            member = guild.get_member(entry.author_id) if guild else None
            user = member or self.interaction.client.get_user(entry.author_id)
            if not user:
                try:
                    user = await self.interaction.client.fetch_user(entry.author_id)
                except discord.HTTPException:
                    user = None

            display_name = user.display_name if user else f"User {entry.author_id}"
            avatar_url = user.display_avatar.url if user else None

            embed = discord.Embed(colour=discord.Colour(0x944ae8))
            embed.set_author(name=display_name, icon_url=avatar_url)

            jump_link = f"[Click here to Jump](https://discord.com/channels/{entry.guild_id or '@me'}/{entry.channel_id or 0}/{entry.message_id or 0})"
            content = entry.content
            if len(content) > 1000:
                content = content[:1000] + "..."

            embed.description = f"{jump_link}\n{content}"
            embed.set_footer(text=f"in {guild.name if guild else 'Unknown Server'}")
            embed.timestamp = datetime.datetime.fromtimestamp(entry.timestamp, tz=datetime.timezone.utc).replace(tzinfo=None)
            embeds.append(embed)

        return embeds

    async def send_initial(self) -> bool:
        embeds = await self.get_page_embeds()
        content = f"## {len(self.entries)} Missed Pings"
        try:
            self.message = await self.interaction.user.send(content=content, embeds=embeds, view=self)
            return True
        except discord.Forbidden:
            return False

    async def update_message(self, interaction: discord.Interaction):
        self.update_buttons()
        embeds = await self.get_page_embeds()
        content = f"## {len(self.entries)} Missed Pings"
        await interaction.response.edit_message(content=content, embeds=embeds, view=self)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.primary)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 1:
            self.current_page -= 1
            await self.update_message(interaction)

    @discord.ui.button(style=discord.ButtonStyle.secondary)
    async def go_to_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(GoToPageModal(self))

    @discord.ui.button(label="▶", style=discord.ButtonStyle.primary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page < self.total_pages:
            self.current_page += 1
            await self.update_message(interaction)


class ViewMissedPings(PrivateView):
    def __init__(self, cog: "AFK", user_id: int, user: discord.User | discord.Member,
                 string_for_after_missed_pings_clear: str):
        super().__init__(user, timeout=120)
        self.cog = cog
        self.user_id = user_id
        self.message = None
        self.string_for_after_missed_pings_clear = string_for_after_missed_pings_clear

    @discord.ui.button(label="View Missed Pings", style=discord.ButtonStyle.primary)
    async def view_missed_pings(self, interaction: discord.Interaction, button: discord.ui.Button):
        entries = self.cog.missed_pings_cache.get(self.user_id, [])

        if not entries:
            return await interaction.response.send_message("You have no missed pings.", ephemeral=True)

        paginator = MissedPingsPaginator(interaction, entries, self.cog)
        sent = await paginator.send_initial()

        if sent:
            if not paginator.message is None:
                await interaction.response.send_message(
                    f"I sent the Missed Pings to your DMs! [Click here to Jump]({paginator.message.jump_url}).", ephemeral=True)
            if not self.message is None:
                await self.message.edit(content=self.string_for_after_missed_pings_clear, view=None)
            await self.cog.clear_missed_pings(self.user_id)
        else:
            await interaction.response.send_message(
                """I can't DM you the Missed Pings! Please first DM me "hi" so that Discord lets me DM you.""",
                ephemeral=True)

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.edit(content=self.string_for_after_missed_pings_clear, view=None)
            except (discord.NotFound, discord.HTTPException):
                pass
        await self.cog.clear_missed_pings(self.user_id)
        self.stop()


class ViewNotifyOnReturn(discord.ui.View):
    """View attached to the AFK notice allowing users to be notified when the AFK target returns."""

    def __init__(self, cog: "AFK", afk_user_id: int):
        super().__init__(timeout=60)
        self.cog = cog
        self.afk_user_id = afk_user_id
        self.message = None

    @discord.ui.button(label="DM me upon their grand return", style=discord.ButtonStyle.secondary,
                       custom_id="afk_notify_on_return")
    async def notify_me(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id == self.afk_user_id:
            return await interaction.response.send_message("Don't you think this is a bit too narcissistic? You can't register to be notified about your own return.",
                                                           ephemeral=True)

        if self.afk_user_id not in self.cog.afk_users:
            await interaction.response.edit_message(view=None)
            return await interaction.followup.send(content="This user is no longer AFK.", ephemeral=True)

        success = await self.cog.add_notification_request(self.afk_user_id, interaction.user.id)
        user = self.cog.bot.get_user(self.afk_user_id) or await self.cog.bot.fetch_user(self.afk_user_id)
        if success:
            await interaction.response.send_message(f"Got it! You will now be notified when {user.mention} returns.", ephemeral=True)
        else:
            success = await self.cog.remove_notification_request(self.afk_user_id, interaction.user.id)
            if success:
                await interaction.response.send_message(f"You will no longer be notified when {user.mention} returns. Sad.",
                                                        ephemeral=True)
            else:
                await interaction.response.send_message("sum ting wong", ephemeral=True)

    @discord.ui.button(label="Leave a message", style=discord.ButtonStyle.secondary)
    async def leave_a_message(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id == self.afk_user_id:
            return await interaction.response.send_message("Why would you wanna leave a message for yourself? 🤨 This isn't a notes app... I mean I do support notes, use </note create:1522184511675301990> to create one! But if you really wanna know how to leave a message, just mention the AFK user and I will hand all the missed pings to them when they return.", ephemeral=True)
        user = self.cog.bot.get_user(self.afk_user_id) or await self.cog.bot.fetch_user(self.afk_user_id)
        await interaction.response.send_message(f"To leave a message, you can simply mention {user.mention} like normal and they will be notified about it by me when they return!", ephemeral=True)

    async def on_timeout(self) -> None:
        if self.message:
            await self.message.edit(view=None)
        self.stop()


class AFK(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db_pool: Optional[asyncio.Queue[aiosqlite.Connection]] = None
        self.afk_users: Dict[int, AFKState] = {}
        self.missed_pings_cache: Dict[int, List[MissedPing]] = {}
        self.notification_cache: Dict[int, Set[int]] = {}

    async def cog_load(self):
        await self.init_pools()
        await self.init_db()
        await self.populate_caches()

    async def cog_unload(self):
        if self.db_pool is not None:
            while not self.db_pool.empty():
                try:
                    conn = self.db_pool.get_nowait()
                    await conn.close()
                except (asyncio.QueueEmpty, Exception):
                    break
            self.db_pool = None

    async def create_pooled_connection(self, path: str) -> Connection | None:
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
                await conn.execute("PRAGMA synchronous=NORMAL")
                await conn.execute("PRAGMA foreign_keys=ON")
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
                conn = await self.create_pooled_connection(AFKDB_PATH)
                if not conn is None:
                    await self.db_pool.put(conn)

    @asynccontextmanager
    async def acquire_db(self) -> AsyncGenerator[Connection, Any]:
        assert self.db_pool is not None
        conn = await self.db_pool.get()
        try:
            yield conn
        finally:
            await self.db_pool.put(conn)

    async def init_db(self):
        async with self.acquire_db() as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS afk_users (
                    user_id INTEGER PRIMARY KEY,
                    status TEXT,
                    is_global INTEGER DEFAULT 1,
                    role_id INTEGER,
                    save_missed_pings INTEGER DEFAULT 1,
                    started_at INTEGER NOT NULL,
                    buffer_until INTEGER NOT NULL,
                    origin_guild_id INTEGER,
                    old_nick TEXT
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS missed_pings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    author_id INTEGER NOT NULL,
                    guild_id INTEGER,
                    channel_id INTEGER,
                    message_id INTEGER,
                    content TEXT,
                    timestamp INTEGER NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS return_notifications (
                    afk_user_id INTEGER NOT NULL,
                    observer_id INTEGER NOT NULL,
                    PRIMARY KEY (afk_user_id, observer_id)
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_missed_pings_user_id ON missed_pings (user_id, timestamp)"
            )
            await db.commit()

    async def populate_caches(self):
        self.afk_users.clear()
        self.missed_pings_cache.clear()
        self.notification_cache.clear()

        now = int(discord.utils.utcnow().timestamp())

        async with self.acquire_db() as db:
            async with db.execute(
                    """
                    SELECT user_id, status, is_global, save_missed_pings,
                           started_at, buffer_until, origin_guild_id, old_nick
                    FROM afk_users
                    """
            ) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    (
                        user_id,
                        status,
                        is_global,
                        save_missed_pings,
                        started_at,
                        buffer_until,
                        origin_guild_id,
                        old_nick,
                    ) = row

                    if now - started_at >= AFK_MAX_SECONDS:
                        await db.execute("DELETE FROM afk_users WHERE user_id = ?", (user_id,))
                        await db.execute("DELETE FROM return_notifications WHERE afk_user_id = ?", (user_id,))
                        continue

                    state = AFKState(
                        user_id=user_id,
                        status=status,
                        is_global=bool(is_global),
                        save_missed_pings=bool(save_missed_pings),
                        started_at=started_at,
                        buffer_until=buffer_until,
                        origin_guild_id=origin_guild_id,
                        old_nick=old_nick,
                    )
                    self.afk_users[user_id] = state

            async with db.execute(
                    """
                    SELECT id, user_id, author_id, guild_id, channel_id,
                           message_id, content, timestamp
                    FROM missed_pings
                    ORDER BY timestamp ASC
                    """
            ) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    (
                        mp_id,
                        user_id,
                        author_id,
                        guild_id,
                        channel_id,
                        message_id,
                        content,
                        timestamp,
                    ) = row
                    entry = MissedPing(
                        id=mp_id,
                        user_id=user_id,
                        author_id=author_id,
                        guild_id=guild_id,
                        channel_id=channel_id,
                        message_id=message_id,
                        content=content or "*No message content*",
                        timestamp=timestamp,
                    )
                    self.missed_pings_cache.setdefault(user_id, []).append(entry)

            async with db.execute("SELECT afk_user_id, observer_id FROM return_notifications") as cursor:
                rows = await cursor.fetchall()
                for afk_uid, obs_id in rows:
                    self.notification_cache.setdefault(afk_uid, set()).add(obs_id)

    async def add_notification_request(self, afk_user_id: int, observer_id: int) -> bool:
        observers = self.notification_cache.setdefault(afk_user_id, set())
        if observer_id in observers:
            return False

        observers.add(observer_id)
        async with self.acquire_db() as db:
            await db.execute(
                "INSERT OR IGNORE INTO return_notifications (afk_user_id, observer_id) VALUES (?, ?)",
                (afk_user_id, observer_id)
            )
            await db.commit()
        return True

    async def remove_notification_request(self, afk_user_id: int, observer_id: int) -> bool:
        observers = self.notification_cache.get(afk_user_id)
        if not observers or observer_id not in observers:
            return False

        observers.discard(observer_id)
        if not observers:
            self.notification_cache.pop(afk_user_id, None)

        async with self.acquire_db() as db:
            await db.execute(
                "DELETE FROM return_notifications WHERE afk_user_id = ? AND observer_id = ?",
                (afk_user_id, observer_id)
            )
            await db.commit()
        return True

    async def set_afk(
            self,
            *,
            user: discord.Member,
            status: Optional[str],
            is_global: bool,
            save_missed_pings: bool,
    ):
        now = int(discord.utils.utcnow().timestamp())
        buffer_until = now + AFK_BUFFER_SECONDS

        old_nick = user.nick
        origin_guild_id = user.guild.id if isinstance(user.guild, discord.Guild) else None

        try:
            new_nick = f"[AFK] {user.display_name}"
            if len(new_nick) <= 32:
                await user.edit(nick=new_nick, reason="AFK enabled")
        except (discord.Forbidden, discord.HTTPException):
            pass

        state = AFKState(
            user_id=user.id,
            status=status,
            is_global=is_global,
            save_missed_pings=save_missed_pings,
            started_at=now,
            buffer_until=buffer_until,
            origin_guild_id=origin_guild_id,
            old_nick=old_nick,
        )

        self.afk_users[user.id] = state

        async with self.acquire_db() as db:
            await db.execute(
                """
                INSERT INTO afk_users (
                    user_id, status, is_global,
                    save_missed_pings, started_at, buffer_until,
                    origin_guild_id, old_nick
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    status = excluded.status,
                    is_global = excluded.is_global,
                    save_missed_pings = excluded.save_missed_pings,
                    started_at = excluded.started_at,
                    buffer_until = excluded.buffer_until,
                    origin_guild_id = excluded.origin_guild_id,
                    old_nick = excluded.old_nick
                """,
                (
                    user.id,
                    status,
                    int(is_global),
                    int(save_missed_pings),
                    now,
                    buffer_until,
                    origin_guild_id,
                    old_nick,
                ),
            )
            await db.commit()

    async def clear_afk(self, user_id: int, revert_nick: bool = True):
        state = self.afk_users.pop(user_id, None)

        if revert_nick and state and state.origin_guild_id:
            guild = self.bot.get_guild(state.origin_guild_id)
            if guild:
                member = guild.get_member(user_id) or await guild.fetch_member(user_id)
                if member:
                    try:
                        await member.edit(nick=state.old_nick, reason="AFK ended")
                    except (discord.Forbidden, discord.HTTPException):
                        pass

        observers = self.notification_cache.pop(user_id, set())
        if observers:
            afk_user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            afk_name = afk_user.mention if afk_user else f"User `{user_id}`"

            for obs_id in observers:
                obs_user = self.bot.get_user(obs_id) or await self.bot.fetch_user(obs_id)
                if obs_user:
                    try:
                        await obs_user.send(f"{afk_name} has now made their grand return from being AFK! You should probably go talk to them or something.\n-# To not receive this in the future, don't click the big black button to be notified next time, duh.")
                    except discord.Forbidden:
                        pass

        async with self.acquire_db() as db:
            await db.execute("DELETE FROM afk_users WHERE user_id = ?", (user_id,))
            await db.execute("DELETE FROM return_notifications WHERE afk_user_id = ?", (user_id,))
            await db.commit()

    async def clear_missed_pings(self, user_id: int):
        self.missed_pings_cache.pop(user_id, None)
        async with self.acquire_db() as db:
            await db.execute("DELETE FROM missed_pings WHERE user_id = ?", (user_id,))
            await db.commit()

    def _format_afk_notice(self, member: discord.Member | discord.User, state: AFKState) -> str:
        now = int(discord.utils.utcnow().timestamp())
        elapsed = max(0, now - state.started_at)

        if elapsed < 60:
            ago = "A few seconds ago"
        elif elapsed < 3600:
            minutes = elapsed // 60
            ago = f"{minutes} minutes ago"
        else:
            hours = elapsed // 3600
            ago = f"{hours} hours ago"

        if state.status:
            return f"{member.display_name} is AFK: {state.status} - {ago}"
        return f"{member.display_name} is AFK - {ago}"

    def _format_welcome_back(self, state: AFKState, missed_count: int) -> tuple[str, str]:
        now = int(discord.utils.utcnow().timestamp())
        elapsed = max(0, now - state.started_at)

        if elapsed < 60:
            base = "Welcome back! You were AFK for less than a minute!"
        elif elapsed < 3600:
            minutes = elapsed // 60
            base = f"Welcome back! You were AFK for **{minutes}** minutes!"
        else:
            hours, remainder = divmod(elapsed, 3600)
            minutes = remainder // 60

            if minutes > 0:
                base = f"Welcome back! You were AFK for **{hours}** hours and **{minutes}** minutes!"
            else:
                base = f"Welcome back! You were AFK for **{hours}** hours!"

        string_for_after_missed_pings_clear = base
        if missed_count > 0 and state.save_missed_pings:
            base += f"\nYou have **{missed_count}** missed pings!"

        return base, string_for_after_missed_pings_clear

    def _is_afk_active_in_context(self, state: AFKState, guild: Optional[discord.Guild]) -> bool:
        now = int(discord.utils.utcnow().timestamp())
        if now - state.started_at >= AFK_MAX_SECONDS:
            return False
        if not state.is_global and guild and state.origin_guild_id:
            return guild.id == state.origin_guild_id
        return True

    async def _maybe_cleanup_if_expired(self, user_id: int, state: AFKState) -> bool:
        now = int(discord.utils.utcnow().timestamp())
        if now - state.started_at >= AFK_MAX_SECONDS:
            await self.clear_afk(user_id, revert_nick=True)
            return True
        return False

    @commands.command(name="afk")
    async def prefix_afk(self, ctx: commands.Context, *, status: Optional[str] = None):
        if not isinstance(ctx.author, discord.Member):
            return

        state = self.afk_users.get(ctx.author.id)
        if state:
            return await ctx.send("You're already AFK!", delete_after=10)

        await self.set_afk(
            user=ctx.author,
            status=status,
            is_global=True,
            save_missed_pings=True,
        )
        reply = f"{ctx.author.mention} you're now AFK: {status}" if status else f"{ctx.author.mention} you're now AFK!"
        await ctx.send(reply)

    @app_commands.command(name="afk", description="Set or update your AFK status.")
    @app_commands.describe(
        status="Optional AFK status message.",
        global_mentions="Whether mentions in all servers should trigger AFK responses instead of only the current server.",
        save_missed_pings="Whether to save messages where you are mentioned and show them when you're back.",
    )
    async def slash_afk(
            self,
            interaction: discord.Interaction,
            status: Optional[str] = None,
            global_mentions: Optional[bool] = True,
            save_missed_pings: Optional[bool] = True,
    ):
        if not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("AFK can only be used in servers.", ephemeral=True)

        state = self.afk_users.get(interaction.user.id)
        if state:
            return await interaction.response.send_message("You're already AFK!", ephemeral=True)

        is_global = bool(global_mentions) if global_mentions is not None else True
        save_mp = bool(save_missed_pings) if save_missed_pings is not None else True

        await self.set_afk(
            user=interaction.user,
            status=status,
            is_global=is_global,
            save_missed_pings=save_mp,
        )

        reply = f"{interaction.user.mention} you're now AFK: {status}" if status else f"{interaction.user.mention} you're now AFK!"
        await interaction.response.send_message(
            reply
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        user_id = message.author.id
        state = self.afk_users.get(user_id)

        if state and self._is_afk_active_in_context(state, message.guild):
            now = int(discord.utils.utcnow().timestamp())
            if await self._maybe_cleanup_if_expired(user_id, state):
                return

            if now >= state.buffer_until:
                missed = self.missed_pings_cache.get(user_id, [])
                content, string_for_after_missed_pings_clear = self._format_welcome_back(state, len(missed))
                view = ViewMissedPings(self, user_id, message.author,
                                       string_for_after_missed_pings_clear) if missed and state.save_missed_pings else None

                try:

                    if view:
                        msg = await message.reply(content, view=view, mention_author=False)
                        view.message = msg
                    else:
                        await message.reply(content, mention_author=False)
                except (discord.Forbidden, discord.HTTPException):
                    pass

                await self.clear_afk(user_id, revert_nick=True)
                return

        if message.guild is None:
            return

        for mentioned in message.mentions:
            if mentioned.bot:
                continue

            state = self.afk_users.get(mentioned.id)
            if not state:
                continue

            if await self._maybe_cleanup_if_expired(mentioned.id, state):
                continue

            if not self._is_afk_active_in_context(state, message.guild):
                continue

            if state.save_missed_pings:
                await self._store_missed_ping(
                    user_id=mentioned.id,
                    author_id=message.author.id,
                    guild_id=message.guild.id,
                    channel_id=message.channel.id,
                    message_id=message.id,
                    content=message.content or "*No message content*",
                    timestamp=int(message.created_at.timestamp()),
                )

            notice = self._format_afk_notice(mentioned, state)
            try:
                notify_view = ViewNotifyOnReturn(self, mentioned.id)
                msg = await message.channel.send(notice, view=notify_view)
                notify_view.message = msg
            except (discord.Forbidden, discord.HTTPException):
                pass

        if message.role_mentions:
            for role in message.role_mentions:
                for uid, state in list(self.afk_users.items()):
                    if await self._maybe_cleanup_if_expired(uid, state):
                        continue

                    if not self._is_afk_active_in_context(state, message.guild):
                        continue

                    member = message.guild.get_member(uid) or await message.guild.fetch_member(uid)
                    if not member or role not in member.roles:
                        continue

                    await self._store_missed_ping(
                        user_id=uid,
                        author_id=message.author.id,
                        guild_id=message.guild.id,
                        channel_id=message.channel.id,
                        message_id=message.id,
                        content=message.content or "*No message content*",
                        timestamp=int(message.created_at.timestamp()),
                    )

                    if len(role.members) <= 3:
                        notice = self._format_afk_notice(member, state)
                        try:
                            notify_view = ViewNotifyOnReturn(self, member.id)
                            msg = await message.channel.send(notice, view=notify_view)
                            notify_view.message = msg
                        except (discord.Forbidden, discord.HTTPException):
                            pass

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        message_id = payload.message_id

        for user_id, entries in self.missed_pings_cache.items():
            for entry in entries:
                if entry.message_id == message_id:
                    entry.content = "*This message was deleted*"

        async with self.acquire_db() as db:
            await db.execute(
                "UPDATE missed_pings SET content = ? WHERE message_id = ?",
                ("[This message was deleted]", message_id),
            )
            await db.commit()

    async def _store_missed_ping(
            self,
            *,
            user_id: int,
            author_id: int,
            guild_id: Optional[int],
            channel_id: Optional[int],
            message_id: Optional[int],
            content: str,
            timestamp: int,
    ):
        async with self.acquire_db() as db:
            cursor = await db.execute(
                """
                INSERT INTO missed_pings (
                    user_id, author_id, guild_id, channel_id,
                    message_id, content, timestamp
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    author_id,
                    guild_id,
                    channel_id,
                    message_id,
                    content or "*No message content*",
                    timestamp,
                ),
            )
            await db.commit()
            mp_id = cursor.lastrowid
        if mp_id is None:
            return
        entry = MissedPing(
            id=mp_id,
            user_id=user_id,
            author_id=author_id,
            guild_id=guild_id,
            channel_id=channel_id,
            message_id=message_id,
            content=content or "*No message content*",
            timestamp=timestamp,
        )
        self.missed_pings_cache.setdefault(user_id, []).append(entry)

    def data_features(self) -> list[DataFeatureMeta]:
        return [DataFeatureMeta(
            feature_id="afk",
            name="AFK",
            user_export=True,
            user_delete=True,
        )]

    async def data_export_user(self, user_id: int, *, guild_ids: list[int] | None) -> DataExportChunk:
        chunk = DataExportChunk(feature_id="afk")
        async with self.acquire_db() as db:
            afk_rows = await export_table(
                db,
                """SELECT user_id, status, is_global, save_missed_pings, started_at,
                          buffer_until, origin_guild_id, old_nick
                   FROM afk_users WHERE user_id = ?""",
                (user_id,),
            )
            missed_rows = await export_table(
                db,
                """SELECT id, user_id, author_id, guild_id, channel_id,
                          message_id, content, timestamp
                   FROM missed_pings WHERE user_id = ? ORDER BY timestamp ASC""",
                (user_id,),
            )
        if afk_rows:
            chunk.global_data["afk_state"] = afk_rows[0]
        if missed_rows:
            chunk.global_data["missed_pings"] = missed_rows
        return chunk

    async def data_export_guild(self, guild_id: int) -> DataExportChunk:
        return DataExportChunk(feature_id="afk")

    async def data_delete_user(self, user_id: int, *, guild_ids: list[int] | None,
                               feature_id: str | None) -> DataDeleteResult:
        if feature_id and feature_id != "afk":
            return DataDeleteResult(feature_id="afk")
        rows_affected = 0
        async with self.acquire_db() as db:
            cur = await db.execute("DELETE FROM afk_users WHERE user_id = ?", (user_id,))
            rows_affected += cur.rowcount
            cur = await db.execute("DELETE FROM missed_pings WHERE user_id = ?", (user_id,))
            rows_affected += cur.rowcount
            cur = await db.execute("DELETE FROM return_notifications WHERE afk_user_id = ? OR observer_id = ?",
                                   (user_id, user_id))
            rows_affected += cur.rowcount
            await db.commit()
        self.afk_users.pop(user_id, None)
        self.missed_pings_cache.pop(user_id, None)
        self.notification_cache.pop(user_id, None)
        for uid in list(self.notification_cache.keys()):
            self.notification_cache[uid].discard(user_id)
        return DataDeleteResult(feature_id="afk", deleted=True, rows_affected=rows_affected)

    async def data_delete_guild(self, guild_id: int, feature_id: str | None) -> DataDeleteResult:
        return DataDeleteResult(feature_id="afk")

    async def data_monitor_guild(self, guild: discord.Guild) -> DataMonitorResult:
        return DataMonitorResult(feature_id="afk")


async def setup(bot: commands.Bot):
    await bot.add_cog(AFK(bot))