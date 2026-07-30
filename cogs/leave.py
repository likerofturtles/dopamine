import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
import asyncio
import aiohttp
import io
import os
from typing import Optional, Dict, Any
from contextlib import asynccontextmanager
from beacon import PrivateLayoutView
from config import LEDB_PATH, LEAVECARD_PATH, BOLDFONT_PATH, MEDIUMFONT_PATH
from beacon import beacon_commands
import re
import pyvips
import ctypes
from pathlib import Path

try:
    fontconfig = ctypes.CDLL("libfontconfig.so.1")
except OSError:
    fontconfig = None


def register_font(font_path: str):
    font_path_str = str(font_path)
    if fontconfig and font_path_str:
        fontconfig.FcConfigAppFontAddFile(None, font_path_str.encode('utf-8'))


async def fetch_image(session: aiohttp.ClientSession, url: str) -> Optional[bytes]:
    try:
        async with session.get(url) as resp:
            if resp.status == 200:
                return await resp.read()
    except:
        return None


class LeaveTextModal(discord.ui.Modal, title="Customise Leave Text"):
    message = discord.ui.TextInput(
        label="Message Content",
        style=discord.TextStyle.paragraph,
        placeholder="Goodbye, {member.display_name}. You will be missed.",
        required=True,
        max_length=2000
    )

    def __init__(self, current_msg: str, callback_func):
        super().__init__()
        self.callback_func = callback_func
        self.message.default = current_msg or "{member.display_name} has left the server"

    async def on_submit(self, interaction: discord.Interaction):
        await self.callback_func(interaction, self.message.value)


class LeaveImageModal(discord.ui.Modal, title="Customise Goodbye Card"):
    def __init__(self, data: dict, callback_func):
        super().__init__()
        self.background_file = discord.ui.FileUpload(
            required=False
        )
        self.line1 = discord.ui.TextInput(
            placeholder="Type here...",
            required=False,
            max_length=40
        )
        self.line2 = discord.ui.TextInput(
            placeholder="Type here...",
            required=False,
            max_length=50
        )
        self.text_color = discord.ui.TextInput(
            placeholder="#FFFFFF",
            required=False,
            max_length=7
        )
        self.callback_func = callback_func
        self.line1.default = data.get("image_line1") or "Goodbye {member.display_name}"
        self.line2.default = data.get("image_line2") or "We hope to see you again!"
        self.text_color.default = data.get("embed_color") or "#FFFFFF"
        self.add_item(discord.ui.Label(text="Upload Background Image", component=self.background_file))
        self.add_item(discord.ui.Label(text="Line 1 Text (Big)", component=self.line1))
        self.add_item(discord.ui.Label(text="Line 2 Text (Small)", component=self.line2))
        self.add_item(discord.ui.Label(text="Text Hex Colour", component=self.text_color))

    async def on_submit(self, interaction: discord.Interaction):
        color_val = self.text_color.value.strip()
        hex_pattern = r'^#?([A-Fa-f0-9]{3}|[A-Fa-f0-9]{6})$'

        if color_val and not re.match(hex_pattern, color_val):
            return await interaction.response.send_message(
                "Invalid Hex Color! Please use a format like `#FFFFFF` or `FFF`.",
                ephemeral=True
            )

        if color_val and not color_val.startswith("#"):
            color_val = f"#{color_val}"

        uploaded_attachment = self.background_file.values[0] if self.background_file.values else None
        await self.callback_func(interaction, uploaded_attachment, self.line1.value, self.line2.value, color_val)


class DestructiveConfirmationView(PrivateLayoutView):
    def __init__(self, user, title_text, body_text):
        super().__init__(user=user, timeout=30)
        self.title_text = title_text
        self.body_text = body_text
        self.value = None
        self.color = None
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container(accent_color=self.color)
        container.add_item(discord.ui.TextDisplay(f"### {self.title_text}"))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(self.body_text))

        is_disabled = self.value is not None
        action_row = discord.ui.ActionRow()
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.gray, disabled=is_disabled)
        confirm = discord.ui.Button(label="Reset to Default", style=discord.ButtonStyle.red, disabled=is_disabled)

        cancel.callback = self.cancel_callback
        confirm.callback = self.confirm_callback

        action_row.add_item(cancel)
        action_row.add_item(confirm)
        container.add_item(discord.ui.Separator())
        container.add_item(action_row)

        self.add_item(container)

    async def update_view(self, interaction: discord.Interaction, title: str, color: discord.Color):
        self.title_text = title
        if not self.body_text.startswith("~~"):
            self.body_text = f"~~{self.body_text}~~"
        self.color = color
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
            self.value = False
            await self.update_view(interaction, "Timed Out", discord.Color(0xdf5046))


