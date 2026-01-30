import asyncio
import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiosqlite
from typing import Optional, Dict, List, Any
import time
from contextlib import asynccontextmanager

from config import STICKYDB_PATH
from utils.checks import slash_mod_check



def parse_color(value: str) -> Optional[discord.Color]:
    if not value:
        return None

    val = value.strip().lower()

    if hasattr(discord.Color, val.replace(" ", "_")):
        method = getattr(discord.Color, val.replace(" ", "_"))
        if callable(method):
            try:
                return method()
            except:
                pass

    hex_val = val.lstrip('#')
    if len(hex_val) == 6:
        try:
            return discord.Color(int(hex_val, 16))
        except:
            pass

    if ',' in val:
        try:
            parts = [int(p.strip()) for p in val.split(',')]
            if len(parts) == 3:
                return discord.Color.from_rgb(*parts)
        except:
            pass

    return None


class PrivateLayoutView(discord.ui.LayoutView):
    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("This isn't for you!", ephemeral=True)
            return False
        return True


class DestructiveConfirmationView(PrivateLayoutView):
    def __init__(self, user, title_name, cog, guild_id):
        super().__init__(user, timeout=30)
        self.title_name = title_name
        self.cog = cog
        self.guild_id = guild_id
        self.value = None
        self.title_text = "Delete Sticky Message"
        self.body_text = f"Are you sure you want to permanently delete the sticky message **{title_name}**? This cannot be undone."
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay(f"### {self.title_text}"))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(self.body_text))

        if self.value is None:
            action_row = discord.ui.ActionRow()
            cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.gray)
            confirm = discord.ui.Button(label="Delete Permanently", style=discord.ButtonStyle.red)

            cancel.callback = self.cancel_callback
            confirm.callback = self.confirm_callback

            action_row.add_item(cancel)
            action_row.add_item(confirm)
            container.add_item(discord.ui.Separator())
            container.add_item(action_row)

        self.add_item(container)

    async def update_view(self, interaction: discord.Interaction, title: str):
        self.title_text = title
        self.body_text = f"~~{self.body_text}~~"
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
        if self.value is None:
            await self.update_view(interaction, "Timed Out", discord.Color(0xdf5046))
            self.stop()


class EditPage(PrivateLayoutView):
    def __init__(self, user, cog, guild_id, panel_data):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.panel_data = panel_data
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        p = self.panel_data
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay(f"## Edit: {p['title']}"))
        container.add_item(discord.ui.Separator())

        bots_enabled = p.get('include_bots', 1) == 1
        details = (
            f"**Channel:** <#{p['channel_id']}>\n"
            f"**Color:** `{p.get('embed_color') or 'Default'}`\n"
            f"**Duration:** `{p.get('conversation_duration', 10)}s`\n"
            f"**Include Bots:** `{'Yes' if bots_enabled else 'No'}`\n"
            f"**Description:** {p.get('description') or '*None*'}"
        )
        container.add_item(discord.ui.TextDisplay(details))
        container.add_item(discord.ui.Separator())

        row1 = discord.ui.ActionRow()
        btn_edit_message = discord.ui.Button(label="Edit Message", style=discord.ButtonStyle.secondary)
        btn_edit_message.callback = self.edit_message_callback
        btn_edit_channel = discord.ui.Button(label="Edit Channel", style=discord.ButtonStyle.secondary)
        btn_edit_channel.callback = self.edit_channel_callback
        btn_delete = discord.ui.Button(label="Delete", style=discord.ButtonStyle.danger)
        btn_delete.callback = self.delete_callback
        btn_duration = discord.ui.Button(label="Edit Duration", style=discord.ButtonStyle.secondary)
        btn_duration.callback = self.edit_duration_callback
        btn_bots = discord.ui.Button(label=f"Include Bots: {'ON' if bots_enabled else 'OFF'}",
                                     style=discord.ButtonStyle.primary if bots_enabled else discord.ButtonStyle.secondary)
        btn_bots.callback = self.toggle_bots_callback

        row1.add_item(btn_edit_message)
        row1.add_item(btn_edit_channel)
        row1.add_item(btn_duration)
        row1.add_item(btn_bots)
        row1.add_item(btn_delete)
        container.add_item(row1)

        back_row = discord.ui.ActionRow()
        btn_back = discord.ui.Button(label="Return to Manage Menu", style=discord.ButtonStyle.secondary)
        btn_back.callback = self.back_callback
        back_row.add_item(btn_back)
        container.add_item(back_row)

        self.add_item(container)

    async def edit_message_callback(self, interaction: discord.Interaction):
        modal = StickySetupModal(self.cog, self.guild_id, self.panel_data['channel_id'], is_edit=True,
                                 original_title=self.panel_data['title'])
        await interaction.response.send_modal(modal)

    async def edit_channel_callback(self, interaction: discord.Interaction):
        view = ChannelSelectView(self.user, self.cog, self.guild_id, is_rebind=True,
                                 panel_title=self.panel_data['title'])
        await interaction.response.send_message(view=view,
                                                ephemeral=True)

    async def delete_callback(self, interaction: discord.Interaction):
        view = DestructiveConfirmationView(self.user, self.panel_data['title'], self.cog, self.guild_id)
        await interaction.response.send_message(view=view)

    async def back_callback(self, interaction: discord.Interaction):
        view = ManagePage(self.user, self.cog, self.guild_id)
        await interaction.response.edit_message(view=view)

    async def edit_duration_callback(self, interaction: discord.Interaction):
        modal = DurationModal(self.cog, self.guild_id, self.panel_data['title'], parent_view=self)
        await interaction.response.send_modal(modal)

    async def toggle_bots_callback(self, interaction: discord.Interaction):
        title = self.panel_data['title']
        panel = self.cog.panel_cache[self.guild_id][title]

        new_val = 0 if panel.get('include_bots', 1) else 1

        async with self.cog.acquire_db() as db:
            await db.execute("UPDATE sticky_panels SET include_bots = ? WHERE guild_id = ? AND title = ?",
                             (new_val, self.guild_id, title))
            await db.commit()

        panel['include_bots'] = new_val
        self.panel_data['include_bots'] = new_val

        self.build_layout()
        await interaction.response.edit_message(view=self)


