from __future__ import annotations

import discord
from beacon import PrivateLayoutView

from utils.data_protocol import DataFeatureMeta


class DestructiveConfirmationView(PrivateLayoutView):
    def __init__(self, user, title_text: str, body_text: str):
        super().__init__(user, timeout=30)
        self.value = None
        self.title_text = title_text
        self.body_text = body_text
        self.message: discord.Message | None = None
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        color = discord.Color.green() if self.value is True else discord.Color(0xdf5046) if self.value is False else None
        container = discord.ui.Container(accent_color=color)
        title = self.title_text
        if self.value is True:
            title = "Action Confirmed"
        elif self.value is False:
            title = "Action Canceled"
        container.add_item(discord.ui.TextDisplay(f"### {title}"))
        container.add_item(discord.ui.Separator())
        body = f"~~{self.body_text}~~" if self.value is not None else self.body_text
        container.add_item(discord.ui.TextDisplay(body))
        if self.value is None:
            row = discord.ui.ActionRow()
            cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
            confirm = discord.ui.Button(label="Confirm", style=discord.ButtonStyle.danger)
            cancel.callback = self._cancel
            confirm.callback = self._confirm
            row.add_item(cancel)
            row.add_item(confirm)
            container.add_item(discord.ui.Separator())
            container.add_item(row)
        self.add_item(container)

    async def _finish(self, interaction: discord.Interaction, confirmed: bool):
        self.value = confirmed
        self.build_layout()
        if interaction.response.is_done():
            await interaction.edit_original_response(view=self)
        else:
            await interaction.response.edit_message(view=self)
        self.stop()

    async def _cancel(self, interaction: discord.Interaction):
        await self._finish(interaction, False)

    async def _confirm(self, interaction: discord.Interaction):
        await self._finish(interaction, True)

    async def on_timeout(self):
        if self.value is None and self.message:
            self.value = False
            self.build_layout()
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
            self.stop()


class ExportQueuedView(PrivateLayoutView):
    def __init__(self, cog, user, scope: str, message: str):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.scope = scope
        self.message = message
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("## Export Queued"))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(self.message))
        container.add_item(discord.ui.Separator())
        back = discord.ui.Button(emoji=self.cog.bot.back_emoji, label="Back", style=discord.ButtonStyle.secondary)
        back.callback = self._back
        container.add_item(discord.ui.ActionRow(back))
        self.add_item(container)

    async def _back(self, interaction: discord.Interaction):
        hub = UserDataHub if self.scope in ("user", "feature_user") else ServerDataHub
        await interaction.response.edit_message(view=hub(self.cog, self.user))


class DataHome(PrivateLayoutView):
    def __init__(self, cog, user: discord.User | discord.Member):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.is_admin = isinstance(user, discord.Member) and user.guild_permissions.administrator
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("## Your Data & Privacy"))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(
            "Dopamine stores data to power moderation, giveaways, and other features. "
            "This dashboard lets you **view**, **export**, or **delete** data we hold about you or your server.\n\n"
            "* Exports are sent to your DMs within a few minutes.\n"
            "* Moderation infraction records cannot be deleted from your personal data (server admins can clear server data).\n"
            "* Export requests are limited to once every 24 hours."
        ))
        container.add_item(discord.ui.Separator())
        row = discord.ui.ActionRow()
        user_btn = discord.ui.Button(label="User Data", style=discord.ButtonStyle.primary)
        server_btn = discord.ui.Button(label="Server Data", style=discord.ButtonStyle.secondary, disabled=not self.is_admin)
        user_btn.callback = self._user
        server_btn.callback = self._server
        row.add_item(user_btn)
        row.add_item(server_btn)
        container.add_item(row)
        self.add_item(container)

    async def _user(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=UserDataHub(self.cog, self.user))

    async def _server(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=ServerDataHub(self.cog, self.user))