class LeaveDashboardView(PrivateLayoutView):
    def __init__(self, cog, guild_id: int, user: discord.Member):
        super().__init__(user=user, timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.data = self.cog.leave_cache.get(guild_id, {})
        self.build_layout()

    async def refresh_state(self):
        self.data = self.cog.leave_cache.get(self.guild_id, {})
        self.build_layout()

    async def update_db(self, **kwargs):
        async with self.cog.acquire_db() as db:
            columns = ", ".join(f"{k} = ?" for k in kwargs.keys())
            values = list(kwargs.values())
            cursor = await db.execute("SELECT 1 FROM leave_settings WHERE guild_id = ?", (self.guild_id,))
            if not await cursor.fetchone():
                await db.execute("INSERT INTO leave_settings (guild_id) VALUES (?)", (self.guild_id,))

            await db.execute(f"UPDATE leave_settings SET {columns} WHERE guild_id = ?", (*values, self.guild_id))
            await db.commit()

        if self.guild_id not in self.cog.leave_cache:
            self.cog.leave_cache[self.guild_id] = {"guild_id": self.guild_id}
        self.cog.leave_cache[self.guild_id].update(kwargs)

        if "local_image_path" in kwargs or "image_url" in kwargs:
            self.cog.image_bytes_cache.pop(self.guild_id, None)

    async def toggle_feature(self, interaction: discord.Interaction):
        is_enabled = self.data.get("is_enabled", 0)
        new_state = 0 if is_enabled else 1

        updates = {"is_enabled": new_state}

        if new_state == 1 and not self.data.get("channel_id"):
            updates["channel_id"] = interaction.channel_id

        await self.update_db(**updates)
        await self.refresh_state()
        await interaction.response.edit_message(view=self)

    async def channel_select_dropdown_callback(self, interaction: discord.Interaction):
        channel_id = int(interaction.data["values"][0])
        await self.update_db(channel_id=channel_id)
        await self.refresh_state()
        await interaction.response.edit_message(view=self)

    async def test_button_callback(self, interaction: discord.Interaction):
        channel_id = self.data.get("channel_id")
        guild = interaction.guild
        channel = guild.get_channel(channel_id) if channel_id else None

        if not channel:
            await interaction.response.send_message("The configured leave channel no longer exists or isn't set.",
                                                    ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        bot_member = guild.me
        content, file = None, None

        if self.data.get("show_text", 1):
            raw_msg = self.data.get("custom_message") or "{member.display_name} has left the server"
            content = f"**TEST:** {raw_msg.format(member=bot_member, server=guild)}"

        if self.data.get("show_image", 1):
            avatar_bytes = None
            async with aiohttp.ClientSession() as session:
                avatar_bytes = await fetch_image(session, bot_member.display_avatar.url)

            loop = asyncio.get_running_loop()
            file = await loop.run_in_executor(
                None,
                self.cog.generate_leave_card,
                bot_member,
                self.data,
                guild,
                avatar_bytes
            )

        try:
            await channel.send(content=content, file=file)
            await interaction.followup.send(f"Test message sent to {channel.mention}!", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send(f"I don't have permission to send messages in {channel.mention}.",
                                            ephemeral=True)

    async def toggle_text(self, interaction: discord.Interaction):
        current = self.data.get("show_text", 1)
        await self.update_db(show_text=0 if current else 1)
        await self.refresh_state()
        await interaction.response.edit_message(view=self)

    async def open_text_modal(self, interaction: discord.Interaction):
        current_msg = self.data.get("custom_message")
        await interaction.response.send_modal(LeaveTextModal(current_msg, self.text_modal_callback))

    async def text_modal_callback(self, interaction: discord.Interaction, value: str):
        await self.update_db(custom_message=value)
        await self.refresh_state()
        await interaction.response.edit_message(view=self)

    async def toggle_image(self, interaction: discord.Interaction):
        current = self.data.get("show_image", 1)
        await self.update_db(show_image=0 if current else 1)
        await self.refresh_state()
        await interaction.response.edit_message(view=self)

    async def open_image_modal(self, interaction: discord.Interaction):
        await interaction.response.send_modal(LeaveImageModal(self.data, self.image_modal_callback))

    async def image_modal_callback(self, interaction: discord.Interaction, attachment: Optional[discord.Attachment],
                                   line1: str, line2: str, color: str):
        await interaction.response.defer(ephemeral=True)

        final_color = color if color.startswith("#") and len(color) == 7 else "#FFFFFF"
        db_updates = {
            "image_line1": line1,
            "image_line2": line2,
            "embed_color": final_color
        }

        if attachment:
            old_path = self.data.get("local_image_path")
            if old_path and os.path.exists(old_path):
                try:
                    os.remove(old_path)
                except Exception as e:
                    self.cog.bot.logger.error(f"Failed to delete old image {old_path}: {e}")

            try:
                file_bytes = await attachment.read()
                img = pyvips.Image.new_from_buffer(file_bytes, "")

                storage_dir = Path("databases/leave_backgrounds")
                storage_dir.mkdir(parents=True, exist_ok=True)

                new_file_path = storage_dir / f"bg_{self.guild_id}.jpg"
                img.write_to_file(str(new_file_path), Q=85)

                db_updates["local_image_path"] = str(new_file_path)
                db_updates["image_url"] = None
            except Exception as e:
                await interaction.followup.send(f"Error processing image compression: {e}", ephemeral=True)
                return

        await self.update_db(**db_updates)
        await self.refresh_state()
        await interaction.edit_original_response(view=self)

    async def reset_button_callback(self, interaction: discord.Interaction):
        view = DestructiveConfirmationView(
            user=interaction.user,
            title_text="Reset Goodbye Settings?",
            body_text="This will delete all custom text, images, and configurations. The feature will remain enabled if it is currently enabled."
        )
        await interaction.response.send_message(view=view)
        await view.wait()

        if view.value:
            old_path = self.data.get("local_image_path")
            if old_path and os.path.exists(old_path):
                try:
                    os.remove(old_path)
                except Exception as e:
                    print(f"Error purging file assets during reset: {e}")

            async with self.cog.acquire_db() as db:
                await db.execute("""
                    UPDATE leave_settings 
                    SET custom_message=NULL, custom_line1=NULL, custom_line2=NULL, 
                        image_url=NULL, local_image_path=NULL, embed_color=NULL, show_text=1, show_image=1 
                    WHERE guild_id=?
                """, (self.guild_id,))
                await db.commit()

            if self.guild_id in self.cog.leave_cache:
                saved_channel = self.cog.leave_cache[self.guild_id].get("channel_id")
                saved_enabled = self.cog.leave_cache[self.guild_id].get("is_enabled")
                self.cog.leave_cache[self.guild_id] = {
                    "guild_id": self.guild_id,
                    "channel_id": saved_channel,
                    "is_enabled": saved_enabled,
                    "show_text": 1,
                    "show_image": 1
                }
            self.cog.image_bytes_cache.pop(self.guild_id, None)
            await self.refresh_state()

    def build_layout(self):
        self.clear_items()

        is_enabled = bool(self.data.get("is_enabled", 0))
        show_text = bool(self.data.get("show_text", 1))
        show_image = bool(self.data.get("show_image", 1))
        channel_id = self.data.get("channel_id")

        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("## Goodbye Feature Dashboard"))

        btn_main = discord.ui.Button(
            label=f"{'Disable Goodbye Feature' if is_enabled else 'Enable'}",
            style=discord.ButtonStyle.secondary if is_enabled else discord.ButtonStyle.primary
        )
        btn_main.callback = self.toggle_feature

        section = discord.ui.Section(
            discord.ui.TextDisplay(
                "Configure all settings related to Dopamine's leave/goodbye feature. Click the adjacent button to enable or disable the feature."),
            accessory=btn_main
        )
        container.add_item(section)

        channel_select = discord.ui.ChannelSelect(
            placeholder="Select goodbye channel...",
            min_values=1,
            max_values=1
        )
        channel_select.callback = self.channel_select_dropdown_callback

        if channel_id:
            channel_select.default_values = [
                discord.SelectDefaultValue(id=channel_id, type=discord.SelectDefaultValueType.channel)
            ]

        if is_enabled:
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay("### Goodbye Channel Location"))
            row = discord.ui.ActionRow()
            row.add_item(channel_select)
            container.add_item(row)
            container.add_item(discord.ui.Separator())
            btn_text_toggle = discord.ui.Button(
                label=f"{'Disable' if show_text else 'Enable'}",
                style=discord.ButtonStyle.secondary if show_text else discord.ButtonStyle.primary
            )
            btn_text_toggle.callback = self.toggle_text

            section = discord.ui.Section(
                discord.ui.TextDisplay("### Text"),
                accessory=btn_text_toggle
            )
            container.add_item(section)

            if show_text:
                btn_text_config = discord.ui.Button(label=f"Customise", style=discord.ButtonStyle.primary)
                btn_text_config.callback = self.open_text_modal

                curr_text = self.data.get("custom_message") or "{member.display_name} has left the server"

                section = discord.ui.Section(
                    discord.ui.TextDisplay(
                        f"The text part of the leave message. Click the customise button to customise the format.\n\n* **Current Format:**\n  * ```{curr_text}```\n* **Available Variables:**\n  * `{{member.mention}}` - Mention the member.\n  * `{{member.display_name}}` - The member's display name.\n  * `{{server.name}}` - The name of the server.\n  * ...and others available in Discord member or server/guild objects"),
                    accessory=btn_text_config
                )
                container.add_item(section)

            container.add_item(discord.ui.Separator())

            btn_img_toggle = discord.ui.Button(
                label=f"{'Disable' if show_image else 'Enable'}",
                style=discord.ButtonStyle.secondary if show_image else discord.ButtonStyle.primary
            )
            btn_img_toggle.callback = self.toggle_image

            section = discord.ui.Section(
                discord.ui.TextDisplay("### Leave Card"),
                accessory=btn_img_toggle
            )
            container.add_item(section)

            if show_image:
                btn_img_config = discord.ui.Button(label="Customise", style=discord.ButtonStyle.primary)
                btn_img_config.callback = self.open_image_modal

                curr_l1 = self.data.get("image_line1") or "Goodbye {member.display_name}"
                curr_l2 = self.data.get("image_line2") or "You will be missed!"
                using_custom_img = "Yes" if self.data.get("local_image_path") else "No"
                curr_color = self.data.get("embed_color") or "#FFFFFF"
                section = discord.ui.Section(
                    discord.ui.TextDisplay(
                        f"The Goodbye Card (image). Use the customise button to upload a custom image, or to edit text.\n\n* **Custom Background:** {using_custom_img}\n* **Current Image Text:**\n  * Line 1: `{curr_l1}`\n  * Line 2: `{curr_l2}`\n* **Text Colour:** {curr_color}\n* **Available Variables:**\n  * `{{member.name}}`, `{{server.name}}`, and others available in Discord member or server/guild objects."),
                    accessory=btn_img_config
                )
                container.add_item(section)

            container.add_item(discord.ui.Separator())

            btn_test = discord.ui.Button(label="Send Test Message", style=discord.ButtonStyle.primary)
            btn_test.callback = self.test_button_callback

            container.add_item(discord.ui.TextDisplay("### Test Message"))

            channel_mention = f"<#{channel_id}>" if channel_id else "`Not Set`"
            container.add_item(discord.ui.Section(discord.ui.TextDisplay(
                f"Click the Send Test Message button to send a test message/preview in the set channel: {channel_mention}"),
                accessory=btn_test))

            container.add_item(discord.ui.Separator())

            container.add_item(discord.ui.TextDisplay("### Reset to Default"))

            btn_reset = discord.ui.Button(label="Reset", style=discord.ButtonStyle.secondary)
            btn_reset.callback = self.reset_button_callback

            container.add_item(discord.ui.Section(
                discord.ui.TextDisplay("Click the Reset button to reset everything to default."),
                accessory=btn_reset
            ))

        self.add_item(container)


class Leaves(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.leave_cache: Dict[int, dict] = {}
        self.image_bytes_cache: Dict[int, bytes] = {}
        self.db_pool: Optional[asyncio.Queue] = None
        register_font(BOLDFONT_PATH)
        register_font(MEDIUMFONT_PATH)

    async def cog_load(self):
        await self.init_pools()
        await self.init_db()
        await self.migrate_old_backgrounds()
        await self.populate_caches()

    async def cog_unload(self):
        if self.db_pool:
            while not self.db_pool.empty():
                conn = await self.db_pool.get()
                await conn.close()
            self.db_pool = None

    async def init_pools(self, pool_size: int = 5):
        if self.db_pool is None:
            self.db_pool = asyncio.Queue(maxsize=pool_size)
            for _ in range(pool_size):
                conn = await aiosqlite.connect(LEDB_PATH, timeout=5)
                await conn.execute("PRAGMA busy_timeout=5000")
                await conn.execute("PRAGMA journal_mode=WAL")
                await conn.execute("PRAGMA synchronous = NORMAL")
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
                             CREATE TABLE IF NOT EXISTS leave_settings
                             (
                                 guild_id INTEGER PRIMARY KEY,
                                 channel_id INTEGER,
                                 is_enabled INTEGER DEFAULT 0,
                                 show_text INTEGER DEFAULT 1,
                                 custom_message TEXT,
                                 custom_line1 TEXT,
                                 custom_line2 TEXT,
                                 show_image INTEGER DEFAULT 1,
                                 image_url TEXT,
                                 local_image_path TEXT,
                                 image_line1 TEXT,
                                 image_line2 TEXT,
                                 embed_color TEXT
                             )
                             ''')
            cursor = await db.execute("PRAGMA table_info(leave_settings)")
            columns = [row[1] for row in await cursor.fetchall()]

            optional_columns = {
                "local_image_path": "TEXT",
                "image_line1": "TEXT",
                "image_line2": "TEXT",
                "embed_color": "TEXT"
            }

            for col_name, col_type in optional_columns.items():
                if col_name not in columns:
                    await db.execute(f"ALTER TABLE leave_settings ADD COLUMN {col_name} {col_type}")

            await db.commit()

    async def migrate_old_backgrounds(self):
        """Scans for legacy URL properties, processes downloads, saves as JPEG, and remaps layout data."""
        storage_dir = Path("databases/leave_backgrounds")
        storage_dir.mkdir(parents=True, exist_ok=True)

        async with self.acquire_db() as db:
            async with db.execute(
                    "SELECT guild_id, image_url FROM leave_settings WHERE image_url IS NOT NULL AND local_image_path IS NULL") as cursor:
                rows = await cursor.fetchall()

            if not rows:
                return

            print(f"[Migration] Migrating {len(rows)} legacy leave background URL profiles...")
            async with aiohttp.ClientSession() as session:
                for guild_id, url in rows:
                    raw_bytes = await fetch_image(session, url)
                    if not raw_bytes:
                        continue

                    try:
                        img = pyvips.Image.new_from_buffer(raw_bytes, "")
                        local_path = storage_dir / f"bg_{guild_id}.jpg"
                        img.write_to_file(str(local_path), Q=85)

                        await db.execute(
                            "UPDATE leave_settings SET local_image_path = ?, image_url = NULL WHERE guild_id = ?",
                            (str(local_path), guild_id)
                        )
                        await db.commit()
                    except Exception as e:
                        print(
                            f"[Migration Failure] Couldn't compress/migrate legacy asset configurations for server {guild_id}: {e}")

    async def populate_caches(self):
        self.leave_cache.clear()
        async with self.acquire_db() as db:
            async with db.execute("SELECT * FROM leave_settings") as cursor:
                rows = await cursor.fetchall()
                columns = [column[0] for column in cursor.description]
                for row in rows:
                    data = dict(zip(columns, row))
                    self.leave_cache[data["guild_id"]] = data

    def get_background_image(self, guild_id: int, local_image_path: Optional[str]) -> pyvips.Image:
        if guild_id in self.image_bytes_cache:
            return pyvips.Image.new_from_buffer(self.image_bytes_cache[guild_id], "")

        try:
            if local_image_path and os.path.exists(local_image_path):
                img = pyvips.Image.new_from_file(local_image_path)
            else:
                img = pyvips.Image.new_from_file(LEAVECARD_PATH)

            img = img.thumbnail_image(686, height=291, crop="centre")
            self.image_bytes_cache[guild_id] = img.write_to_buffer(".png")
            return img
        except Exception as e:
            print(f"Error processing Background: {e}")
            return pyvips.Image.new_from_file(LEAVECARD_PATH).thumbnail_image(686, height=291, crop="centre")

    def generate_leave_card(self, member: discord.User, data: dict, guild: discord.Guild,
                            avatar_bytes: Optional[bytes]) -> discord.File:
        guild_id = guild.id
        local_path = data.get("local_image_path")

        line1_text = (data.get("image_line1") or "Goodbye {member.display_name}").format(
            member=member, server=guild
        )
        line2_text = (data.get("image_line2") or "You will be missed!").format(
            member=member, server=guild
        )
        hex_color = data.get("embed_color") or "#FFFFFF"
        rgb = [int(hex_color.lstrip('#')[i:i + 2], 16) for i in (0, 2, 4)]

        base_img = self.get_background_image(guild_id, local_path)
        if not base_img.hasalpha():
            base_img = base_img.addalpha()

        avatar_size = 100
        avatar_radius = avatar_size // 2

        card_width = 686
        x_pos = (card_width - avatar_size) // 2
        y_pos = 102 - avatar_radius

        if avatar_bytes:
            if not local_path:
                ring_offset = 5
                ring_thickness = 4
                outer_radius = avatar_radius + ring_offset
                inner_radius = outer_radius - ring_thickness

                ring_box_size = outer_radius * 2
                ring_center = outer_radius

                ring_mask = pyvips.Image.black(ring_box_size, ring_box_size)
                ring_mask = ring_mask.draw_circle(255, ring_center, ring_center, outer_radius - 1, fill=True)

                inner_mask = pyvips.Image.black(ring_box_size, ring_box_size)
                inner_mask = inner_mask.draw_circle(255, ring_center, ring_center, inner_radius - 1, fill=True)
                ring_alpha = ring_mask - inner_mask

                ring_color_rgba = [127, 37, 201, 255]
                ring_colored = pyvips.Image.new_from_image(ring_mask, ring_color_rgba[:3]).copy(interpretation="srgb")
                ring_colored = ring_colored.bandjoin((ring_alpha / 255) * ring_color_rgba[3])

                ring_x = (card_width - ring_box_size) // 2
                ring_y = 102 - ring_center
                base_img = base_img.composite2(ring_colored, 'over', x=ring_x, y=ring_y)

            avatar = pyvips.Image.new_from_buffer(avatar_bytes, "").thumbnail_image(
                avatar_size, height=avatar_size, crop="centre"
            )
            if not avatar.hasalpha():
                avatar = avatar.addalpha()

            mask = pyvips.Image.black(avatar_size, avatar_size)
            mask = mask.draw_circle(255, avatar_radius, avatar_radius, avatar_radius - 1, fill=True)
            mask = mask.gaussblur(0.7)

            original_alpha = avatar.extract_band(avatar.bands - 1)
            final_alpha = (original_alpha / 255) * (mask / 255) * 255
            avatar = avatar.extract_band(0, n=3).bandjoin(final_alpha)

            base_img = base_img.composite2(avatar, 'over', x=x_pos, y=y_pos)

        def draw_centered_text(base, text, size, y_pos_text, font_name, weight, color_rgb):
            max_width = 638
            min_size = 10

            while size > min_size:
                mask = pyvips.Image.text(
                    f'<span font_family="{font_name}" weight="{weight}" size="{size * 1024}">{text}</span>'
                )
                if mask.width <= max_width:
                    break
                size -= 2

            mask = pyvips.Image.text(
                f'<span font_family="{font_name}" weight="{weight}" size="{size * 1024}">{text}</span>'
            )

            text_x_pos = (card_width - mask.width) // 2
            white_text = mask.new_from_image(color_rgb).copy(interpretation="srgb")
            text_img = white_text.bandjoin(mask)
            return base.composite2(text_img, 'over', x=text_x_pos, y=y_pos_text)

        base_img = draw_centered_text(base_img, line1_text, 24, 178, font_name="gg sans", weight="Bold", color_rgb=rgb)
        base_img = draw_centered_text(base_img, line2_text, 22, 223, font_name="gg sans Medium", weight="Normal",
                                      color_rgb=rgb)

        png_buffer = base_img.write_to_buffer(".png")
        return discord.File(io.BytesIO(png_buffer), filename="leave.png")

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent):
        guild_id = payload.guild_id
        data = self.leave_cache.get(guild_id)

        if not data or not data.get("is_enabled") or not data.get("channel_id"):
            return

        guild = self.bot.get_guild(guild_id) or await self.bot.fetch_guild(guild_id)
        if not guild:
            return

        channel = guild.get_channel(data["channel_id"]) or await self.bot.fetch_channel(data["channel_id"])
        if not channel:
            return

        user = payload.user

        try:
            msg_content = None
            msg_file = None

            if data.get("show_text", 1):
                raw_msg = data.get("custom_message") or "{member.display_name} has left the server"
                msg_content = raw_msg.format(member=user, server=guild)

            if data.get("show_image", 1):
                avatar_bytes = None
                async with aiohttp.ClientSession() as session:
                    avatar_bytes = await fetch_image(session, user.display_avatar.url)

                loop = asyncio.get_running_loop()
                msg_file = await loop.run_in_executor(
                    None,
                    self.generate_leave_card,
                    user,
                    data,
                    guild,
                    avatar_bytes
                )

            if msg_content or msg_file:
                await channel.send(content=msg_content, file=msg_file)

        except Exception as e:
            from utils.discord_health import is_access_error, report_access_failure
            if is_access_error(e):
                await report_access_failure(self.bot, guild.id, "leave")

    @beacon_commands.command(name="goodbye", description="Open the leave/goodbye feature dashboard.",
                             permissions_preset="automation")
    async def leave_dashboard(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            view=LeaveDashboardView(self, interaction.guild.id, interaction.user)
        )

    def data_features(self) -> list:
        from utils.data_protocol import DataFeatureMeta
        return [DataFeatureMeta(feature_id="leave", name="Goodbye", guild_export=True, guild_delete=True)]

    async def data_export_user(self, user_id: int, *, guild_ids: list[int] | None):
        from utils.data_protocol import DataExportChunk
        return DataExportChunk(feature_id="leave")

    async def data_export_guild(self, guild_id: int):
        from utils.data_handlers import export_table
        from utils.data_protocol import DataExportChunk
        chunk = DataExportChunk(feature_id="leave")
        async with self.acquire_db() as db:
            rows = await export_table(db, "SELECT * FROM leave_settings WHERE guild_id = ?", (guild_id,))
        if rows:
            chunk.guild_data[guild_id] = {"settings": rows[0]}
        return chunk

    async def data_delete_user(self, user_id: int, *, guild_ids: list[int] | None, feature_id: str | None):
        from utils.data_protocol import DataDeleteResult
        return DataDeleteResult(feature_id="leave")

    async def data_delete_guild(self, guild_id: int, feature_id: str | None):
        import os
        from pathlib import Path
        from utils.data_protocol import DataDeleteResult
        async with self.acquire_db() as db:
            cur = await db.execute("DELETE FROM leave_settings WHERE guild_id = ?", (guild_id,))
            await db.commit()
        self.leave_cache.pop(guild_id, None)
        bg = Path("databases/leave_backgrounds") / f"{guild_id}.jpg"
        if bg.is_file():
            os.remove(bg)
        return DataDeleteResult(feature_id="leave", deleted=True, rows_affected=cur.rowcount)

    async def data_monitor_guild(self, guild: discord.Guild):
        from utils.data_protocol import DataMonitorResult
        result = DataMonitorResult(feature_id="leave")
        data = self.leave_cache.get(guild.id)
        if not data or not data.get("is_enabled"):
            return result
        channel_id = data.get("channel_id")
        channel = guild.get_channel(channel_id) if channel_id else None
        if not channel or not channel.permissions_for(guild.me).send_messages:
            async with self.acquire_db() as db:
                await db.execute(
                    "UPDATE leave_settings SET is_enabled = 0 WHERE guild_id = ?", (guild.id,)
                )
                await db.commit()
            if guild.id in self.leave_cache:
                self.leave_cache[guild.id]["is_enabled"] = 0
            result.actions.append("disabled_leave")
        return result


async def setup(bot):
    await bot.add_cog(Leaves(bot))