class ManagePage(PrivateLayoutView):
    def __init__(self, user, cog, guild_id):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        panels = self.cog.get_guild_panels(self.guild_id)
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("## Manage Sticky Messages"))
        container.add_item(discord.ui.TextDisplay(
            "List of all existing sticky messages. Click Edit to configure details or the channel."))
        container.add_item(discord.ui.Separator())

        if not panels:
            container.add_item(discord.ui.TextDisplay("*No sticky messages found.*"))
        else:
            for idx, panel in enumerate(panels, 1):
                p_title = panel['title']
                chan_id = panel['channel_id']

                async def edit_nav(interaction, data=panel):
                    view = EditPage(self.user, self.cog, self.guild_id, data)
                    await interaction.response.edit_message(view=view)

                btn_edit = discord.ui.Button(label="Edit", style=discord.ButtonStyle.secondary)
                btn_edit.callback = edit_nav
                display_text = f"{idx}. **{p_title}** in <#{chan_id}>"
                container.add_item(discord.ui.Section(discord.ui.TextDisplay(display_text), accessory=btn_edit))

        container.add_item(discord.ui.Separator())
        row = discord.ui.ActionRow()
        return_btn = discord.ui.Button(label="Return to Dashboard", style=discord.ButtonStyle.secondary)
        return_btn.callback = self.return_callback
        row.add_item(return_btn)
        container.add_item(row)
        self.add_item(container)

    async def return_callback(self, interaction: discord.Interaction):
        view = StickyDashboard(self.user, self.cog, self.guild_id)
        await interaction.response.edit_message(view=view)


class ChannelSelectView(PrivateLayoutView):
    def __init__(self, user, cog, guild_id, is_rebind=False, panel_title=None):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.is_rebind = is_rebind
        self.panel_title = panel_title
        self.build_layout()

    def build_layout(self):
        container = discord.ui.Container()

        self.select = discord.ui.ChannelSelect(
            placeholder="Select a channel...",
            channel_types=[discord.ChannelType.text],
            min_values=1, max_values=1
        )
        self.select.callback = self.select_callback

        row = discord.ui.ActionRow()
        row.add_item(self.select)
        container.add_item(discord.ui.TextDisplay("### Select a channel for the Sticky Message:"))
        container.add_item(row)
        self.add_item(container)

    async def select_callback(self, interaction: discord.Interaction):
        selected_channel = self.select.values[0]

        if self.is_rebind:
            panel = self.cog.panel_cache[self.guild_id][self.panel_title]
            old_channel_id = panel['channel_id']
            panel['channel_id'] = selected_channel.id

            async with self.cog.acquire_db() as db:
                await db.execute(
                    "UPDATE sticky_panels SET channel_id = ?, last_message_id = NULL WHERE guild_id = ? AND title = ?",
                    (selected_channel.id, self.guild_id, self.panel_title)
                )
                await db.commit()

            self.cog.active_channels.pop(old_channel_id, None)
            self.cog.active_channels[selected_channel.id] = panel

            await interaction.response.edit_message(
                content=f"✅ Moved **{self.panel_title}** to {selected_channel.mention}", view=None)


            new_channel = self.cog.bot.get_channel(selected_channel.id)
            if new_channel:
                await self.cog.update_sticky_message(panel, new_channel)

        else:
            modal = StickySetupModal(self.cog, self.guild_id, selected_channel.id, is_edit=False)
            await interaction.response.send_modal(modal)