class UserDataHub(PrivateLayoutView):
    def __init__(self, cog, user):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("## User Data"))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(
            "Manage data stored about **you** across all servers Dopamine is in."
        ))
        container.add_item(discord.ui.Separator())
        row = discord.ui.ActionRow()
        view_btn = discord.ui.Button(label="View My Data", style=discord.ButtonStyle.primary)
        del_btn = discord.ui.Button(label="Delete My Data", style=discord.ButtonStyle.danger)
        feat_btn = discord.ui.Button(label="Manage by Feature", style=discord.ButtonStyle.secondary)
        view_btn.callback = self._view
        del_btn.callback = self._delete
        feat_btn.callback = self._features
        row.add_item(view_btn)
        row.add_item(del_btn)
        row.add_item(feat_btn)
        container.add_item(row)
        back = discord.ui.Button(emoji=self.cog.bot.back_emoji, label="Back", style=discord.ButtonStyle.secondary)
        back.callback = self._back
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(back))
        self.add_item(container)

    async def _view(self, interaction: discord.Interaction):
        await self.cog.queue_export(interaction, scope="user")

    async def _delete(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=UserDeleteHub(self.cog, self.user))

    async def _features(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            view=FeatureBrowserPage(self.cog, self.user, scope="user")
        )

    async def _back(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=DataHome(self.cog, self.user))


class ServerDataHub(PrivateLayoutView):
    def __init__(self, cog, user):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("## Server Data"))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(
            f"Manage all data stored for **{self.user.guild.name}**. "
            "This includes moderation records and all feature configuration."
        ))
        container.add_item(discord.ui.Separator())
        row = discord.ui.ActionRow()
        view_btn = discord.ui.Button(label="View Server Data", style=discord.ButtonStyle.primary)
        del_btn = discord.ui.Button(label="Delete Server Data", style=discord.ButtonStyle.danger)
        feat_btn = discord.ui.Button(label="Manage by Feature", style=discord.ButtonStyle.secondary)
        view_btn.callback = self._view
        del_btn.callback = self._delete
        feat_btn.callback = self._features
        row.add_item(view_btn)
        row.add_item(del_btn)
        row.add_item(feat_btn)
        container.add_item(row)
        back = discord.ui.Button(emoji=self.cog.bot.back_emoji, label="Back", style=discord.ButtonStyle.secondary)
        back.callback = self._back
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(back))
        self.add_item(container)

    async def _view(self, interaction: discord.Interaction):
        await self.cog.queue_export(interaction, scope="guild")

    async def _delete(self, interaction: discord.Interaction):
        view = DestructiveConfirmationView(
            self.user,
            "Delete All Server Data",
            "This will **permanently delete** all Dopamine data for this server, including moderation cases. This cannot be undone.",
        )
        await interaction.response.send_message(view=view, ephemeral=True)
        view.message = await interaction.original_response()
        await view.wait()
        if view.value:
            await self.cog.run_guild_delete(interaction.guild.id)
            await interaction.followup.send("Server data deletion completed.", ephemeral=True)

    async def _features(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            view=FeatureBrowserPage(self.cog, self.user, scope="guild")
        )

    async def _back(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=DataHome(self.cog, self.user))


