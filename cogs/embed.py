import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Optional, List, Dict, Any

import aiosqlite
import discord
from beacon import PrivateLayoutView, PrivateView, beacon_commands
from discord.ext import commands

from config import EDB_PATH
from utils.data_handlers import export_table
from utils.data_protocol import DataDeleteResult, DataExportChunk, DataFeatureMeta, DataMonitorResult
from utils.time import get_now_plus_seconds_unix


@dataclass
class EmbedDraft:
    guild_id: int
    content: str = ""
    title: str = ""
    description: str = ""
    color: str = "0x944ae8"
    url: Optional[str] = None
    footer_text: str = ""
    footer_icon_url: str = ""
    author_name: str = ""
    author_icon_url: str = ""
    thumbnail_url: str = ""
    image_url: str = ""
    timestamp_enabled: bool = False


class Embeds(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db_pool: Optional[asyncio.Queue[aiosqlite.Connection]] = None

    async def cog_load(self):
        await self.init_pools(pool_size=3)
        await self.init_db()

    async def cog_unload(self):
        if self.db_pool is not None:
            while not self.db_pool.empty():
                try:
                    conn = self.db_pool.get_nowait()
                    await conn.close()
                except (asyncio.QueueEmpty, Exception):
                    break

    async def init_pools(self, pool_size: int = 3):
        if self.db_pool is None:
            self.db_pool = asyncio.Queue(maxsize=pool_size)
            for _ in range(pool_size):
                conn = await aiosqlite.connect(
                    EDB_PATH,
                    timeout=5,
                )
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
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS embeds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER,
                    name TEXT,
                    content TEXT,
                    title TEXT,
                    description TEXT,
                    color TEXT,
                    url TEXT,
                    footer_text TEXT,
                    footer_icon_url TEXT,
                    author_name TEXT,
                    author_icon_url TEXT,
                    thumbnail_url TEXT,
                    image_url TEXT,
                    timestamp_enabled INTEGER DEFAULT 0,
                    created_by INTEGER,
                    created_at INTEGER
                )
                """
            )
            await db.commit()

    def _parse_color(self, color_str: Optional[str]) -> discord.Color:
        if not color_str:
            return discord.Color(0x944ae8)

        s = color_str.strip()
        if s.startswith("#"):
            s = s[1:]

        try:
            value = int(s, 16)
            return discord.Color(value)
        except ValueError:
            pass

        try:
            return getattr(discord.Color, s.lower())()
        except AttributeError:
            return discord.Color(0x944ae8)

    def build_embed_from_draft(self, draft: EmbedDraft) -> discord.Embed:
        color = self._parse_color(draft.color)
        embed = discord.Embed(
            title=draft.title or None,
            description=draft.description or "Add a description",
            color=color,
        )
        if draft.url:
            embed.url = draft.url

        if draft.thumbnail_url:
            embed.set_thumbnail(url=draft.thumbnail_url)
        if draft.image_url:
            embed.set_image(url=draft.image_url)

        if draft.footer_text or draft.footer_icon_url:
            embed.set_footer(
                text=draft.footer_text or discord.Embed.Empty,
                icon_url=draft.footer_icon_url or None,
            )

        if draft.author_name or draft.author_icon_url:
            embed.set_author(
                name=draft.author_name or discord.Embed.Empty,
                icon_url=draft.author_icon_url or None,
            )

        if draft.timestamp_enabled:
            embed.timestamp = discord.utils.utcnow()

        return embed

    def build_draft_from_row(self, row: Dict[str, Any]) -> EmbedDraft:
        return EmbedDraft(
            guild_id=row["guild_id"],
            content=row.get("content") or "",
            title=row.get("title") or "",
            description=row.get("description") or "",
            color=row.get("color") or "0x944ae8",
            url=row.get("url"),
            footer_text=row.get("footer_text") or "",
            footer_icon_url=row.get("footer_icon_url") or "",
            author_name=row.get("author_name") or "",
            author_icon_url=row.get("author_icon_url") or "",
            thumbnail_url=row.get("thumbnail_url") or "",
            image_url=row.get("image_url") or "",
            timestamp_enabled=bool(row.get("timestamp_enabled", 0)),
        )

    def build_embed_from_row(self, row: Dict[str, Any]) -> discord.Embed:
        draft = self.build_draft_from_row(row)
        return self.build_embed_from_draft(draft)

    async def save_embed(
        self,
        guild_id: int,
        user_id: int,
        draft: EmbedDraft,
        existing_id: Optional[int] = None,
    ) -> int:
        name = draft.title or (draft.description[:20] if draft.description else "Untitled Embed")
        now_ts = int(discord.utils.utcnow().timestamp())

        async with self.acquire_db() as db:
            if existing_id is None:
                cursor = await db.execute(
                    """
                    INSERT INTO embeds (
                        guild_id, name, content, title, description, color, url,
                        footer_text, footer_icon_url, author_name, author_icon_url,
                        thumbnail_url, image_url, timestamp_enabled, created_by, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        guild_id,
                        name,
                        draft.content,
                        draft.title,
                        draft.description,
                        draft.color,
                        draft.url,
                        draft.footer_text,
                        draft.footer_icon_url,
                        draft.author_name,
                        draft.author_icon_url,
                        draft.thumbnail_url,
                        draft.image_url,
                        int(draft.timestamp_enabled),
                        user_id,
                        now_ts,
                    ),
                )
                await db.commit()
                return cursor.lastrowid
            else:
                await db.execute(
                    """
                    UPDATE embeds
                    SET name = ?, content = ?, title = ?, description = ?, color = ?, url = ?,
                        footer_text = ?, footer_icon_url = ?, author_name = ?, author_icon_url = ?,
                        thumbnail_url = ?, image_url = ?, timestamp_enabled = ?
                    WHERE id = ? AND guild_id = ?
                    """,
                    (
                        name,
                        draft.content,
                        draft.title,
                        draft.description,
                        draft.color,
                        draft.url,
                        draft.footer_text,
                        draft.footer_icon_url,
                        draft.author_name,
                        draft.author_icon_url,
                        draft.thumbnail_url,
                        draft.image_url,
                        int(draft.timestamp_enabled),
                        existing_id,
                        guild_id,
                    ),
                )
                await db.commit()
                return existing_id

    async def fetch_embeds_for_guild(self, guild_id: int) -> List[Dict[str, Any]]:
        async with self.acquire_db() as db:
            async with db.execute(
                """
                SELECT id, guild_id, name, content, title, description, color, url,
                       footer_text, footer_icon_url, author_name, author_icon_url,
                       thumbnail_url, image_url, timestamp_enabled, created_by, created_at
                FROM embeds
                WHERE guild_id = ?
                ORDER BY id DESC
                """,
                (guild_id,),
            ) as cursor:
                rows = await cursor.fetchall()
                columns = [col[0] for col in cursor.description]
        return [dict(zip(columns, r)) for r in rows]

    async def delete_embed(self, guild_id: int, embed_id: int) -> None:
        async with self.acquire_db() as db:
            await db.execute(
                "DELETE FROM embeds WHERE id = ? AND guild_id = ?",
                (embed_id, guild_id),
            )
            await db.commit()

    def data_features(self) -> list[DataFeatureMeta]:
        return [DataFeatureMeta(
            feature_id="embeds",
            name="Embeds",
            guild_export=True,
            guild_delete=True,
        )]

    async def data_export_user(self, user_id: int, *, guild_ids: list[int] | None) -> DataExportChunk:
        return DataExportChunk(feature_id="embeds")

    async def data_export_guild(self, guild_id: int) -> DataExportChunk:
        chunk = DataExportChunk(feature_id="embeds")
        async with self.acquire_db() as db:
            embeds = await export_table(db, "SELECT * FROM embeds WHERE guild_id = ?", (guild_id,))
        chunk.guild_data[guild_id] = {"embeds": embeds}
        return chunk

    async def data_delete_user(self, user_id: int, *, guild_ids: list[int] | None, feature_id: str | None) -> DataDeleteResult:
        return DataDeleteResult(feature_id="embeds")

    async def data_delete_guild(self, guild_id: int, feature_id: str | None) -> DataDeleteResult:
        if feature_id and feature_id != "embeds":
            return DataDeleteResult(feature_id="embeds")
        async with self.acquire_db() as db:
            cur = await db.execute("DELETE FROM embeds WHERE guild_id = ?", (guild_id,))
            await db.commit()
        return DataDeleteResult(feature_id="embeds", deleted=True, rows_affected=cur.rowcount)

    async def data_monitor_guild(self, guild: discord.Guild) -> DataMonitorResult:
        return DataMonitorResult(feature_id="embeds")

    @beacon_commands.command(name="embed", description="Open the Embed Dashboard.", permissions_preset="support")
    async def embed_dashboard_cmd(self, interaction: discord.Interaction):
        view = EmbedDashboard(interaction.user, self)
        await interaction.response.send_message(view=view)