class StickyDashboard(PrivateLayoutView):
    def __init__(self, user, cog, guild_id):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.panels = self.cog.get_guild_panels(guild_id)
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        has_panels = len(self.panels) > 0
        bots_enabled = self.panels[0].get('include_bots', 1) == 1 if has_panels else True

        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("## Sticky Messages Dashboard"))
        container.add_item(discord.ui.TextDisplay("This is the dashboard for Dopamine's Sticky Messages feature. Sticky messages allow you to pin important information at the bottom of a channel."))
        container.add_item(discord.ui.Separator())

        container.add_item(discord.ui.TextDisplay(
                "* **Conversation Detection:** Dopamine automatically detects a conversation if 2 messages are sent within 5 seconds, and pauses sending the sticky message to avoid spam. The duration that Dopamine will wait after the last message can be customized."))

        container.add_item(discord.ui.TextDisplay(
                "* **Bot Detection:** Choose whether Dopamine should re-send the sticky message if a bot sends a message or ignore bots."))

        container.add_item(discord.ui.TextDisplay("To customize the above and more for a Sticky Message or to create a new Sticky Message, use the buttons below."))

        container.add_item(discord.ui.Separator())
        row = discord.ui.ActionRow()
        btn_create = discord.ui.Button(label="Create", style=discord.ButtonStyle.primary)
        btn_create.callback = self.create_callback
        btn_manage = discord.ui.Button(label="Manage & Edit", style=discord.ButtonStyle.secondary)
        btn_manage.callback = self.manage_callback
        row.add_item(btn_create)
        row.add_item(btn_manage)
        container.add_item(row)
        self.add_item(container)

    async def create_callback(self, interaction: discord.Interaction):
        view = ChannelSelectView(self.user, self.cog, self.guild_id)
        await interaction.response.send_message(view=view, ephemeral=True)

    async def manage_callback(self, interaction: discord.Interaction):
        view = ManagePage(self.user, self.cog, self.guild_id)
        await interaction.response.edit_message(view=view)


class PanelSelectView(PrivateLayoutView):
    def __init__(self, user, panels, placeholder, callback_func):
        super().__init__(user)
        self.placeholder = placeholder
        self.panels = panels
        self.callback_func = callback_func
        self.build_layout()

    def build_layout(self):
        container = discord.ui.Container()
        options = [discord.SelectOption(label=p['title'], value=p['title']) for p in self.panels[:25]]
        select = discord.ui.Select(placeholder=self.placeholder, options=options)
        select.callback = self.select_callback
        row = discord.ui.ActionRow()
        row.add_item(select)
        container.add_item(discord.ui.TextDisplay("### Select the sticky message whose setting you want to change: "))
        container.add_item(row)
        self.add_item(container)

    async def select_callback(self, interaction: discord.Interaction):
        await self.callback_func(interaction, interaction.data['values'][0])


class DurationModal(discord.ui.Modal):
    def __init__(self, cog, guild_id, title_name, parent_view: EditPage):
        super().__init__(title="Edit Duration")
        self.cog = cog
        self.guild_id = guild_id
        self.title_name = title_name
        self.parent_view = parent_view
        self.duration = discord.ui.TextInput(label="Duration (seconds)", placeholder="10", max_length=2)
        self.add_item(self.duration)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            val = int(self.duration.value)
            if not 0 <= val <= 60: raise ValueError
        except ValueError:
            return await interaction.response.send_message("Enter a number between 0 and 60.", ephemeral=True)

        async with self.cog.acquire_db() as db:
            await db.execute("UPDATE sticky_panels SET conversation_duration = ? WHERE guild_id = ? AND title = ?",
                             (val, self.guild_id, self.title_name))
            await db.commit()

        self.cog.panel_cache[self.guild_id][self.title_name]['conversation_duration'] = val
        self.parent_view.panel_data['conversation_duration'] = val  # Update view's local data

        self.parent_view.build_layout()
        await interaction.response.edit_message(view=self.parent_view)