class FeatureBrowserPage(PrivateLayoutView):
    per_page = 5

    def __init__(self, cog, user, scope: str, page: int = 1, delete_mode: bool = False):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.scope = scope
        self.page = page
        self.delete_mode = delete_mode
        self.features: list[DataFeatureMeta] = cog.get_features_for_scope(scope)
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        label = "User" if self.scope == "user" else "Server"
        container.add_item(discord.ui.TextDisplay(f"## {label} Data by Feature"))
        container.add_item(discord.ui.Separator())
        if self.delete_mode:
            container.add_item(discord.ui.TextDisplay(
                "-# **Delete mode** is on. Use the red buttons to delete data. Protected features are disabled."
            ))
            container.add_item(discord.ui.Separator())
        total = max(1, (len(self.features) + self.per_page - 1) // self.per_page)
        start = (self.page - 1) * self.per_page
        page_feats = self.features[start:start + self.per_page]
        if not page_feats:
            container.add_item(discord.ui.TextDisplay("No features with data in this scope."))
        for feat in page_feats:
            can_del = feat.user_delete if self.scope == "user" else feat.guild_delete
            badges = []
            if feat.user_export or feat.guild_export:
                badges.append("Exportable")
            if can_del:
                badges.append("Deletable")
            elif self.scope == "user" and feat.user_export:
                badges.append("Protected")
            desc = f"{' · '.join(badges)}" if badges else ""
            if feat.user_delete_note and self.scope == "user" and not can_del:
                desc += f"\n-# {feat.user_delete_note}"
            if self.delete_mode:
                btn_label = "Delete"
                btn_style = discord.ButtonStyle.danger
                btn_disabled = not can_del
            else:
                btn_label = "View"
                btn_style = discord.ButtonStyle.secondary
                btn_disabled = not (feat.user_export if self.scope == "user" else feat.guild_export)
            btn = discord.ui.Button(label=btn_label, style=btn_style, disabled=btn_disabled)
            btn.callback = self._make_cb(feat)
            title = feat.name if not desc else f"{feat.name}\n{desc}"
            container.add_item(discord.ui.Section(
                discord.ui.TextDisplay(f"### {title}"),
                accessory=btn,
            ))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(f"-# Page {self.page} of {total}"))
        nav = discord.ui.ActionRow()
        prev_b = discord.ui.Button(emoji="◀️", style=discord.ButtonStyle.primary, disabled=self.page <= 1)
        next_b = discord.ui.Button(emoji="▶️", style=discord.ButtonStyle.primary, disabled=self.page >= total)
        prev_b.callback = self._prev
        next_b.callback = self._next
        nav.add_item(prev_b)
        nav.add_item(next_b)
        container.add_item(nav)
        toggle = discord.ui.Button(
            label=f"{'Disable' if self.delete_mode else 'Enable'} Delete Mode",
            style=discord.ButtonStyle.danger if self.delete_mode else discord.ButtonStyle.secondary,
        )
        toggle.callback = self._toggle_delete
        container.add_item(discord.ui.ActionRow(toggle))
        back = discord.ui.Button(emoji=self.cog.bot.back_emoji, label="Back", style=discord.ButtonStyle.secondary)
        back.callback = self._back
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(back))
        self.add_item(container)

    def _make_cb(self, feat: DataFeatureMeta):
        async def cb(interaction: discord.Interaction):
            can_del = feat.user_delete if self.scope == "user" else feat.guild_delete
            if self.delete_mode:
                if not can_del:
                    return
                view = DestructiveConfirmationView(
                    self.user,
                    f"Delete {feat.name} Data",
                    f"Permanently delete **{feat.name}** data?",
                )
                await interaction.response.send_message(view=view, ephemeral=True)
                view.message = await interaction.original_response()
                await view.wait()
                if view.value:
                    if self.scope == "user":
                        await self.cog.run_user_delete(interaction.user.id, feature_id=feat.feature_id)
                    else:
                        await self.cog.run_guild_delete(interaction.guild.id, feature_id=feat.feature_id)
                    await interaction.followup.send(f"Deleted {feat.name} data.", ephemeral=True)
            else:
                await self.cog.queue_export(
                    interaction, scope=f"feature_{self.scope}", feature_id=feat.feature_id
                )
        return cb

    async def _toggle_delete(self, interaction: discord.Interaction):
        self.delete_mode = not self.delete_mode
        self.build_layout()
        await interaction.response.edit_message(view=self)

    async def _prev(self, interaction: discord.Interaction):
        self.page -= 1
        self.build_layout()
        await interaction.response.edit_message(view=self)

    async def _next(self, interaction: discord.Interaction):
        self.page += 1
        self.build_layout()
        await interaction.response.edit_message(view=self)

    async def _back(self, interaction: discord.Interaction):
        hub = UserDataHub if self.scope == "user" else ServerDataHub
        await interaction.response.edit_message(view=hub(self.cog, self.user))


