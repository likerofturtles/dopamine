import asyncio
import io
import random
from datetime import datetime, timedelta, time

import discord
from beacon import beacon_commands
from discord import app_commands, Interaction
from discord.ext import commands, tasks

from utils.data_protocol import DataDeleteResult, DataExportChunk, DataFeatureMeta, DataMonitorResult
from utils.discord_health import is_access_error, report_access_failure


class DeleteImageModal(discord.ui.Modal):
    def __init__(self, cog: "DailyCats", image_ids: list[int], group_idx: int):
        super().__init__(title=f"Delete Image from Group {group_idx + 1}")
        self.cog = cog
        self.image_ids = image_ids
        max_idx = len(image_ids)

        self.index_input = discord.ui.TextInput(
            label=f"Image Index (1 - {max_idx})",
            placeholder=f"Enter a number between 1 and {max_idx}...",
            min_length=1,
            max_length=2,
            required=True
        )
        self.add_item(self.index_input)

    async def on_submit(self, interaction: Interaction):
        try:
            val = int(self.index_input.value.strip())
            if not (1 <= val <= len(self.image_ids)):
                return await interaction.response.send_message(
                    f"Invalid index. Please enter a number between 1 and {len(self.image_ids)}.", ephemeral=True
                )
        except ValueError:
            return await interaction.response.send_message("Please enter a valid number.", ephemeral=True)

        target_id = self.image_ids[val - 1]
        await self.cog.bot.db.execute("DELETE FROM cat_images WHERE id = ?", (target_id,))

        await interaction.response.send_message(
            f"Successfully deleted image #{val} (Database ID: `{target_id}`)!", ephemeral=True
        )


class AddImageModal(discord.ui.Modal, title="Add Cat Image"):
    def __init__(self, cog: "DailyCats"):
        super().__init__()
        self.cog = cog
        self.file_upload = discord.ui.FileUpload(
            required=True,
            max_values=10
        )
        self.add_item(discord.ui.Label(text="Select Image", description="Upload a PNG, JPEG, or GIF image to add to the daily cat database.", component=self.file_upload))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        uploaded_files = self.file_upload.values
        if not uploaded_files:
            return await interaction.followup.send("No files uploaded.", ephemeral=True)

        valid_types = {'image/png', 'image/jpeg', 'image/gif'}
        saved_count = 0
        failed_files = []

        for uploaded_file in uploaded_files:
            if uploaded_file.content_type not in valid_types:
                failed_files.append(f"`{uploaded_file.filename}` (Invalid type: {uploaded_file.content_type})")
                continue

            try:
                image_bytes = await uploaded_file.read()
                await self.cog.bot.db.execute("INSERT INTO cat_images (image_data) VALUES (?)", (image_bytes,))
                saved_count += 1
            except Exception as e:
                failed_files.append(f"`{uploaded_file.filename}` ({e})")

        response_msg = []
        if saved_count > 0:
            response_msg.append(f"Successfully added **{saved_count}** image(s) to the database!")
        if failed_files:
            response_msg.append("Failed to upload the following files:\n" + "\n".join(f"- {f}" for f in failed_files))

        await interaction.followup.send("\n\n".join(response_msg), ephemeral=True)