class StickySetupModal(discord.ui.Modal):
    def __init__(self, cog, guild_id, channel_id, is_edit=False, original_title=None):
        super().__init__(title="Configure Sticky Message")
        self.cog = cog
        self.guild_id = guild_id
        self.is_edit = is_edit
        self.channel_id = channel_id
        self.original_title = original_title

        self.color_input = discord.ui.TextInput(label="Embed Color (Hex, RGB, or Name)", placeholder="#FFFFFF or blue",
                                                required=False)
        self.title_input = discord.ui.TextInput(label="Embed Title (Identifier)", default=original_title or "",
                                                required=True)
        self.description_input = discord.ui.TextInput(label="Embed Description", style=discord.TextStyle.paragraph,
                                                      required=False)
        self.footer_input = discord.ui.TextInput(label="Embed Footer", required=False)
        self.image_url_input = discord.ui.TextInput(label="Embed Image URL", required=False)

        self.add_item(self.color_input)
        self.add_item(self.title_input)
        self.add_item(self.description_input)
        self.add_item(self.footer_input)
        self.add_item(self.image_url_input)

        if is_edit:
            data = cog.panel_cache[guild_id].get(original_title, {})
            self.color_input.default = data.get('embed_color', '')
            self.description_input.default = data.get('description', '')
            self.footer_input.default = data.get('footer', '')
            self.image_url_input.default = data.get('image_url', '')

    async def on_submit(self, interaction: discord.Interaction):
        title = self.title_input.value
        color_val = self.color_input.value

        if color_val and not parse_color(color_val):
            return await interaction.response.send_message("Invalid color format provided.", ephemeral=True)

        if not self.is_edit and title in self.cog.panel_cache.get(self.guild_id, {}):
            return await interaction.response.send_message("A sticky message with that title already exists.",
                                                           ephemeral=True)

        data = {
            "guild_id": self.guild_id,
            "title": title,
            "embed_color": color_val or None,
            "description": self.description_input.value or None,
            "image_url": self.image_url_input.value or None,
            "footer": self.footer_input.value or None,
            "channel_id": self.channel_id,
            "last_message_id": None,
            "conversation_duration": 10,
            "include_bots": 1,
            "panel_id": int(time.time())
        }

        async with self.cog.acquire_db() as db:
            if self.is_edit:
                old_data = self.cog.panel_cache[self.guild_id].pop(self.original_title)
                data.update({k: v for k, v in old_data.items() if
                             k not in ["title", "description", "footer", "image_url", "embed_color"]})
                await db.execute("""UPDATE sticky_panels SET title=?, description=?, footer=?, image_url=?, embed_color=?
                                    WHERE guild_id=? AND title=?""",
                                 (title, data['description'], data['footer'], data['image_url'], color_val,
                                  self.guild_id, self.original_title))
                msg = f"Sticky message **{title}** updated."
            else:
                cols = ", ".join(data.keys());
                placeholders = ", ".join(["?"] * len(data))
                await db.execute(f"INSERT INTO sticky_panels ({cols}) VALUES ({placeholders})", list(data.values()))
                msg = f"Sticky message **{title}** created!"
            await db.commit()

        if self.guild_id not in self.cog.panel_cache: self.cog.panel_cache[self.guild_id] = {}
        self.cog.panel_cache[self.guild_id][title] = data

        channel = self.cog.bot.get_channel(self.channel_id)
        if channel: await self.cog.update_sticky_message(data, channel)
        await interaction.response.send_message(msg, ephemeral=True)