class UserDeleteHub(PrivateLayoutView):
    def __init__(self, cog, user):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("## Delete User Data"))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(
            "**Will NOT be deleted:** moderation infractions, points, pending punishments, or ban schedules.\n\n"
            "**Can be deleted:** AFK, notes, giveaway entries, usage stats, and similar participation data."
        ))
        container.add_item(discord.ui.Separator())
        all_btn = discord.ui.Button(label="Delete from All Servers", style=discord.ButtonStyle.danger)
        all_btn.callback = self._all_servers
        container.add_item(discord.ui.ActionRow(all_btn))
        pick_btn = discord.ui.Button(label="Pick Servers…", style=discord.ButtonStyle.secondary)
        pick_btn.callback = self._pick_servers
        container.add_item(discord.ui.ActionRow(pick_btn))
        back = discord.ui.Button(emoji=self.cog.bot.back_emoji, label="Back", style=discord.ButtonStyle.secondary)
        back.callback = self._back
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(back))
        self.add_item(container)

    async def _all_servers(self, interaction: discord.Interaction):
        view = DestructiveConfirmationView(
            self.user,
            "Delete All Personal Data",
            "Delete all deletable personal data across **every server**, including global data (notes, AFK, usage)?",
        )
        await interaction.response.send_message(view=view, ephemeral=True)
        view.message = await interaction.original_response()
        await view.wait()
        if view.value:
            await self.cog.run_user_delete(interaction.user.id, guild_ids=None)
            await interaction.followup.send("Personal data deletion completed.", ephemeral=True)

    async def _pick_servers(self, interaction: discord.Interaction):
        guild_ids = await self.cog.discover_user_guilds(interaction.user.id)
        if not guild_ids:
            return await interaction.response.send_message(
                "No guild-scoped deletable data found.", ephemeral=True
            )
        await interaction.response.edit_message(
            view=GuildDeleteSelectPage(self.cog, self.user, guild_ids)
        )

    async def _back(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=UserDataHub(self.cog, self.user))


class GuildDeleteSelectPage(PrivateLayoutView):
    def __init__(self, cog, user, guild_ids: list[int]):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.guild_ids = guild_ids[:25]
        self._pending: list[int] = []
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("## Select Servers"))
        container.add_item(discord.ui.Separator())
        options = []
        for gid in self.guild_ids:
            guild = self.cog.bot.get_guild(gid)
            name = guild.name if guild else str(gid)
            options.append(discord.SelectOption(label=name[:100], value=str(gid)))
        select = discord.ui.Select(
            placeholder="Choose servers (up to 25)",
            min_values=1,
            max_values=min(25, len(options)),
            options=options,
        )
        select.callback = self._on_select
        container.add_item(discord.ui.ActionRow(select))
        guild_only = discord.ui.Button(label="Delete Selected (guild only)", style=discord.ButtonStyle.secondary)
        guild_only.callback = self._confirm_guild_only
        global_row = discord.ui.ActionRow()
        toggle = discord.ui.Button(label="Delete Selected + global data", style=discord.ButtonStyle.danger)
        toggle.callback = self._confirm_with_global
        global_row.add_item(guild_only)
        global_row.add_item(toggle)
        container.add_item(global_row)
        back = discord.ui.Button(emoji=self.cog.bot.back_emoji, label="Back", style=discord.ButtonStyle.secondary)
        back.callback = self._back
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(back))
        self.add_item(container)

    async def _on_select(self, interaction: discord.Interaction):
        self._pending = [int(v) for v in interaction.data["values"]]
        await interaction.response.send_message(
            f"Selected {len(self._pending)} server(s). Choose a delete option below.",
            ephemeral=True,
        )

    async def _confirm_guild_only(self, interaction: discord.Interaction):
        if not self._pending:
            return await interaction.response.send_message("Select at least one server first.", ephemeral=True)
        view = DestructiveConfirmationView(
            self.user,
            "Delete Selected Data",
            f"Delete deletable data for {len(self._pending)} server(s) only (no global notes/AFK)?",
        )
        await interaction.response.send_message(view=view, ephemeral=True)
        view.message = await interaction.original_response()
        await view.wait()
        if view.value:
            await self.cog.run_user_delete(interaction.user.id, guild_ids=self._pending, include_global=False)
            await interaction.followup.send("Deletion completed.", ephemeral=True)

    async def _confirm_with_global(self, interaction: discord.Interaction):
        if not self._pending:
            return await interaction.response.send_message("Select at least one server first.", ephemeral=True)
        view = DestructiveConfirmationView(
            self.user,
            "Delete Selected Data",
            f"Delete deletable data for {len(self._pending)} server(s) **and** global personal data?",
        )
        await interaction.response.send_message(view=view, ephemeral=True)
        view.message = await interaction.original_response()
        await view.wait()
        if view.value:
            await self.cog.run_user_delete(interaction.user.id, guild_ids=self._pending, include_global=True)
            await interaction.followup.send("Deletion completed.", ephemeral=True)

    async def _back(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=UserDeleteHub(self.cog, self.user))


