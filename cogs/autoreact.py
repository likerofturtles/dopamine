import asyncio
import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiosqlite
import time
import re
from typing import Optional, List, Dict, Tuple, Set, Any
from datetime import datetime, timezone
from contextlib import asynccontextmanager

from config import ARDB_PATH
from utils.checks import slash_mod_check

EMOJI_REGEX = re.compile(
    r'(<a?:\w{2,32}:\d{15,25}>)'
    r'|([\U0001F1E6-\U0001F1FF]{2})'
    r'|([\U0001F300-\U0001FAFF]\uFE0F?)'
    r'|([\u2600-\u27BF]\uFE0F?)',
    flags=re.UNICODE
)


class MemberWhitelistUserView(discord.ui.View):
    def __init__(self, cog, panel: Dict):
        super().__init__(timeout=300)
        self.cog = cog
        self.panel = panel

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Select members to whitelist...", min_values=1,
                       max_values=25)
    async def select_users(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        guild_id = interaction.guild_id
        panel_id = self.panel['panel_id']
        key = (guild_id, panel_id)

        selected_members = select.values
        added_names = []

        async with self.cog.acquire_db() as db:
            await db.execute(
                "UPDATE autoreact_panels SET member_whitelist = 1 WHERE guild_id = ? AND panel_id = ?",
                key
            )
            self.panel['member_whitelist'] = 1

            for member in selected_members:
                if member.bot:
                    continue

                await db.execute(
                    "INSERT OR REPLACE INTO autoreact_whitelist (guild_id, panel_id, user_id) VALUES (?, ?, ?)",
                    (guild_id, panel_id, member.id)
                )

                if key not in self.cog.whitelist_cache:
                    self.cog.whitelist_cache[key] = set()
                self.cog.whitelist_cache[key].add(member.id)

                added_names.append(member.display_name)

            await db.commit()

        if not added_names:
            return await interaction.response.send_message("No valid (non-bot) members were added.", ephemeral=True)

        await interaction.response.send_message(
            f"Successfully whitelisted: **{', '.join(added_names)}** for panel **{self.panel['name']}**.",
            ephemeral=True
        )

class AutoReact(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db_pool: Optional[asyncio.Queue] = None

        self.panel_cache: Dict[Tuple[int, int], Dict[str, Any]] = {}
        self.whitelist_cache: Dict[Tuple[int, int], Set[int]] = {}

        self._reaction_queue: asyncio.Queue[Tuple[discord.Message, str]] = asyncio.Queue()
        self._reaction_semaphore = asyncio.Semaphore(5)
        self._reaction_task: Optional[asyncio.Task] = None

    async def cog_load(self):
        await self.init_pools()
        await self.init_db()
        await self.populate_caches()
        if self._reaction_task is None or self._reaction_task.done():
            self._reaction_task = asyncio.create_task(self.reaction_processor())

    async def cog_unload(self):
        if self._reaction_task is not None:
            self._reaction_task.cancel()

        if self.db_pool:
            while not self.db_pool.empty():
                conn = await self.db_pool.get()
                await conn.close()

    async def init_pools(self, pool_size: int = 5):
        if self.db_pool is None:
            self.db_pool = asyncio.Queue(maxsize=pool_size)
            for _ in range(pool_size):
                conn = await aiosqlite.connect(ARDB_PATH, timeout=5.0)
                await conn.execute("PRAGMA busy_timeout=5000")
                await conn.execute("PRAGMA journal_mode=WAL")
                await conn.execute("PRAGMA synchronous=NORMAL")
                await conn.execute("PRAGMA foreign_keys=ON")
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
                             CREATE TABLE IF NOT EXISTS autoreact_panels
                             (
                                 guild_id INTEGER,
                                 panel_id INTEGER,
                                 name TEXT,
                                 emoji TEXT,
                                 channel_id INTEGER,
                                 is_active INTEGER DEFAULT 0,
                                 member_whitelist INTEGER DEFAULT 0,
                                 image_only_mode INTEGER DEFAULT 0,
                                 started_at REAL, 
                                 PRIMARY KEY (guild_id, panel_id)
                                 )
                             ''')
            await db.execute('''
                             CREATE TABLE IF NOT EXISTS autoreact_whitelist
                             (
                                 guild_id INTEGER,
                                 panel_id INTEGER,
                                 user_id INTEGER,
                                 PRIMARY KEY (guild_id, panel_id, user_id)
                                 )
                             ''')
            await db.commit()

    async def populate_caches(self):
        self.panel_cache.clear()
        self.whitelist_cache.clear()

        async with self.acquire_db() as db:
            async with db.execute("SELECT * FROM autoreact_panels") as cursor:
                rows = await cursor.fetchall()
                cols = [c[0] for c in cursor.description]
                for row in rows:
                    data = dict(zip(cols, row))
                    key = (data['guild_id'], data['panel_id'])
                    # Convert stored pipe string back to list for cache efficiency
                    data['emoji_list'] = self.deserialize_emojis(data['emoji'])
                    self.panel_cache[key] = data

            async with db.execute("SELECT guild_id, panel_id, user_id FROM autoreact_whitelist") as cursor:
                rows = await cursor.fetchall()
                for g_id, p_id, u_id in rows:
                    key = (g_id, p_id)
                    if key not in self.whitelist_cache:
                        self.whitelist_cache[key] = set()
                    self.whitelist_cache[key].add(u_id)

    def parse_emoji_input(self, emoji_input: str) -> List[str]:
        if not emoji_input: return []
        tokens = []
        parts = [p.strip() for p in re.split(r'[,\s]+', emoji_input) if p.strip()]
        for part in parts:
            matches = [m.group(0) for m in EMOJI_REGEX.finditer(part)]
            if matches:
                tokens.extend(matches)
            else:
                tokens.append(part)
        return tokens

    def serialize_emojis(self, emojis: List[str]) -> str:
        return '|'.join(emojis)

    def deserialize_emojis(self, value: str) -> List[str]:
        if not value: return []
        if '|' in value:
            parts = value.split('|')
        elif ',' in value:
            parts = [p.strip() for p in value.split(',')]
        elif ' ' in value:
            parts = [p.strip() for p in value.split(' ')]
        else:
            parts = [value.strip()]
        return [p for p in parts if p]

    def format_emojis_for_display(self, emojis: List[str]) -> str:
        return ', '.join(emojis) if emojis else 'None'

    autoreact_group = app_commands.Group(name="autoreact", description="AutoReact commands")
    panel_group = app_commands.Group(name="panel", description="AutoReact panel management", parent=autoreact_group)
    member_group = app_commands.Group(name="member", description="AutoReact member settings", parent=autoreact_group)
    image_group = app_commands.Group(name="image", description="AutoReact image settings", parent=autoreact_group)

    async def panel_name_autocomplete(self, interaction: discord.Interaction, current: str):
        current_lower = current.lower()
        choices = [
            app_commands.Choice(
                name=f"{data['name']} ({data['panel_id']})",
                value=data['name']
            )
            for (g_id, p_id), data in self.panel_cache.items()
            if g_id == interaction.guild_id and current_lower in data['name'].lower()
        ]
        return choices[:25]

    @panel_group.command(name="setup", description="Create a new autoreact panel")
    @app_commands.check(slash_mod_check)
    async def setup_autoreact_panel(self, interaction: discord.Interaction, name: str, emojis: str,
                                    channel: discord.TextChannel):
        if not await self.bot.get_cog('TopGGVoter').check_vote_access(interaction.user.id):
            embed = discord.Embed(
                title="Vote to Use This Feature!",
                description="This command requires voting! To access this feature, please vote for Dopamine here: [top.gg](https://top.gg/bot/{bot_id})".format(
                    bot_id=self.bot.user.id
                ),
                color=0xffaa00
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        parsed = self.parse_emoji_input(emojis)
        if not (0 < len(parsed) <= 3):
            return await interaction.response.send_message("Provide 1-3 valid emojis.", ephemeral=True)

        guild_panels = [p for (g, pid), p in self.panel_cache.items() if g == interaction.guild.id]
        if len(guild_panels) >= 3:
            return await interaction.response.send_message("Maximum of 3 panels reached.", ephemeral=True)

        existing_ids = {p['panel_id'] for p in guild_panels}
        panel_id = next(i for i in range(1, 5) if i not in existing_ids)

        now = time.time()
        serialized = self.serialize_emojis(parsed)

        async with self.acquire_db() as db:
            await db.execute('''
                             INSERT INTO autoreact_panels (guild_id, panel_id, name, emoji, channel_id, is_active, started_at)
                             VALUES (?, ?, ?, ?, ?, 1, ?)
                             ''', (interaction.guild.id, panel_id, name, serialized, channel.id, now))
            await db.commit()

        self.panel_cache[(interaction.guild.id, panel_id)] = {
            "guild_id": interaction.guild.id, "panel_id": panel_id, "name": name,
            "emoji": serialized, "emoji_list": parsed, "channel_id": channel.id,
            "is_active": 1, "member_whitelist": 0, "image_only_mode": 0, "started_at": now
        }

        await interaction.response.send_message(embed=discord.Embed(title="Panel created successfully", description=f"Panel **{name}** created and started successfully."), ephemeral=True)

    @panel_group.command(name="list", description="View all autoreact panels")
    @app_commands.check(slash_mod_check)
    async def autoreact_panels(self, interaction: discord.Interaction):
        guild_panels = [p for (g, pid), p in self.panel_cache.items() if g == interaction.guild.id]
        if not guild_panels:
            return await interaction.response.send_message("No panels found.", ephemeral=True)

        embed = discord.Embed(title="Your AutoReact Panels", color=0x337fd5)
        desc = ""
        for p in sorted(guild_panels, key=lambda x: x['panel_id']):
            chan = f"<#{p['channel_id']}>"
            emojis = self.format_emojis_for_display(p['emoji_list'])
            status = '🟢 Active' if p['is_active'] else '🔴 Inactive'
            wl_count = len(self.whitelist_cache.get((p['guild_id'], p['panel_id']), [])) if p[
                'member_whitelist'] else "All users"

            desc += f"## {p['panel_id']}. {p['name']}\n"
            desc += f"* **Emoji(s):** {emojis}\n* **Channel:** {chan}\n* **Status:** {status}\n"
            desc += f"* **Target:** {wl_count}\n* **Mode:** {'Image-only' if p['image_only_mode'] else 'All'}\n\n"

        embed.description = desc
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @panel_group.command(name="start", description="Start a panel")
    @app_commands.check(slash_mod_check)
    @app_commands.autocomplete(name=panel_name_autocomplete)
    async def start_autoreact_panel(self, interaction: discord.Interaction, name: str):
        target = next(
            (p for (g, pid), p in self.panel_cache.items() if g == interaction.guild_id and p['name'] == name), None)
        if not target: return await interaction.response.send_message("Panel not found.", ephemeral=True)

        now = time.time()
        async with self.acquire_db() as db:
            await db.execute(
                "UPDATE autoreact_panels SET is_active = 1, started_at = ? WHERE guild_id = ? AND panel_id = ?",
                (now, interaction.guild_id, target['panel_id']))
            await db.commit()

        target['is_active'] = 1
        target['started_at'] = now
        await interaction.response.send_message(embed=discord.Embed(title="AutoReact Stopped",description=f"Panel **{name}** has been sstarted successfully.", color=discord.Color.green()), ephemeral=True)

    @panel_group.command(name="stop", description="Stop a panel")
    @app_commands.check(slash_mod_check)
    @app_commands.autocomplete(name=panel_name_autocomplete)
    async def stop_autoreact_panel(self, interaction: discord.Interaction, name: str):
        target = next(
            (p for (g, pid), p in self.panel_cache.items() if g == interaction.guild_id and p['name'] == name), None)
        if not target: return await interaction.response.send_message("Panel not found.", ephemeral=True)

        async with self.acquire_db() as db:
            await db.execute("UPDATE autoreact_panels SET is_active = 0 WHERE guild_id = ? AND panel_id = ?",
                             (interaction.guild_id, target['panel_id']))
            await db.commit()

        target['is_active'] = 0
        await interaction.response.send_message(f"Stopped **{name}**.", ephemeral=True)

    @panel_group.command(name="delete", description="Delete a panel")
    @app_commands.check(slash_mod_check)
    @app_commands.autocomplete(name=panel_name_autocomplete)
    async def delete_autoreact_panel(self, interaction: discord.Interaction, name: str):
        target = next(
            (p for (g, pid), p in self.panel_cache.items() if g == interaction.guild_id and p['name'] == name), None)
        if not target: return await interaction.response.send_message("Panel not found.", ephemeral=True)

        key = (interaction.guild_id, target['panel_id'])
        async with self.acquire_db() as db:
            await db.execute("DELETE FROM autoreact_whitelist WHERE guild_id = ? AND panel_id = ?", key)
            await db.execute("DELETE FROM autoreact_panels WHERE guild_id = ? AND panel_id = ?", key)
            await db.commit()

        self.panel_cache.pop(key, None)
        self.whitelist_cache.pop(key, None)
        await interaction.response.send_message(

            embed=discord.Embed(title="AutoReact Panel Deleted", description=f"Deleted panel **{name}**.", color=discord.Color.green()), ephemeral=True)

    @panel_group.command(name="edit", description="Edit a panel")
    @app_commands.check(slash_mod_check)
    @app_commands.autocomplete(name=panel_name_autocomplete)
    async def edit_autoreact_panel(self, interaction: discord.Interaction, name: str, emoji: Optional[str] = None, channel: Optional[discord.TextChannel] = None, new_name: Optional[str] = None):
        target = next(
            (p for (g, pid), p in self.panel_cache.items() if g == interaction.guild_id and p['name'] == name), None)
        if not target: return await interaction.response.send_message("Panel not found.", ephemeral=True)

        updates = []
        params = []
        if emoji:
            parsed = self.parse_emoji_input(emoji)
            if 0 < len(parsed) <= 3:
                updates.append("emoji = ?")
                params.append(self.serialize_emojis(parsed))
                target['emoji_list'] = parsed
                target['emoji'] = self.serialize_emojis(parsed)
        if channel:
            updates.append("channel_id = ?")
            params.append(channel.id)
            target['channel_id'] = channel.id
        if new_name:
            updates.append("name = ?")
            params.append(new_name)
            target['name'] = new_name

        if not updates: return await interaction.response.send_message("No changes provided.", ephemeral=True)

        params.extend([interaction.guild_id, target['panel_id']])
        async with self.acquire_db() as db:
            await db.execute(f"UPDATE autoreact_panels SET {', '.join(updates)} WHERE guild_id = ? AND panel_id = ?",
                             params)
            await db.commit()

        await interaction.response.send_message(embed=discord.Embed(title="AutoReact Panel Updated", description=f"Updated panel **{name}**.", color=discord.Color.green()), ephemeral=True)

    @member_group.command(name="whitelist", description="Manage whitelisted members for a panel")
    @app_commands.check(slash_mod_check)
    @app_commands.autocomplete(name=panel_name_autocomplete)
    @app_commands.describe(name="The name of the panel to manage")
    async def autoreact_member_whitelist(self, interaction: discord.Interaction, name: str):
        target = next(
            (p for (g, pid), p in self.panel_cache.items()
             if g == interaction.guild_id and p['name'] == name),
            None
        )

        if not target:
            return await interaction.response.send_message("Panel not found.", ephemeral=True)

        view = MemberWhitelistUserView(self, target)

        embed = discord.Embed(
            title=f"Whitelist Management: {target['name']}",
            description="Select or remove members from the dropdown below to manage who can trigger AutoReact for this panel.",
            color=0x337fd5
        )

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    image_group.command(name="only", description="Toggle image-only mode for a specific panel")

    @app_commands.check(slash_mod_check)
    @app_commands.autocomplete(name=panel_name_autocomplete)
    @app_commands.describe(name="The name of the panel", enabled="Whether image-only mode should be on or off")
    async def autoreact_image_only_mode(self, interaction: discord.Interaction, name: str, enabled: bool):
        target = next(
            (p for (g, pid), p in self.panel_cache.items()
             if g == interaction.guild_id and p['name'] == name),
            None
        )

        if not target:
            return await interaction.response.send_message("Panel not found.", ephemeral=True)

        mode_val = 1 if enabled else 0

        async with self.acquire_db() as db:
            await db.execute(
                "UPDATE autoreact_panels SET image_only_mode = ? WHERE guild_id = ? AND panel_id = ?",
                (mode_val, interaction.guild_id, target['panel_id'])
            )
            await db.commit()

        target['image_only_mode'] = mode_val

        status_text = "enabled" if enabled else "disabled"
        embed = discord.Embed(
            title="Image-Only Mode Updated",
            description=f"Image-only mode has been **{status_text}** for panel: **{name}**.",
            color=discord.Color.green() if enabled else discord.Color.red()
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        for (g_id, p_id), panel in self.panel_cache.items():
            if g_id != message.guild.id or panel['channel_id'] != message.channel.id or not panel['is_active']:
                continue

            if panel['image_only_mode']:
                has_img = bool(message.attachments) or any(e.type == 'image' for e in message.embeds)
                if not has_img: continue

            if panel['member_whitelist']:
                allowed = self.whitelist_cache.get((g_id, p_id), set())
                if message.author.id not in allowed: continue
            for em in panel['emoji_list']:
                await self._reaction_queue.put((message, em))

    async def reaction_processor(self):
        while True:
            try:
                message, em = await self._reaction_queue.get()
                async with self._reaction_semaphore:
                    try:
                        await message.add_reaction(em)
                    except:
                        pass
                self._reaction_queue.task_done()
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                break
            except:
                continue


class MemberWhitelistSelectionView(discord.ui.View):
    def __init__(self, cog: AutoReact, guild_id: int, user_id: int, panels: List[Dict]):
        super().__init__(timeout=300)
        self.cog = cog
        for p in panels:
            btn = discord.ui.Button(label=f"{p['panel_id']}. {p['name']}", style=discord.ButtonStyle.primary)
            btn.callback = self.make_callback(p, guild_id, user_id)
            self.add_item(btn)

    def make_callback(self, panel, guild_id, user_id):
        async def callback(interaction: discord.Interaction):
            key = (guild_id, panel['panel_id'])
            async with self.cog.acquire_db() as db:
                await db.execute("UPDATE autoreact_panels SET member_whitelist = 1 WHERE guild_id = ? AND panel_id = ?",
                                 key)
                await db.execute("INSERT OR REPLACE INTO autoreact_whitelist VALUES (?, ?, ?)",
                                 (guild_id, panel['panel_id'], user_id))
                await db.commit()

            panel['member_whitelist'] = 1
            if key not in self.cog.whitelist_cache: self.cog.whitelist_cache[key] = set()
            self.cog.whitelist_cache[key].add(user_id)

            await interaction.response.send_message(f"Whitelisted for **{panel['name']}**.", ephemeral=True)

        return callback


class ImageOnlyModeSelectionView(discord.ui.View):
    def __init__(self, cog: AutoReact, guild_id: int, panels: List[Dict]):
        super().__init__(timeout=300)
        self.cog = cog
        for p in panels:
            btn = discord.ui.Button(label=f"{p['panel_id']}. {p['name']}", style=discord.ButtonStyle.primary)
            btn.callback = self.make_callback(p, guild_id)
            self.add_item(btn)

    def make_callback(self, panel, guild_id):
        async def callback(interaction: discord.Interaction):
            async with self.cog.acquire_db() as db:
                await db.execute("UPDATE autoreact_panels SET image_only_mode = 1 WHERE guild_id = ? AND panel_id = ?",
                                 (guild_id, panel['panel_id']))
                await db.commit()

            panel['image_only_mode'] = 1
            await interaction.response.send_message(f"Image-only enabled for **{panel['name']}**.", ephemeral=True)

        return callback


async def setup(bot):
    await bot.add_cog(AutoReact(bot))