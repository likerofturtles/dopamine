import asyncio
import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiosqlite
from typing import Optional, Dict, List, Any
from contextlib import asynccontextmanager

from config import STICKYDB_PATH
from utils.checks import slash_mod_check


class StickyMessages(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.panel_cache: Dict[int, Dict[str, dict]] = {}
        self.active_channels: Dict[int, dict] = {}
        self.db_pool: Optional[asyncio.Queue[aiosqlite.Connection]] = None

    async def cog_load(self):
        await self.init_pools()
        await self.init_db()
        await self.populate_caches()
        if not self.sticky_monitor.is_running():
            self.sticky_monitor.start()

    async def cog_unload(self):
        if self.sticky_monitor.is_running():
            self.sticky_monitor.cancel()
        if self.db_pool:
            while not self.db_pool.empty():
                conn = await self.db_pool.get()
                await conn.close()

    async def init_pools(self, pool_size: int = 6):
        if self.db_pool is None:
            self.db_pool = asyncio.Queue(maxsize=pool_size)
            for _ in range(pool_size):
                conn = await aiosqlite.connect(STICKYDB_PATH, timeout=5.0)
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
                             CREATE TABLE IF NOT EXISTS sticky_panels
                             (
                                 guild_id INTEGER,
                                 panel_id INTEGER,
                                 name TEXT,
                                 embed_color TEXT,
                                 title TEXT,
                                 description TEXT,
                                 message_content TEXT,
                                 image_url TEXT,
                                 footer TEXT,
                                 channel_id INTEGER,
                                 last_message_id INTEGER,
                                 image_only_mode INTEGER DEFAULT 0,
                                 member_whitelist_enabled INTEGER DEFAULT 0,
                                 member_whitelist_id INTEGER,
                                 PRIMARY KEY (guild_id, panel_id)
                                 )
                             ''')
            for col, dtype in [("embed_color", "TEXT"), ("title", "TEXT"), ("description", "TEXT"),
                               ("footer", "TEXT"), ("member_whitelist_enabled", "INTEGER DEFAULT 0"),
                               ("member_whitelist_id", "INTEGER")]:
                try:
                    await db.execute(f'ALTER TABLE sticky_panels ADD COLUMN {col} {dtype}')
                except aiosqlite.OperationalError:
                    continue
            await db.commit()

    async def populate_caches(self):
        self.panel_cache.clear()
        self.active_channels.clear()
        async with self.acquire_db() as db:
            async with db.execute("SELECT * FROM sticky_panels") as cursor:
                rows = await cursor.fetchall()
                columns = [column[0] for column in cursor.description]
                for row in rows:
                    data = dict(zip(columns, row))
                    g_id = data["guild_id"]
                    c_id = data["channel_id"]

                    if g_id not in self.panel_cache:
                        self.panel_cache[g_id] = {}

                    self.panel_cache[g_id][data["name"]] = data

                    if c_id and data["last_message_id"]:
                        self.active_channels[c_id] = data

    def get_guild_panels(self, guild_id: int) -> List[dict]:
        return list(self.panel_cache.get(guild_id, {}).values())

    @staticmethod
    def parse_color(color_str: str) -> Optional[discord.Color]:
        if not color_str: return None
        color_str = color_str.strip().lstrip('#')
        try:
            return discord.Color(int(color_str, 16))
        except ValueError:
            colors = {'red': discord.Color.red(), 'blue': discord.Color.blue(), 'green': discord.Color.green(),
                      'yellow': discord.Color.gold(), 'orange': discord.Color.orange(),
                      'purple': discord.Color.purple(),
                      'pink': discord.Color.magenta(), 'teal': discord.Color.teal(),
                      'white': discord.Color.from_rgb(255, 255, 255),
                      'black': discord.Color.from_rgb(0, 0, 0)}
            return colors.get(color_str.lower())

    @staticmethod
    def build_panel_embed(data: dict) -> discord.Embed:
        embed = discord.Embed()
        if data.get('embed_color'):
            color = StickyMessages.parse_color(data['embed_color'])
            if color: embed.color = color
        if data.get('title'): embed.title = data['title']
        if data.get('description'): embed.description = data['description']
        if data.get('message_content'):
            if embed.description:
                embed.add_field(name="Message", value=data['message_content'], inline=False)
            else:
                embed.description = data['message_content']
        if data.get('image_url'): embed.set_image(url=data['image_url'])
        if data.get('footer'): embed.set_footer(text=data['footer'])
        return embed

    sticky_group = app_commands.Group(name="sticky", description="Sticky message commands")
    panel_group = app_commands.Group(name="panel", description="Sticky panel commands", parent=sticky_group)

    @panel_group.command(name="setup", description="Create a new sticky panel")
    @app_commands.check(slash_mod_check)
    async def panel_setup(self, interaction: discord.Interaction, name: str, channel: discord.TextChannel):
        if name in self.panel_cache.get(interaction.guild.id, {}):
            return await interaction.response.send_message("A panel with that name already exists.", ephemeral=True)

        panels = self.get_guild_panels(interaction.guild.id)
        next_id = max([p['panel_id'] for p in panels], default=0) + 1

        modal = PanelSetupModal(self.bot, interaction.guild.id, next_id, name, channel.id)
        await interaction.response.send_modal(modal)

    @panel_group.command(name="start", description="Start a sticky panel")
    @app_commands.check(slash_mod_check)
    async def panel_start(self, interaction: discord.Interaction, name: str):
        guild_id = interaction.guild.id
        panel = self.panel_cache.get(guild_id, {}).get(name)

        if not panel:
            return await interaction.response.send_message(f"Panel **{name}** not found.", ephemeral=True)

        if panel['last_message_id']:
            return await interaction.response.send_message("Panel is already active.", ephemeral=True)

        channel = self.bot.get_channel(panel['channel_id'])
        if not channel:
            return await interaction.response.send_message("Channel no longer exists.", ephemeral=True)

        try:
            embed = self.build_panel_embed(panel)
            message = await channel.send(embed=embed)

            async with self.acquire_db() as db:
                await db.execute("UPDATE sticky_panels SET last_message_id = ? WHERE guild_id = ? AND name = ?",
                                 (message.id, guild_id, name))
                await db.commit()

            panel['last_message_id'] = message.id
            self.active_channels[channel.id] = panel

            await interaction.response.send_message(f"Panel **{name}** started in {channel.mention}!", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)

    @panel_group.command(name="stop", description="Stop a sticky panel")
    @app_commands.check(slash_mod_check)
    async def panel_stop(self, interaction: discord.Interaction, name: str):
        guild_id = interaction.guild.id
        panel = self.panel_cache.get(guild_id, {}).get(name)

        if not panel or not panel['last_message_id']:
            return await interaction.response.send_message("Panel is not active.", ephemeral=True)

        channel = self.bot.get_channel(panel['channel_id'])
        if channel:
            try:
                msg = await channel.fetch_message(panel['last_message_id'])
                await msg.delete()
            except:
                pass

        async with self.acquire_db() as db:
            await db.execute("UPDATE sticky_panels SET last_message_id = NULL WHERE guild_id = ? AND name = ?",
                             (guild_id, name))
            await db.commit()

        self.active_channels.pop(panel['channel_id'], None)
        panel['last_message_id'] = None

        await interaction.response.send_message(f"Panel **{name}** stopped.", ephemeral=True)

    @panel_group.command(name="modes", description="Configure sticky panel modes")
    @app_commands.check(slash_mod_check)
    async def panel_modes(self, interaction: discord.Interaction, name: str,
                          image_only: Optional[str] = None, member_whitelist: Optional[str] = None,
                          member: Optional[discord.Member] = None):
        guild_id = interaction.guild.id
        panel = self.panel_cache.get(guild_id, {}).get(name)

        if not panel:
            return await interaction.response.send_message("Panel not found.", ephemeral=True)

        if member_whitelist == "On" and not member:
            return await interaction.response.send_message("Member required for whitelist.", ephemeral=True)

        updates = {}
        if image_only:
            updates['image_only_mode'] = 1 if image_only == "On" else 0
        if member_whitelist:
            updates['member_whitelist_enabled'] = 1 if member_whitelist == "On" else 0
            updates['member_whitelist_id'] = member.id if member_whitelist == "On" else None

        if not updates:
            return await interaction.response.send_message("No changes specified.", ephemeral=True)

        query = f"UPDATE sticky_panels SET {', '.join([f'{k} = ?' for k in updates.keys()])} WHERE guild_id = ? AND name = ?"
        params = list(updates.values()) + [guild_id, name]

        async with self.acquire_db() as db:
            await db.execute(query, params)
            await db.commit()

        panel.update(updates)

        await interaction.response.send_message(f"Updated modes for **{name}**.", ephemeral=True)

    @panel_group.command(name="delete", description="Delete a sticky panel")
    @app_commands.check(slash_mod_check)
    async def panel_delete(self, interaction: discord.Interaction, name: str):
        guild_id = interaction.guild.id
        panel = self.panel_cache.get(guild_id, {}).get(name)

        if not panel:
            return await interaction.response.send_message("Panel not found.", ephemeral=True)

        if panel['last_message_id']:
            channel = self.bot.get_channel(panel['channel_id'])
            if channel:
                try:
                    msg = await channel.fetch_message(panel['last_message_id'])
                    await msg.delete()
                except:
                    pass
            self.active_channels.pop(panel['channel_id'], None)

        async with self.acquire_db() as db:
            await db.execute("DELETE FROM sticky_panels WHERE guild_id = ? AND name = ?", (guild_id, name))
            await db.commit()

        self.panel_cache[guild_id].pop(name)
        await interaction.response.send_message(f"Deleted panel **{name}**.", ephemeral=True)

    @sticky_group.command(name="panels", description="View all sticky panels in this server")
    @app_commands.check(slash_mod_check)
    async def panels(self, interaction: discord.Interaction):
        panels = self.get_guild_panels(interaction.guild.id)
        if not panels:
            return await interaction.response.send_message("No panels found.", ephemeral=True)

        embed = discord.Embed(title="Your Sticky Panels", color=0x337fd5)
        lines = []
        for p in sorted(panels, key=lambda x: x['panel_id']):
            status = "Active" if p['last_message_id'] else "Inactive"
            chan = f"<#{p['channel_id']}>" if p['channel_id'] else "None"
            lines.append(f"**{p['panel_id']}. {p['name']}** | {chan} | {status}")

        embed.description = "\n".join(lines)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tasks.loop(seconds=120)
    async def sticky_monitor(self):
        for channel_id, panel in list(self.active_channels.items()):
            channel = self.bot.get_channel(channel_id)
            if not channel: continue
            try:
                await channel.fetch_message(panel['last_message_id'])
            except discord.NotFound:
                await self.update_sticky_message(panel, channel)
            except:
                continue

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot: return

        panel = self.active_channels.get(message.channel.id)
        if not panel or message.id == panel['last_message_id']: return

        if panel['member_whitelist_enabled'] and message.author.id != panel['member_whitelist_id']:
            return

        if panel['image_only_mode']:
            has_img = bool(message.attachments) or any(getattr(e, "image", None) for e in message.embeds)
            if not has_img: return

        await self.update_sticky_message(panel, message.channel)

    async def update_sticky_message(self, panel: dict, channel: discord.TextChannel):
        try:
            if panel['last_message_id']:
                try:
                    old = await channel.fetch_message(panel['last_message_id'])
                    await old.delete()
                except:
                    pass

            new_msg = await channel.send(embed=self.build_panel_embed(panel))

            async with self.acquire_db() as db:
                await db.execute("UPDATE sticky_panels SET last_message_id = ? WHERE guild_id = ? AND panel_id = ?",
                                 (new_msg.id, panel['guild_id'], panel['panel_id']))
                await db.commit()

            panel['last_message_id'] = new_msg.id
        except Exception as e:
            print(f"Sticky Update Error: {e}")

    # Autocompletes
    @panel_start.autocomplete('name')
    @panel_stop.autocomplete('name')
    @panel_modes.autocomplete('name')
    @panel_delete.autocomplete('name')
    async def panel_name_autocomplete(self, interaction: discord.Interaction, current: str):
        names = list(self.panel_cache.get(interaction.guild.id, {}).keys())
        return [app_commands.Choice(name=n, value=n) for n in names if current.lower() in n.lower()][:25]


class PanelSetupModal(discord.ui.Modal):
    def __init__(self, bot, guild_id: int, panel_id: int, name: str, channel_id: int):
        super().__init__(title="Configure Sticky Panel")
        self.bot = bot
        self.guild_id = guild_id
        self.panel_id = panel_id
        self.name = name
        self.channel_id = channel_id

        self.color_input = discord.ui.TextInput(label="Embed Color", placeholder="Hex or Name", required=False)
        self.title_input = discord.ui.TextInput(label="Title", required=False)
        self.description_input = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph,
                                                      required=False)
        self.image_url_input = discord.ui.TextInput(label="Image URL", required=False)
        self.footer_input = discord.ui.TextInput(label="Footer", required=False)

        for item in [self.color_input, self.title_input, self.description_input, self.image_url_input,
                     self.footer_input]:
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        cog = self.bot.get_cog('StickyMessages')
        data = {
            "guild_id": self.guild_id,
            "panel_id": self.panel_id,
            "name": self.name,
            "embed_color": self.color_input.value or None,
            "title": self.title_input.value or None,
            "description": self.description_input.value or None,
            "message_content": None,
            "image_url": self.image_url_input.value or None,
            "footer": self.footer_input.value or None,
            "channel_id": self.channel_id,
            "last_message_id": None,
            "image_only_mode": 0,
            "member_whitelist_enabled": 0,
            "member_whitelist_id": None
        }

        if data['embed_color'] and not cog.parse_color(data['embed_color']):
            return await interaction.response.send_message("Invalid color.", ephemeral=True)

        async with cog.acquire_db() as db:
            cols = ", ".join(data.keys())
            placeholders = ", ".join(["?"] * len(data))
            await db.execute(f"INSERT INTO sticky_panels ({cols}) VALUES ({placeholders})", list(data.values()))
            await db.commit()

        if self.guild_id not in cog.panel_cache: cog.panel_cache[self.guild_id] = {}
        cog.panel_cache[self.guild_id][self.name] = data

        channel = self.bot.get_channel(self.channel_id)
        if channel:
            msg = await channel.send(embed=cog.build_panel_embed(data))
            async with cog.acquire_db() as db:
                await db.execute("UPDATE sticky_panels SET last_message_id = ? WHERE guild_id = ? AND panel_id = ?",
                                 (msg.id, self.guild_id, self.panel_id))
                await db.commit()
            data['last_message_id'] = msg.id
            cog.active_channels[self.channel_id] = data

        await interaction.response.send_message(f"Panel **{self.name}** created and started!", ephemeral=True)


async def setup(bot):
    await bot.add_cog(StickyMessages(bot))