class RemovalFeedbackView(PrivateLayoutView):
    REASONS = {
        "deleted_server": "Deleted the Server",
        "confusing": "Confusing",
        "not_expected": "Not what we expected",
    }

    def __init__(self, cog, user, guild_id: int, guild_name: str):
        super().__init__(user, timeout=86400)
        self.cog = cog
        self.guild_id = guild_id
        self.guild_name = guild_name
        self.feedback_done = False
        self.message = None
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("## See you next time!" if not self.feedback_done else "## Thank you for your feedback!"))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(
            f"Would you mind sharing why your server (**{self.guild_name}**) decided to kick Dopamine?"
        ))
        container.add_item(discord.ui.Separator())
        row = discord.ui.ActionRow()
        for key, label in self.REASONS.items():
            btn = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, disabled=self.feedback_done)
            btn.callback = self._make_reason(key)
            row.add_item(btn)
        other = discord.ui.Button(label="Other", style=discord.ButtonStyle.secondary, disabled=self.feedback_done)
        other.callback = self._other
        row.add_item(other)
        container.add_item(row)
        self.add_item(container)

    def _make_reason(self, reason: str):
        async def cb(interaction: discord.Interaction):
            await self.cog.save_removal_feedback(self.guild_id, self.guild_name, interaction.user.id, reason)
            self.feedback_done = True
            self.build_layout()
            await interaction.response.edit_message(view=self)
            self.stop()
        return cb

    async def _other(self, interaction: discord.Interaction):
        await interaction.response.send_modal(RemovalFeedbackModal(self.cog, self.guild_id, self.guild_name, self))

    async def on_timeout(self) -> None:
        try:
            await self.message.delete()
        except Exception:
            pass
        self.stop()


class RemovalFeedbackModal(discord.ui.Modal, title="Feedback"):
    detail = discord.ui.TextInput(label="Tell us more (optional)", required=False, max_length=500)

    def __init__(self, cog, guild_id: int, guild_name: str, parent_view):
        super().__init__()
        self.cog = cog
        self.guild_id = guild_id
        self.guild_name = guild_name
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.save_removal_feedback(
            self.guild_id, self.guild_name, interaction.user.id, "other", self.detail.value or None
        )
        self.parent_view.feedback_done = True
        self.parent_view.build_layout()
        await self.parent_view.message.edit(view=self.parent_view)
        self.parent_view.stop()


class InsightsDashboard(PrivateLayoutView):
    def __init__(self, cog, user):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("## Data Insights"))
        container.add_item(discord.ui.Separator())
        stats = self.cog.cached_insights or {}
        container.add_item(discord.ui.TextDisplay(
            f"**Usage today:** {stats.get('today', 0):,}\n"
            f"**Last 7 days:** {stats.get('week', 0):,}\n"
            f"**Last 30 days:** {stats.get('month', 0):,}\n"
            f"**All time:** {stats.get('all_time', 0):,}\n\n"
            f"**Last backup:** {stats.get('last_backup', 'Never')}\n"
            f"**Bot removal feedback responses:** {stats.get('feedback_count', 0)}"
        ))
        container.add_item(discord.ui.Separator())

        row = discord.ui.ActionRow()
        feat_btn = discord.ui.Button(label="By Feature", style=discord.ButtonStyle.primary)
        cmd_btn = discord.ui.Button(label="Top Commands", style=discord.ButtonStyle.secondary)
        feedback_btn = discord.ui.Button(label="Bot Removal User Feedback", style=discord.ButtonStyle.secondary)

        feat_btn.callback = self._features
        cmd_btn.callback = self._commands
        feedback_btn.callback = self._removal_feedback

        row.add_item(feat_btn)
        row.add_item(cmd_btn)
        row.add_item(feedback_btn)
        container.add_item(row)
        self.add_item(container)

    async def _features(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=InsightsFeaturePage(self.cog, self.user))

    async def _commands(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=InsightsCommandsPage(self.cog, self.user))

    async def _removal_feedback(self, interaction: discord.Interaction):
        view = RemovalFeedbackListPage(self.cog, self.user)
        await view.fetch_data()
        view.build_layout()
        await interaction.response.edit_message(view=view)