class EmbedDashboard(PrivateLayoutView):
    def __init__(self, user: discord.abc.User, cog: Embeds):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("## Embed Creator Dashboard"))
        container.add_item(
            discord.ui.TextDisplay(
                "Create embeds, send them, and manage them using this dashboard. "
                "Dopamine allows you to customize all fields of an embed."
            )
        )
        container.add_item(discord.ui.Separator())

        create_btn = discord.ui.Button(
            label="Create", style=discord.ButtonStyle.primary
        )
        send_btn = discord.ui.Button(
            label="Send", style=discord.ButtonStyle.primary
        )
        manage_btn = discord.ui.Button(
            label="Manage & Edit", style=discord.ButtonStyle.secondary
        )

        create_btn.callback = self.create_callback
        send_btn.callback = self.send_callback
        manage_btn.callback = self.manage_callback

        row = discord.ui.ActionRow()
        row.add_item(create_btn)
        row.add_item(send_btn)
        row.add_item(manage_btn)

        container.add_item(row)
        self.add_item(container)

    async def create_callback(self, interaction: discord.Interaction):
        draft = EmbedDraft(guild_id=interaction.guild.id)
        embed = self.cog.build_embed_from_draft(draft)

        expires_ts = get_now_plus_seconds_unix(1800)
        view = EmbedPreviewView(self.cog, self.user, draft, expires_ts=expires_ts)

        await interaction.response.send_message(
            content=view.get_formatted_content(),
            embed=embed,
            view=view,
        )
        view.message = await interaction.original_response()

    async def send_callback(self, interaction: discord.Interaction):
        embeds = await self.cog.fetch_embeds_for_guild(interaction.guild.id)
        if not embeds:
            return await interaction.response.send_message(
                "No saved embeds found for this server.", ephemeral=True
            )

        view = UseEmbedPage(
            user=self.user,
            cog=self.cog,
            guild_id=interaction.guild.id,
            embeds=embeds,
            returnembed=False,
        )
        await interaction.response.send_message(view=view)

    async def manage_callback(self, interaction: discord.Interaction):
        embeds = await self.cog.fetch_embeds_for_guild(interaction.guild.id)
        view = ManageEmbedPage(
            user=self.user,
            cog=self.cog,
            guild_id=interaction.guild.id,
            embeds=embeds,
            page=1,
            delete_mode=False,
        )
        await interaction.response.edit_message(view=view)