class StickyMessages(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.panel_cache: Dict[int, Dict[str, dict]] = {}
        self.active_channels: Dict[int, dict] = {}
        self.db_pool = None
        self.last_message_time: Dict[int, float] = {}
        self.last_activity: Dict[int, float] = {}
        self.sticky_tasks: Dict[int, asyncio.Task] = {}

    async def cog_load(self):
        await self.init_pools()
        await self.init_db()
        await self.populate_caches()
        if not self.sticky_monitor.is_running(): self.sticky_monitor.start()

    async def cog_unload(self):
        if self.sticky_monitor.is_running(): self.sticky_monitor.cancel()
        for t in self.sticky_tasks.values(): t.cancel()
        if self.db_pool:
            while not self.db_pool.empty():
                await (await self.db_pool.get()).close()

    async def init_pools(self, pool_size=6):
        if self.db_pool is None:
            self.db_pool = asyncio.Queue(maxsize=pool_size)
            for _ in range(pool_size):
                conn = await aiosqlite.connect(STICKYDB_PATH)
                await conn.execute("PRAGMA journal_mode=WAL")
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
            await db.execute('''CREATE TABLE IF NOT EXISTS sticky_panels (
                guild_id INTEGER, panel_id INTEGER, title TEXT, description TEXT, footer TEXT, 
                image_url TEXT, embed_color TEXT, channel_id INTEGER, last_message_id INTEGER,
                conversation_duration INTEGER DEFAULT 10, include_bots INTEGER DEFAULT 1,
                PRIMARY KEY (guild_id, panel_id))''')
            await db.commit()

    async def populate_caches(self):
        async with self.acquire_db() as db:
            async with db.execute("SELECT * FROM sticky_panels") as cursor:
                rows = await cursor.fetchall()
                cols = [c[0] for c in cursor.description]
                for r in rows:
                    d = dict(zip(cols, r))
                    self.panel_cache.setdefault(d["guild_id"], {})[d["title"]] = d
                    if d["channel_id"]: self.active_channels[d["channel_id"]] = d

    def get_guild_panels(self, guild_id: int) -> List[dict]:
        return list(self.panel_cache.get(guild_id, {}).values())

    async def delete_panel(self, guild_id: int, title: str):
        panel = self.panel_cache.get(guild_id, {}).pop(title, None)
        if not panel: return
        self.active_channels.pop(panel['channel_id'], None)
        async with self.acquire_db() as db:
            await db.execute("DELETE FROM sticky_panels WHERE guild_id = ? AND title = ?", (guild_id, title))
            await db.commit()

    def build_panel_embed(self, data: dict) -> discord.Embed:
        color = parse_color(data.get('embed_color', ''))
        embed = discord.Embed(title=data.get('title'), description=data.get('description'),
                              color=color or discord.Color.default())
        if data.get('image_url'): embed.set_image(url=data['image_url'])
        if data.get('footer'): embed.set_footer(text=data['footer'])
        return embed

    async def sticky_worker(self, channel, panel, delay):
        try:
            await asyncio.sleep(delay)
            await self.update_sticky_message(panel, channel)
        except asyncio.CancelledError:
            pass
        finally:
            self.sticky_tasks.pop(channel.id, None)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.id == self.bot.user.id:
            return

        panel = self.active_channels.get(message.channel.id)
        if not panel:
            return

        if message.author.bot and not panel.get('include_bots', 1):
            return

        current_time = time.time()
        last_time = self.last_message_time.get(message.channel.id, 0)
        self.last_message_time[message.channel.id] = current_time

        if message.channel.id in self.sticky_tasks:
            self.sticky_tasks[message.channel.id].cancel()

        if (current_time - last_time) < 5.0:
            delay = panel.get('conversation_duration', 10)
            self.sticky_tasks[message.channel.id] = asyncio.create_task(
                self.sticky_worker(message.channel, panel, delay)
            )
        else:
            self.sticky_tasks[message.channel.id] = asyncio.create_task(
                self.sticky_worker(message.channel, panel, 0)
            )

    async def update_sticky_message(self, panel, channel):
        try:
            if panel.get('last_message_id'):
                try:
                    await (await channel.fetch_message(panel['last_message_id'])).delete()
                except:
                    pass
            new_msg = await channel.send(embed=self.build_panel_embed(panel))
            async with self.acquire_db() as db:
                await db.execute("UPDATE sticky_panels SET last_message_id = ? WHERE guild_id = ? AND title = ?",
                                 (new_msg.id, panel['guild_id'], panel['title']))
                await db.commit()
            panel['last_message_id'] = new_msg.id
        except Exception as e:
            print(f"Sticky Error: {e}")

    @tasks.loop(seconds=120)
    async def sticky_monitor(self):
        for c_id, panel in list(self.active_channels.items()):
            if c_id in self.sticky_tasks: continue
            channel = self.bot.get_channel(c_id)
            if channel and channel.last_message_id != panel.get('last_message_id'):
                await self.update_sticky_message(panel, channel)

    sticky_group = app_commands.Group(name="sticky", description="Sticky message commands")

    @sticky_group.command(name="message", description="Open the Sticky Message Dashboard")
    @app_commands.check(slash_mod_check)
    async def sticky_dashboard(self, interaction: discord.Interaction):
        await interaction.response.send_message(view=StickyDashboard(interaction.user, self, interaction.guild.id))


async def setup(bot):
    await bot.add_cog(StickyMessages(bot))