class InsightsFeaturePage(PrivateLayoutView):
    per_page = 5

    def __init__(self, cog, user, page: int = 1):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.page = page
        self.rows = cog.cached_feature_stats or []
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("## Usage by Feature"))
        container.add_item(discord.ui.Separator())
        total = max(1, (len(self.rows) + self.per_page - 1) // self.per_page)
        chunk = self.rows[(self.page - 1) * self.per_page:self.page * self.per_page]
        for feat_id, count in chunk:
            container.add_item(discord.ui.TextDisplay(f"### {feat_id}\n**{count:,}** total uses"))
        container.add_item(discord.ui.Separator())
        nav = discord.ui.ActionRow()
        prev_b = discord.ui.Button(emoji="◀️", style=discord.ButtonStyle.primary, disabled=self.page <= 1)
        page_b = discord.ui.Button(label=f"Page {self.page} of {total}", style=discord.ButtonStyle.secondary, disabled=True)
        next_b = discord.ui.Button(emoji="▶️", style=discord.ButtonStyle.primary, disabled=self.page >= total)
        prev_b.callback = self._prev
        next_b.callback = self._next
        nav.add_item(prev_b)
        nav.add_item(page_b)
        nav.add_item(next_b)
        container.add_item(nav)
        back = discord.ui.Button(emoji=self.cog.bot.back_emoji, label="Back", style=discord.ButtonStyle.secondary)
        back.callback = self._back
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(back))
        self.add_item(container)

    async def _prev(self, interaction: discord.Interaction):
        self.page -= 1
        self.build_layout()
        await interaction.response.edit_message(view=self)

    async def _next(self, interaction: discord.Interaction):
        self.page += 1
        self.build_layout()
        await interaction.response.edit_message(view=self)

    async def _back(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=InsightsDashboard(self.cog, self.user))


class InsightsCommandsPage(PrivateLayoutView):
    def __init__(self, cog, user):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.rows = cog.cached_command_stats or []
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("## Top Commands"))
        container.add_item(discord.ui.Separator())
        for i, (cmd, count) in enumerate(self.rows[:15], 1):
            container.add_item(discord.ui.TextDisplay(f"**{i}.** `{cmd}` — {count:,}"))
        back = discord.ui.Button(emoji=self.cog.bot.back_emoji, label="Back", style=discord.ButtonStyle.secondary)
        back.callback = self._back
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(back))
        self.add_item(container)

    async def _back(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=InsightsDashboard(self.cog, self.user))

class RemovalSearchModal(discord.ui.Modal, title="Search Feedback"):
    query_input = discord.ui.TextInput(
        label="Search Keyword",
        placeholder="Server name, reason, or comment...",
        required=False,
        max_length=100,
    )

    def __init__(self, parent_view):
        super().__init__()
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        self.parent_view.search_query = self.query_input.value.strip() or None
        self.parent_view.page = 1
        await self.parent_view.fetch_data()
        self.parent_view.build_layout()
        await interaction.response.edit_message(view=self.parent_view)