class ManageEmbedPage(PrivateLayoutView):
    def __init__(
        self,
        user: discord.abc.User,
        cog: Embeds,
        guild_id: int,
        embeds: List[Dict[str, Any]],
        page: int = 1,
        delete_mode: bool = False,
    ):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.embeds = embeds
        self.page = page
        self.items_per_page = 5
        self.delete_mode = delete_mode
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("## Edit Embeds"))
        container.add_item(discord.ui.Separator())

        total_items = len(self.embeds)
        total_pages = (total_items + self.items_per_page - 1) // self.items_per_page if total_items > 0 else 1

        start_idx = (self.page - 1) * self.items_per_page
        end_idx = start_idx + self.items_per_page
        current_embeds = self.embeds[start_idx:end_idx]

        if not current_embeds:
            container.add_item(discord.ui.TextDisplay("*No saved embeds found.*"))
            container.add_item(discord.ui.Separator())
        else:
            for idx, record in enumerate(current_embeds, start=start_idx + 1):
                title = record.get("title") or (record.get("description") or "")[:20] or "Untitled Embed"

                btn_label = "Delete" if self.delete_mode else "Edit"
                btn_style = discord.ButtonStyle.danger if self.delete_mode else discord.ButtonStyle.secondary

                edit_btn = discord.ui.Button(label=btn_label, style=btn_style)
                edit_btn.callback = self.make_entry_callback(record, total_items)

                display_text = f"### {idx}. {title}"
                container.add_item(
                    discord.ui.Section(
                        discord.ui.TextDisplay(display_text),
                        accessory=edit_btn,
                    )
                )

            container.add_item(discord.ui.Separator())

            nav_row = discord.ui.ActionRow()
            left_btn = discord.ui.Button(
                emoji="◀️",
                style=discord.ButtonStyle.primary,
                disabled=(self.page <= 1),
            )
            left_btn.callback = self.prev_page
            nav_row.add_item(left_btn)

            go_btn = discord.ui.Button(
                label=f"Page {self.page} of {total_pages}",
                style=discord.ButtonStyle.secondary,
                disabled=(total_pages == 1),
            )

            async def go_to_page_callback(interaction: discord.Interaction):
                modal = ManageGoToPageModal(self, total_pages)
                await interaction.response.send_modal(modal)

            go_btn.callback = go_to_page_callback
            nav_row.add_item(go_btn)

            right_btn = discord.ui.Button(
                emoji="▶️",
                style=discord.ButtonStyle.primary,
                disabled=(self.page >= total_pages),
            )
            right_btn.callback = self.next_page
            nav_row.add_item(right_btn)

            container.add_item(nav_row)
            container.add_item(discord.ui.Separator())

        control_row = discord.ui.ActionRow()
        toggle_delete_btn = discord.ui.Button(
            label=f"{'Disable' if self.delete_mode else 'Enable'} Delete Mode",
            style=discord.ButtonStyle.danger if self.delete_mode else discord.ButtonStyle.secondary,
        )
        toggle_delete_btn.callback = self.toggle_delete
        control_row.add_item(toggle_delete_btn)

        container.add_item(control_row)
        control_row = discord.ui.ActionRow()
        return_btn = discord.ui.Button(emoji=self.cog.bot.back_emoji, label="Back", style=discord.ButtonStyle.secondary)
        return_btn.callback = self.return_home
        container.add_item(discord.ui.Separator())
        control_row.add_item(return_btn)

        container.add_item(control_row)
        self.add_item(container)

    def make_entry_callback(self, record: Dict[str, Any], total_items: int):
        async def callback(interaction: discord.Interaction):
            embed_id = record["id"]
            if self.delete_mode:
                await self.cog.delete_embed(self.guild_id, embed_id)
                self.embeds = [e for e in self.embeds if e["id"] != embed_id]
                new_total = len(self.embeds)
                max_pages = (new_total + self.items_per_page - 1) // self.items_per_page if new_total > 0 else 1
                self.page = min(self.page, max_pages) if max_pages > 0 else 1
                self.build_layout()
                await interaction.response.edit_message(view=self)
            else:
                draft = self.cog.build_draft_from_row(record)
                preview_embed = self.cog.build_embed_from_draft(draft)


                expires_ts = get_now_plus_seconds_unix(1800)
                view = EmbedPreviewView(self.cog, self.user, draft, existing_id=embed_id, parent_view=self,
                                        expires_ts=expires_ts)

                await interaction.response.send_message(
                    content=view.get_formatted_content(),
                    embed=preview_embed,
                    view=view,
                )
                view.message = await interaction.original_response()

        return callback

    async def prev_page(self, interaction: discord.Interaction):
        self.page -= 1
        self.build_layout()
        await interaction.response.edit_message(view=self)

    async def next_page(self, interaction: discord.Interaction):
        self.page += 1
        self.build_layout()
        await interaction.response.edit_message(view=self)

    async def toggle_delete(self, interaction: discord.Interaction):
        self.delete_mode = not self.delete_mode
        self.build_layout()
        await interaction.response.edit_message(view=self)

    async def return_home(self, interaction: discord.Interaction):
        view = EmbedDashboard(self.user, self.cog)
        await interaction.response.edit_message(view=view)