class CatDashboardView(discord.ui.LayoutView):
    def __init__(self, cog: "DailyCats", images: list[tuple[int, bytes]], page: int = 0):
        super().__init__(timeout=300)
        self.cog = cog
        self.images = images
        self.page = page
        self.items_per_group = 5
        self.groups_per_page = 2
        self.items_per_page = self.items_per_group * self.groups_per_page

        self.max_pages = max(1, (len(self.images) + self.items_per_page - 1) // self.items_per_page)

    async def build(self) -> list[discord.File]:
        files = []

        header_container = discord.ui.Container()
        header_container.add_item(
            discord.ui.TextDisplay(
                f"# 🐱 Cat Image Dashboard\n"
                f"Total Images: **{len(self.images)}** | Page **{self.page + 1}** of **{self.max_pages}**"
            )
        )
        self.add_item(header_container)

        start_idx = self.page * self.items_per_page
        page_images = self.images[start_idx:start_idx + self.items_per_page]

        if not page_images:
            empty_container = discord.ui.Container()
            empty_container.add_item(discord.ui.TextDisplay("*No cat images found in the database.*"))
            self.add_item(empty_container)
        else:
            for group_idx in range(0, len(page_images), self.items_per_group):
                group_chunk = page_images[group_idx:group_idx + self.items_per_group]
                group_num = (group_idx // self.items_per_group) + 1

                group_container = discord.ui.Container()
                group_container.add_item(
                    discord.ui.TextDisplay(f"### Group {group_num} ({len(group_chunk)} Images)")
                )

                gallery = discord.ui.MediaGallery()
                group_ids = []

                for idx, (img_id, img_bytes) in enumerate(group_chunk):
                    filename = f"p{self.page}_g{group_num}_i{idx}_{img_id}.png"
                    file = discord.File(io.BytesIO(img_bytes), filename=filename)
                    files.append(file)
                    group_ids.append(img_id)

                    gallery.add_item(media=f"attachment://{filename}")

                group_container.add_item(gallery)

                del_row = discord.ui.ActionRow()
                del_btn = discord.ui.Button(
                    label=f"Delete Image from Group {group_num}",
                    style=discord.ButtonStyle.danger,
                    custom_id=f"cd_del_{self.page}_{group_num}"
                )

                async def make_del_callback(ids=group_ids, g_num=group_num):
                    async def callback(interaction: Interaction):
                        await interaction.response.send_modal(DeleteImageModal(self.cog, ids, g_num - 1))
                    return callback

                del_btn.callback = await make_del_callback()
                del_row.add_item(del_btn)
                group_container.add_item(del_row)

                self.add_item(group_container)

        nav_container = discord.ui.Container()
        nav_row = discord.ui.ActionRow()

        prev_btn = discord.ui.Button(
            label="◀ Previous",
            style=discord.ButtonStyle.secondary,
            disabled=(self.page == 0)
        )
        prev_btn.callback = self.prev_page
        nav_row.add_item(prev_btn)

        next_btn = discord.ui.Button(
            label="Next ▶",
            style=discord.ButtonStyle.secondary,
            disabled=(self.page >= self.max_pages - 1)
        )
        next_btn.callback = self.next_page
        nav_row.add_item(next_btn)

        add_btn = discord.ui.Button(
            label="➕ Add Image",
            style=discord.ButtonStyle.success,
            custom_id="cd_add_img"
        )
        add_btn.callback = self.add_image_click
        nav_row.add_item(add_btn)

        nav_container.add_item(nav_row)
        self.add_item(nav_container)

        return files

    async def prev_page(self, interaction: Interaction):
        if self.page > 0:
            self.page -= 1
            await self.refresh_dashboard(interaction)

    async def next_page(self, interaction: Interaction):
        if self.page < self.max_pages - 1:
            self.page += 1
            await self.refresh_dashboard(interaction)

    async def add_image_click(self, interaction: Interaction):
        await interaction.response.send_modal(AddImageModal(self.cog))

    async def refresh_dashboard(self, interaction: Interaction):
        await interaction.response.defer()
        rows = await self.cog.bot.db.execute("SELECT id, image_data FROM cat_images ORDER BY id ASC")
        self.images = [(r["id"], r["image_data"]) for r in rows]

        self.max_pages = max(1, (len(self.images) + self.items_per_page - 1) // self.items_per_page)
        self.page = min(self.page, self.max_pages - 1)

        new_view = CatDashboardView(self.cog, self.images, page=self.page)
        files = await new_view.build()

        await interaction.edit_original_response(attachments=files, view=new_view)


class DailyCats(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        self.active_cat_channels = set()
        self.next_send_time = None

        self.init_data.start()

    def cog_unload(self):
        self.init_data.cancel()
        self.daily_task.cancel()

        self.active_cat_channels.clear()
        self.active_cat_channels = None
        self.next_send_time = None

    @tasks.loop(count=1)
    async def init_data(self):
        await self.bot.db.wait_ready()

        rows = await self.bot.db.execute("SELECT channel_id FROM cat_channels")
        self.active_cat_channels = {row["channel_id"] for row in rows}

        rows = await self.bot.db.execute("SELECT value FROM daily_settings WHERE key = 'next_send_time'")
        if rows:
            self.next_send_time = datetime.fromisoformat(rows[0]["value"])
        else:
            now = datetime.now()
            self.next_send_time = datetime.combine(now.date() + timedelta(days=1), time(0, 0))
            await self.save_next_time()

        self.daily_task.start()

    async def save_next_time(self):
        await self.bot.db.execute(
            "INSERT OR REPLACE INTO daily_settings (key, value) VALUES (?, ?)",
            ('next_send_time', self.next_send_time.isoformat())
        )

    @beacon_commands.command(name="cd", description=".", permissions_preset="bot_owner")
    async def cd(self, interaction: Interaction):
        await interaction.response.defer(ephemeral=True)
        rows = await self.bot.db.execute("SELECT id, image_data FROM cat_images ORDER BY id ASC")
        images = [(r["id"], r["image_data"]) for r in rows]

        view = CatDashboardView(self, images, page=0)
        files = await view.build()

        await interaction.edit_original_response(
            attachments=files,
            view=view,
        )

    @commands.command(name="catadd", hidden=True)
    @commands.is_owner()
    async def catadd(self, ctx: commands.Context):
        if not ctx.message.attachments:
            return await ctx.send("Please attach at least one image.")

        valid_types = ['image/png', 'image/jpeg', 'image/gif']
        images_added = 0

        for attachment in ctx.message.attachments:
            if attachment.content_type not in valid_types:
                await ctx.send(f"Skipping {attachment.filename}: Not a valid image type (PNG/JPEG/GIF).",
                               delete_after=10)
                continue

            try:
                image_bytes = await attachment.read()
                await self.bot.db.execute("INSERT INTO cat_images (image_data) VALUES (?)", (image_bytes,))
                images_added += 1
            except Exception as e:
                await ctx.send(f"Failed to add {attachment.filename}: {e}", delete_after=10)

        await ctx.send(f"Successfully added {images_added} cat pics to the database!", delete_after=10)
        await asyncio.sleep(10)
        await ctx.message.delete()

    @tasks.loop(seconds=30)
    async def daily_task(self):
        await self.bot.db.wait_ready()
        if not self.next_send_time or not self.active_cat_channels:
            return

        now = datetime.now()
        if now >= self.next_send_time:
            image_blob = None
            rows = await self.bot.db.execute("SELECT id FROM cat_images")
            ids = [row["id"] for row in rows]

            if ids:
                random_id = random.choice(ids)
                row = await self.bot.db.execute("SELECT image_data FROM cat_images WHERE id = ?", (random_id,))
                if row:
                    image_blob = row[0]["image_data"]

            async def send_to_channel(channel_id):
                guild_id = None
                ch = self.bot.get_channel(channel_id)
                if isinstance(ch, discord.abc.GuildChannel):
                    guild_id = ch.guild.id
                elif ch is None:
                    try:
                        ch = await self.bot.fetch_channel(channel_id)
                        if isinstance(ch, discord.abc.GuildChannel):
                            guild_id = ch.guild.id
                    except Exception as e:
                        if guild_id is None:
                            for g in self.bot.guilds:
                                if g.get_channel(channel_id):
                                    guild_id = g.id
                                    ch = g.get_channel(channel_id)
                                    break
                        if guild_id and is_access_error(e):
                            await report_access_failure(
                                self.bot, guild_id, "daily", f"channel:{channel_id}"
                            )
                        return

                if not ch or guild_id is None:
                    return

                if channel_id in self.active_cat_channels and image_blob:
                    try:
                        file = discord.File(io.BytesIO(image_blob), filename="daily_cat.png")
                        await ch.send(content="Today's Cat Pic:", file=file)
                        await asyncio.sleep(0.25)
                    except Exception as e:
                        if is_access_error(e):
                            await self.bot.db.execute(
                                "DELETE FROM cat_channels WHERE channel_id = ?", (channel_id,)
                            )
                            self.active_cat_channels.discard(channel_id)
                            await report_access_failure(
                                self.bot, guild_id, "daily", f"channel:{channel_id}"
                            )

            await asyncio.gather(*(send_to_channel(cid) for cid in list(self.active_cat_channels)))

            self.next_send_time = self.next_send_time + timedelta(hours=23)
            await self.save_next_time()

    daily = app_commands.Group(name="daily", description="Daily automated messages.")

    cat_group = beacon_commands.Group(name="cat", description="Daily cat image commands", parent=daily, permissions_preset="automation")

    @cat_group.command(name="start", description="Start daily cat pics in a channel.")
    @app_commands.describe(
        channel="The channel where you want the daily cat image to be posted (defaults to current channel).")
    async def daily_cat_start(self, interaction: Interaction, channel: discord.TextChannel = None):
        channel_id = (channel.id if channel else interaction.channel_id)

        if channel_id in self.active_cat_channels:
            return await interaction.response.send_message("Daily cat pics are already active here!", ephemeral=True)

        await self.bot.db.execute("INSERT INTO cat_channels (channel_id) VALUES (?)", (channel_id,))
        self.active_cat_channels.add(channel_id)

        unix_timestamp = int(self.next_send_time.timestamp())

        await interaction.response.send_message(
            f"Daily cat pictures started! Next cat pic at: <t:{unix_timestamp}:F> (<t:{unix_timestamp}:R>)"
        )

    @cat_group.command(name="stop", description="Stop daily cat pics in a channel.")
    @app_commands.describe(
        channel="The channel where you want the daily cat image to be stopped (defaults to current channel).")
    async def daily_cat_stop(self, interaction: Interaction, channel: discord.TextChannel = None):
        channel_id = channel.id if channel else interaction.channel_id
        if channel_id not in self.active_cat_channels:
            return await interaction.response.send_message("Feature isn't active in this channel.", ephemeral=True)

        await self.bot.db.execute("DELETE FROM cat_channels WHERE channel_id = ?", (channel_id,))
        self.active_cat_channels.remove(channel_id)

        await interaction.response.send_message(content="Daily cat pictures stopped.")

    @commands.command(name="del", hidden=True)
    @commands.is_owner()
    async def catwipe(self, ctx: commands.Context):
        try:
            count_rows = await self.bot.db.execute("SELECT COUNT(*) AS cnt FROM cat_images")
            count = count_rows[0]["cnt"] if count_rows else 0

            if count == 0:
                return await ctx.send("The cat database is already empty.")

            await self.bot.db.execute("DELETE FROM cat_images")
            await self.bot.db.execute("DELETE FROM sqlite_sequence WHERE name='cat_images'")

            await ctx.send(f"Successfully wiped **{count}** images from the database.")

        except Exception as e:
            await ctx.send(f"An error occurred while wiping the database: {e}")

    def data_features(self) -> list[DataFeatureMeta]:
        return [DataFeatureMeta(
            feature_id="daily",
            name="Daily Cats",
            guild_export=True,
            guild_delete=True,
        )]

    async def data_export_user(self, user_id: int, *, guild_ids: list[int] | None) -> DataExportChunk:
        return DataExportChunk(feature_id="daily")

    async def _guild_cat_channels(self, guild: discord.Guild) -> list[int]:
        channels = []
        for channel_id in list(self.active_cat_channels or []):
            channel = guild.get_channel(channel_id)
            if channel is not None and getattr(channel, "guild", None) and channel.guild.id == guild.id:
                channels.append(channel_id)
        return channels

    async def data_export_guild(self, guild_id: int) -> DataExportChunk:
        chunk = DataExportChunk(feature_id="daily")
        guild = self.bot.get_guild(guild_id)
        cat_channels = await self._guild_cat_channels(guild) if guild else []
        count_rows = await self.bot.db.execute("SELECT COUNT(*) AS cnt FROM cat_images")
        image_count = count_rows[0]["cnt"] if count_rows else 0
        chunk.guild_data[guild_id] = {
            "cat_channels": cat_channels,
            "cat_images_metadata": {"count": image_count},
        }
        return chunk

    async def data_delete_user(self, user_id: int, *, guild_ids: list[int] | None, feature_id: str | None) -> DataDeleteResult:
        return DataDeleteResult(feature_id="daily")

    async def data_delete_guild(self, guild_id: int, feature_id: str | None) -> DataDeleteResult:
        if feature_id and feature_id != "daily":
            return DataDeleteResult(feature_id="daily")
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return DataDeleteResult(feature_id="daily")
        channel_ids = await self._guild_cat_channels(guild)
        if not channel_ids:
            return DataDeleteResult(feature_id="daily")
        placeholders = ",".join("?" * len(channel_ids))
        count_rows = await self.bot.db.execute(
            f"SELECT COUNT(*) AS cnt FROM cat_channels WHERE channel_id IN ({placeholders})", channel_ids)
        rows_affected = count_rows[0]["cnt"] if count_rows else 0
        await self.bot.db.execute(
            f"DELETE FROM cat_channels WHERE channel_id IN ({placeholders})", channel_ids)
        for cid in channel_ids:
            self.active_cat_channels.discard(cid)
        return DataDeleteResult(feature_id="daily", deleted=True, rows_affected=rows_affected)

    async def _channel_sendable(self, guild: discord.Guild, channel_id: int) -> bool:
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return False
        if not isinstance(channel, discord.abc.GuildChannel) or channel.guild.id != guild.id:
            return False
        perms = channel.permissions_for(guild.me)
        return perms.view_channel and perms.send_messages

    async def data_monitor_guild(self, guild: discord.Guild) -> DataMonitorResult:
        result = DataMonitorResult(feature_id="daily")
        for channel_id in await self._guild_cat_channels(guild):
            if not await self._channel_sendable(guild, channel_id):
                await self.bot.db.execute("DELETE FROM cat_channels WHERE channel_id = ?", (channel_id,))
                self.active_cat_channels.discard(channel_id)
                result.actions.append(f"removed_cat_channel:{channel_id}")
        return result


async def setup(bot):
    await bot.add_cog(DailyCats(bot))