class RemovalFeedbackListPage(PrivateLayoutView):
    per_page = 5

    def __init__(
        self,
        cog,
        user,
        page: int = 1,
        sort_by: str = "newest",
        search_query: str | None = None,
    ):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.page = page
        self.sort_by = sort_by
        self.search_query = search_query
        self.rows: list[tuple] = []
        self.total_count = 0

    async def fetch_data(self):
        """Query the removal_feedback SQLite table dynamically based on sort & search."""
        query = "SELECT guild_id, guild_name, responder_user_id, reason, other_text, responded_at FROM removal_feedback"
        params: list[str] = []

        if self.search_query:
            query += " WHERE guild_name LIKE ? OR reason LIKE ? OR other_text LIKE ?"
            like_term = f"%{self.search_query}%"
            params.extend([like_term, like_term, like_term])

        sort_sql_map = {
            "newest": "ORDER BY responded_at DESC",
            "oldest": "ORDER BY responded_at ASC",
            "guild_name": "ORDER BY guild_name ASC",
            "reason": "ORDER BY reason ASC",
        }
        query += f" {sort_sql_map.get(self.sort_by, 'ORDER BY responded_at DESC')}"

        async with self.cog.acquire_db() as db:
            async with db.execute(query, params) as cur:
                self.rows = await cur.fetchall()

        self.total_count = len(self.rows)

    def build_layout(self):
        self.clear_items()
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("## Removal Feedback Responses"))
        container.add_item(discord.ui.Separator())

        status_parts = [f"**Total:** {self.total_count}", f"**Sort:** `{self.sort_by}`"]
        if self.search_query:
            status_parts.append(f"**Search:** `{self.search_query}`")
        container.add_item(discord.ui.TextDisplay(" | ".join(status_parts)))
        container.add_item(discord.ui.Separator())

        total_pages = max(1, (self.total_count + self.per_page - 1) // self.per_page)
        start = (self.page - 1) * self.per_page
        chunk = self.rows[start : start + self.per_page]

        if not chunk:
            container.add_item(discord.ui.TextDisplay("*(No feedback entries found)*"))
            container.add_item(discord.ui.Separator())
        else:
            for gid, gname, uid, reason, other, responded_at in chunk:
                reason_label = RemovalFeedbackView.REASONS.get(reason, reason)
                detail = f"### {gname} (`{gid}`)\n"
                detail += f"- **Reason:** {reason_label}\n"
                if other:
                    detail += f"- **Notes:** {other}\n"
                detail += f"- **Responder:** <@{uid}>\n"
                detail += f"- **Submitted:** <t:{responded_at}:R>"
                container.add_item(discord.ui.TextDisplay(detail))
                container.add_item(discord.ui.Separator())

        sort_select = discord.ui.Select(
            placeholder="Change sorting option...",
            options=[
                discord.SelectOption(
                    label="Newest First", value="newest", default=self.sort_by == "newest"
                ),
                discord.SelectOption(
                    label="Oldest First", value="oldest", default=self.sort_by == "oldest"
                ),
                discord.SelectOption(
                    label="Server Name", value="guild_name", default=self.sort_by == "guild_name"
                ),
                discord.SelectOption(
                    label="Reason", value="reason", default=self.sort_by == "reason"
                ),
            ],
        )
        sort_select.callback = self._on_sort_change
        container.add_item(discord.ui.ActionRow(sort_select))

        filter_row = discord.ui.ActionRow()
        search_btn = discord.ui.Button(
            label="Search Filter", style=discord.ButtonStyle.secondary
        )
        search_btn.callback = self._on_search
        filter_row.add_item(search_btn)

        if self.search_query:
            clear_btn = discord.ui.Button(
                label="Clear Search", style=discord.ButtonStyle.secondary
            )
            clear_btn.callback = self._on_clear_search
            filter_row.add_item(clear_btn)

        container.add_item(filter_row)

        nav_row = discord.ui.ActionRow()
        prev_b = discord.ui.Button(
            emoji="◀️", style=discord.ButtonStyle.primary, disabled=self.page <= 1
        )
        page_b = discord.ui.Button(
            label=f"Page {self.page} of {total_pages}",
            style=discord.ButtonStyle.secondary,
            disabled=True,
        )
        next_b = discord.ui.Button(
            emoji="▶️", style=discord.ButtonStyle.primary, disabled=self.page >= total_pages
        )
        prev_b.callback = self._prev
        next_b.callback = self._next
        nav_row.add_item(prev_b)
        nav_row.add_item(page_b)
        nav_row.add_item(next_b)
        container.add_item(nav_row)

        back_btn = discord.ui.Button(emoji=self.cog.bot.back_emoji, label="Back", style=discord.ButtonStyle.secondary)
        back_btn.callback = self._back
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(back_btn))

        self.add_item(container)

    async def _on_sort_change(self, interaction: discord.Interaction):
        self.sort_by = interaction.data["values"][0]
        self.page = 1
        await self.fetch_data()
        self.build_layout()
        await interaction.response.edit_message(view=self)

    async def _on_search(self, interaction: discord.Interaction):
        await interaction.response.send_modal(RemovalSearchModal(self))

    async def _on_clear_search(self, interaction: discord.Interaction):
        self.search_query = None
        self.page = 1
        await self.fetch_data()
        self.build_layout()
        await interaction.response.edit_message(view=self)

    async def _prev(self, interaction: discord.Interaction):
        self.page -= 1
        self.build_layout()
        await interaction.response.edit_message(view=self)

    async def _next(self, interaction: discord.Interaction):
        self.page += 1
        self.build_layout()
        await interaction.response.edit_message(view=self)

    async def _back(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=InsightsDashboard(self.cog, self.user))