class ManageGoToPageModal(discord.ui.Modal):
    def __init__(self, parent_view: ManageEmbedPage, total_pages: int):
        super().__init__(title="Jump to Page")
        self.parent_view = parent_view
        self.total_pages = total_pages

        self.page_input = discord.ui.TextInput(
            label=f"Page Number (1-{total_pages})",
            placeholder="Enter a page number...",
            min_length=1,
            max_length=5,
            required=True,
        )
        self.add_item(self.page_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            page_num = int(self.page_input.value)
            if 1 <= page_num <= self.total_pages:
                self.parent_view.page = page_num
                self.parent_view.build_layout()
                await interaction.response.edit_message(view=self.parent_view)
            else:
                await interaction.response.send_message(
                    f"Enter a number between 1 and {self.total_pages}.",
                    ephemeral=True,
                )
        except ValueError:
            await interaction.response.send_message("Invalid input.", ephemeral=True)


class UseEmbedPage(PrivateLayoutView):
    def __init__(
        self,
        user: discord.abc.User,
        cog: Embeds,
        guild_id: int,
        embeds: List[Dict[str, Any]],
        returnembed: bool = True,
        on_pick=None,
    ):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.embeds = embeds
        self.returnembed = returnembed
        self.on_pick = on_pick
        self.page = 1
        self.items_per_page = 5
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        refresh_btn = discord.ui.Button(
            label="Refresh List",
            style=discord.ButtonStyle.success,
        )
        refresh_btn.callback = self.refresh_callback
        container.add_item(discord.ui.Section(discord.ui.TextDisplay("## Pick an Embed to Use"), accessory=refresh_btn))
        container.add_item(discord.ui.TextDisplay("To create a new embed, use `/embed` -> click Create button."))
        container.add_item(discord.ui.Separator())

        total_items = len(self.embeds)
        total_pages = (total_items + self.items_per_page - 1) // self.items_per_page if total_items > 0 else 1

        start_idx = (self.page - 1) * self.items_per_page
        end_idx = start_idx + self.items_per_page
        current_embeds = self.embeds[start_idx:end_idx]

        if not current_embeds:
            container.add_item(discord.ui.TextDisplay("*No saved embeds found.*"))
        else:
            for idx, record in enumerate(current_embeds, start=start_idx + 1):
                title = record.get("title") or (record.get("description") or "")[:20] or "Untitled Embed"

                use_btn = discord.ui.Button(
                    label="Use",
                    style=discord.ButtonStyle.primary,
                )
                use_btn.callback = self.make_use_callback(record)

                display_text = f"### {idx}. {title}"
                container.add_item(
                    discord.ui.Section(
                        discord.ui.TextDisplay(display_text),
                        accessory=use_btn,
                    )
                )

            container.add_item(discord.ui.Separator())

            nav_row = discord.ui.ActionRow()
            left_btn = discord.ui.Button(
                emoji="◀️",
                style=discord.ButtonStyle.primary,
                disabled=(self.page <= 1),
            )
            left_btn.callback = self.prev_page
            nav_row.add_item(left_btn)

            go_btn = discord.ui.Button(
                label=f"Page {self.page} of {total_pages}",
                style=discord.ButtonStyle.secondary,
                disabled=(total_pages == 1),
            )

            async def go_to_page_callback(interaction: discord.Interaction):
                modal = UseGoToPageModal(self, total_pages)
                await interaction.response.send_modal(modal)

            go_btn.callback = go_to_page_callback
            nav_row.add_item(go_btn)

            right_btn = discord.ui.Button(
                emoji="▶️",
                style=discord.ButtonStyle.primary,
                disabled=(self.page >= total_pages),
            )
            right_btn.callback = self.next_page
            nav_row.add_item(right_btn)

            container.add_item(nav_row)

        self.add_item(container)

    async def refresh_callback(self, interaction: discord.Interaction):
        self.embeds = await self.cog.fetch_embeds_for_guild(self.guild_id)

        total_items = len(self.embeds)
        total_pages = (total_items + self.items_per_page - 1) // self.items_per_page if total_items > 0 else 1
        if self.page > total_pages:
            self.page = total_pages

        self.build_layout()
        await interaction.response.edit_message(view=self)

    def make_use_callback(self, record: Dict[str, Any]):
        async def callback(interaction: discord.Interaction):
            embed_obj = self.cog.build_embed_from_row(record)
            content = record.get("content") or None

            if self.returnembed:
                if self.on_pick is not None:
                    await self.on_pick(interaction, content, embed_obj)
                    return
                await interaction.response.send_message(
                    content=content,
                    embed=embed_obj,
                    ephemeral=True,
                )
            else:
                view = LayoutViewChannelSelect(self.user, self.cog, record)
                await interaction.response.edit_message(view=view)

        return callback

    async def prev_page(self, interaction: discord.Interaction):
        self.page -= 1
        self.build_layout()
        await interaction.response.edit_message(view=self)

    async def next_page(self, interaction: discord.Interaction):
        self.page += 1
        self.build_layout()
        await interaction.response.edit_message(view=self)


class UseGoToPageModal(discord.ui.Modal):
    def __init__(self, parent_view: UseEmbedPage, total_pages: int):
        super().__init__(title="Jump to Page")
        self.parent_view = parent_view
        self.total_pages = total_pages

        self.page_input = discord.ui.TextInput(
            label=f"Page Number (1-{total_pages})",
            placeholder="Enter a page number...",
            min_length=1,
            max_length=5,
            required=True,
        )
        self.add_item(self.page_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            page_num = int(self.page_input.value)
            if 1 <= page_num <= self.total_pages:
                self.parent_view.page = page_num
                self.parent_view.build_layout()
                await interaction.response.edit_message(view=self.parent_view)
            else:
                await interaction.response.send_message(
                    f"Enter a number between 1 and {self.total_pages}.",
                    ephemeral=True,
                )
        except ValueError:
            await interaction.response.send_message("Invalid input.", ephemeral=True)


class LayoutViewChannelSelect(PrivateLayoutView):
    def __init__(self, user: discord.abc.User, cog: Embeds, embed_record: Dict[str, Any]):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.embed_record = embed_record
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        select = discord.ui.ChannelSelect(
            placeholder="Select a channel...",
            min_values=1,
            max_values=1,
            channel_types=[discord.ChannelType.text],
        )
        select.callback = self.select_channel
        container.add_item(
            discord.ui.TextDisplay(
                "## Select the channel where you want the embed to be sent:"
            )
        )
        container.add_item(discord.ui.ActionRow(select))
        self.add_item(container)

    async def select_channel(self, interaction: discord.Interaction):
        channel_id = int(interaction.data['values'][0])
        channel = interaction.guild.get_channel(channel_id) or await interaction.guild.fetch_channel(channel_id)

        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message(
                "The selected item is not a text channel. Please select a valid text channel.",
                ephemeral=True
            )

        embed_obj = self.cog.build_embed_from_row(self.embed_record)
        content = self.embed_record.get("content") or None

        try:
            await channel.send(content=content, embed=embed_obj)
            await interaction.response.send_message(
                f"Embed sent to {channel.mention} successfully.", ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "I don't have permission to send messages in that channel.",
                ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(
                f"An unexpected error occurred: {e}",
                ephemeral=True
            )


class EmbedPreviewView(PrivateView):
    def __init__(
            self,
            cog: Embeds,
            user: discord.abc.User,
            draft: EmbedDraft,
            existing_id: Optional[int] = None,
            parent_view: Optional[ManageEmbedPage] = None,
            expires_ts: int = 0,
    ):
        super().__init__(user, timeout=1800)
        self.cog = cog
        self.draft = draft
        self.existing_id = existing_id
        self.parent_view = parent_view
        self.expires_ts = expires_ts
        self.message: Optional[discord.Message] = None

    def get_formatted_content(self) -> str:
        prefix = (
            "This is a preview of your embed. Configure it using the buttons below, then save and/or send it.\n"
            f"This preview expires **<t:{self.expires_ts}:R>**!"
        )
        if self.draft.content:
            return f"{prefix}\n\n{self.draft.content}"
        return prefix

    async def _update_parent_cache(self):
        if self.parent_view and self.existing_id:
            updated_embeds = await self.cog.fetch_embeds_for_guild(self.parent_view.guild_id)
            self.parent_view.embeds = updated_embeds
            self.parent_view.build_layout()

    @discord.ui.button(label="Save", style=discord.ButtonStyle.green)
    async def save_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed_id = await self.cog.save_embed(
            guild_id=interaction.guild.id,
            user_id=interaction.user.id,
            draft=self.draft,
            existing_id=self.existing_id,
        )
        self.existing_id = embed_id

        await self._update_parent_cache()

        await interaction.response.edit_message(content=f"Embed saved successfully.", embed=None, view=None)

    @discord.ui.button(label="Save & Send", style=discord.ButtonStyle.blurple)
    async def save_and_send_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed_id = await self.cog.save_embed(
            guild_id=interaction.guild.id,
            user_id=interaction.user.id,
            draft=self.draft,
            existing_id=self.existing_id,
        )
        self.existing_id = embed_id

        await self._update_parent_cache()

        view = ViewChannelSelect(self.cog, self.draft)
        await interaction.response.edit_message(content="## Select the channel where you want the embed to be sent:",
                                                embed=None, view=view)

    @discord.ui.button(label="Send Without Saving", style=discord.ButtonStyle.secondary)
    async def send_without_saving_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = ViewChannelSelect(self.cog, self.draft)
        await interaction.response.edit_message(
            content="## Select the channel where you want the embed to be sent:",
            embed=None,
            view=view
        )

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.primary)
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.message = interaction.message
        view = discord.ui.View()
        select = EmbedEditSelect(cog=self.cog, draft=self.draft, parent_view=self)
        view.add_item(select)

        await interaction.response.send_message(
            embed=discord.Embed(
                title="Edit Embed",
                description="Select a field to edit...",
                color=discord.Color(0x944ae8),
            ),
            view=view,
            ephemeral=True,
        )
        self.message = interaction.message

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content=None,
            embed=discord.Embed(title="Embed creation cancelled."),
            view=None,
        )
        self.stop()


class ViewChannelSelect(discord.ui.View):
    def __init__(self, cog: Embeds, draft: EmbedDraft):
        super().__init__(timeout=300)
        self.cog = cog
        self.draft = draft
        self.select = discord.ui.ChannelSelect(
            placeholder="Select a channel...",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
        )
        self.select.callback = self.select_channel
        self.add_item(self.select)

    async def select_channel(self, interaction: discord.Interaction):
        channel_id = self.select.values[0].id
        ch = self.cog.bot.get_channel(channel_id) or await interaction.guild.fetch_channel(channel_id)

        if not isinstance(ch, discord.TextChannel):
            return await interaction.response.send_message(
                "The selected item is not a text channel.",
                ephemeral=True
            )

        try:
            embed_obj = self.cog.build_embed_from_draft(self.draft)
            await ch.send(content=self.draft.content or None, embed=embed_obj)

            await interaction.response.edit_message(
                content=f"Embed sent to {ch.mention} successfully.",
                embed=None,
                view=None,
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "I don't have permission to send messages in that channel.",
                ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(
                f"An error occurred: {e}",
                ephemeral=True
            )


class EmbedEditSelect(discord.ui.Select):
    def __init__(self, cog: Embeds, draft: EmbedDraft, parent_view: EmbedPreviewView):
        self.cog = cog
        self.draft = draft
        self.parent_view = parent_view

        options = [
            discord.SelectOption(
                label="1. Content",
                value="content",
                description="Change the message content (outside the embed).",
            ),
            discord.SelectOption(
                label="2. Title",
                value="title",
                description="Change the embed title.",
            ),
            discord.SelectOption(
                label="3. Description",
                value="description",
                description="Change the embed description.",
            ),
            discord.SelectOption(
                label="4. Colour",
                value="color",
                description="Set embed color (Hex or Valid Name).",
            ),
            discord.SelectOption(
                label="5. URL",
                value="url",
                description="Set the embed URL.",
            ),
            discord.SelectOption(
                label="6. Thumbnail",
                value="thumbnail",
                description="Provide a valid URL for the embed thumbnail.",
            ),
            discord.SelectOption(
                label="7. Image",
                value="image",
                description="Provide a valid URL for the embed image.",
            ),
            discord.SelectOption(
                label="8. Footer Text",
                value="footer_text",
                description="Edit the footer text.",
            ),
            discord.SelectOption(
                label="9. Footer Icon URL",
                value="footer_icon_url",
                description="Edit the footer icon URL.",
            ),
            discord.SelectOption(
                label="10. Author Name",
                value="author_name",
                description="Edit the author name.",
            ),
            discord.SelectOption(
                label="11. Author Icon URL",
                value="author_icon_url",
                description="Edit the author icon URL.",
            ),
            discord.SelectOption(
                label="12. Timestamp",
                value="timestamp",
                description="Toggle showing the current timestamp.",
            ),
        ]

        super().__init__(
            placeholder="Select a field to customize...",
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        value = self.values[0]

        if value == "timestamp":
            self.draft.timestamp_enabled = not self.draft.timestamp_enabled
            new_embed = self.cog.build_embed_from_draft(self.draft)

            await self.parent_view.message.edit(
                content=self.parent_view.get_formatted_content(),
                embed=new_embed
            )
            state = "enabled" if self.draft.timestamp_enabled else "disabled"
            return await interaction.response.send_message(
                f"Timestamp {state} successfully!", ephemeral=True
            )

        trait = value
        await interaction.response.send_modal(
            EmbedFieldModal(trait, self.draft, self.parent_view)
        )


class EmbedFieldModal(discord.ui.Modal):
    def __init__(self, trait: str, draft: EmbedDraft, parent_view: EmbedPreviewView):
        title_map = {
            "content": "Edit Content",
            "title": "Edit Title",
            "description": "Edit Description",
            "color": "Edit Colour",
            "url": "Edit URL",
            "thumbnail": "Edit Thumbnail URL",
            "image": "Edit Image URL",
            "footer_text": "Edit Footer Text",
            "footer_icon_url": "Edit Footer Icon URL",
            "author_name": "Edit Author Name",
            "author_icon_url": "Edit Author Icon URL",
        }
        super().__init__(title=title_map.get(trait, "Edit Field"))
        self.trait = trait
        self.draft = draft
        self.parent_view = parent_view

        current_value = ""
        if trait == "content":
            current_value = self.draft.content
        elif trait == "title":
            current_value = self.draft.title
        elif trait == "description":
            current_value = self.draft.description
        elif trait == "color":
            current_value = self.draft.color
        elif trait == "url":
            current_value = self.draft.url or ""
        elif trait == "thumbnail":
            current_value = self.draft.thumbnail_url
        elif trait == "image":
            current_value = self.draft.image_url
        elif trait == "footer_text":
            current_value = self.draft.footer_text
        elif trait == "footer_icon_url":
            current_value = self.draft.footer_icon_url
        elif trait == "author_name":
            current_value = self.draft.author_name
        elif trait == "author_icon_url":
            current_value = self.draft.author_icon_url

        if trait == "description":
            self.input_field = discord.ui.TextInput(
                label="Enter description",
                placeholder="Type here...",
                style=discord.TextStyle.long,
                default=current_value,
                required=False,
                min_length=1,
                max_length=4000
            )
        else:
            self.input_field = discord.ui.TextInput(
                label=f"Enter value",
                           placeholder="Type here...",
                default=current_value,
                required=False,
            )
        self.add_item(self.input_field)

    async def on_submit(self, interaction: discord.Interaction):
        value = self.input_field.value.strip()

        if self.trait == "content":
            self.draft.content = value
        elif self.trait == "title":
            self.draft.title = value
        elif self.trait == "description":
            self.draft.description = value
        elif self.trait == "color":
            self.draft.color = value or "0x944ae8"
        elif self.trait == "url":
            self.draft.url = value or None
        elif self.trait == "thumbnail":
            self.draft.thumbnail_url = value
        elif self.trait == "image":
            self.draft.image_url = value
        elif self.trait == "footer_text":
            self.draft.footer_text = value
        elif self.trait == "footer_icon_url":
            self.draft.footer_icon_url = value
        elif self.trait == "author_name":
            self.draft.author_name = value
        elif self.trait == "author_icon_url":
            self.draft.author_icon_url = value

        new_embed = self.parent_view.cog.build_embed_from_draft(self.draft)

        await self.parent_view.message.edit(
            content=self.parent_view.get_formatted_content(),
            embed=new_embed,
        )

        pretty = self.trait.replace("_", " ").title()
        await interaction.response.send_message(
            f"Updated **{pretty}** successfully!",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Embeds(bot))