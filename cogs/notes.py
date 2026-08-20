from __future__ import annotations

import discord
from beacon import ViewPaginator, preconditions, PrivateView
from discord import app_commands
from discord.ext import commands

from utils.data_protocol import DataDeleteResult, DataExportChunk, DataFeatureMeta, DataMonitorResult

note_group = app_commands.Group(name="note", description="Note management commands")


class UndoButtonView(PrivateView):
    def __init__(self, user, cog: Notes, user_id: int, action_type: str, data: dict, interaction: discord.Interaction):
        super().__init__(user, timeout=10.0)
        self.cog = cog
        self.user_id = user_id
        self.action_type = action_type
        self.data = data
        self.interaction = interaction

    async def on_timeout(self):
        try:
            await self.interaction.edit_original_response(view=None)
        except Exception:
            pass

    @discord.ui.button(label="Undo", style=discord.ButtonStyle.secondary, custom_id="undo_action")
    async def undo_button(self, interaction: discord.Interaction, button: discord.ui.Button):

        button.disabled = True
        await interaction.response.edit_message(view=self)

        try:
            if self.action_type == "create":
                note_name = self.data["name"]
                await self.cog.bot.db.execute_write(
                    "DELETE FROM user_notes WHERE user_id = ? AND note_name = ?",
                    (self.user_id, note_name)
                )

                if self.user_id in self.cog.notes_cache:
                    self.cog.notes_cache[self.user_id].pop(note_name, None)

                message = f"Action undone! Note '{note_name}' has been deleted."

            elif self.action_type == "edit":
                old_name = self.data["old_name"]
                new_name = self.data["new_name"]
                old_content = self.data["old_content"]

                if old_name != new_name:
                    await self.cog.bot.db.execute_write(
                        "DELETE FROM user_notes WHERE user_id = ? AND note_name = ?",
                        (self.user_id, new_name)
                    )

                await self.cog.bot.db.execute_write(
                    """
                    INSERT INTO user_notes (user_id, note_name, note_content, updated_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(user_id, note_name) DO UPDATE SET
                        note_content = excluded.note_content,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (self.user_id, old_name, old_content)
                )

                if self.user_id in self.cog.notes_cache:
                    if old_name != new_name:
                        self.cog.notes_cache[self.user_id].pop(new_name, None)
                    self.cog.notes_cache[self.user_id][old_name] = old_content

                message = f"Action undone! Note has been reverted back to '{old_name}'."

            elif self.action_type == "delete":
                note_name = self.data["name"]
                content = self.data["content"]

                await self.cog.bot.db.execute_write(
                    """
                    INSERT INTO user_notes (user_id, note_name, note_content)
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id, note_name) DO UPDATE SET
                        note_content = excluded.note_content
                    """,
                    (self.user_id, note_name, content)
                )

                if self.user_id not in self.cog.notes_cache:
                    self.cog.notes_cache[self.user_id] = {}
                self.cog.notes_cache[self.user_id][note_name] = content

                message = f"Action undone! Note '{note_name}' has been restored."

            await self.interaction.edit_original_response(content=message, embed=None, view=None)
            self.stop()

        except Exception as e:
            await interaction.followup.send(f"Error executing undo: {e}", ephemeral=True)


class Notes(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.notes_cache: dict[int, dict[str, str]] = {}

    async def cog_load(self):
        await self.bot.db.wait_ready()
        await self.populate_caches()

    async def cog_unload(self):
        try:
            self.bot.tree.remove_command(note_group.name)
        except Exception:
            pass

    async def populate_caches(self):
        self.notes_cache.clear()
        rows = await self.bot.db.execute("SELECT user_id, note_name, note_content FROM user_notes")
        for row in rows:
            user_id = row["user_id"]
            name = row["note_name"]
            content = row["note_content"]
            if user_id not in self.notes_cache:
                self.notes_cache[user_id] = {}
            self.notes_cache[user_id][name] = content

    async def check_vote_access(self, user_id: int) -> bool:
        voter_cog = self.bot.get_cog('TopGGVoter')
        return await voter_cog.check_vote_access(user_id) if voter_cog else True

    class NoteEditModal(discord.ui.Modal, title="Edit Note"):
        def __init__(self, cog, old_name: str, old_content: str):
            super().__init__()
            self.cog = cog
            self.old_name = old_name
            self.old_content = old_content

            self.note_name = discord.ui.TextInput(
                label="Note Name",
                default=old_name,
                placeholder="Enter a name for your note...",
                required=True,
                max_length=100
            )
            self.note_content = discord.ui.TextInput(
                label="Note Content",
                default=old_content,
                placeholder="Enter your note content here...",
                required=True,
                style=discord.TextStyle.paragraph,
                max_length=2000
            )

            self.add_item(self.note_name)
            self.add_item(self.note_content)

        async def on_submit(self, interaction: discord.Interaction):
            new_name = self.note_name.value
            new_content = self.note_content.value
            user_id = interaction.user.id

            try:
                await self.cog.bot.db.execute_write(
                    """
                    UPDATE user_notes
                    SET note_name    = ?,
                        note_content = ?,
                        updated_at   = CURRENT_TIMESTAMP
                    WHERE user_id = ?
                      AND note_name = ?
                    """,
                    (new_name, new_content, user_id, self.old_name),
                )

                if self.old_name != new_name:
                    self.cog.notes_cache[user_id].pop(self.old_name, None)

                if user_id not in self.cog.notes_cache:
                    self.cog.notes_cache[user_id] = {}
                self.cog.notes_cache[user_id][new_name] = new_content

                embed = discord.Embed(
                    title=f"{new_name}",
                    description=f"{new_content}",
                    color=discord.Colour.green()
                )
                embed.set_footer(text=f"To see it, use /note get {new_name}.")
                embed.set_author(name="Note Updated Successfully")
                undo_view = UndoButtonView(
                    user=interaction.user,
                    cog=self.cog,
                    user_id=user_id,
                    action_type="edit",
                    data={"old_name": self.old_name, "new_name": new_name, "old_content": self.old_content},
                    interaction=interaction
                )
                await interaction.response.send_message(embed=embed, view=undo_view, ephemeral=True)

            except Exception as e:
                await interaction.response.send_message(f"Error updating note: {e}", ephemeral=True)

    class NoteModal(discord.ui.Modal, title="Create/Update Note"):
        note_name = discord.ui.TextInput(
            label="Note Name",
            placeholder="Enter a name for your note...",
            required=True,
            max_length=100
        )

        note_content = discord.ui.TextInput(
            label="Note Content",
            placeholder="Enter your note content here...",
            required=True,
            style=discord.TextStyle.paragraph,
            max_length=2000
        )

        def __init__(self, cog):
            super().__init__()
            self.cog = cog

        async def on_submit(self, interaction: discord.Interaction):
            name = self.note_name.value
            content = self.note_content.value
            user_id = interaction.user.id

            try:
                await self.cog.bot.db.execute_write(
                    """
                    INSERT INTO user_notes (user_id, note_name, note_content)
                    VALUES (?, ?, ?) ON CONFLICT(user_id, note_name) DO
                    UPDATE SET
                        note_content = excluded.note_content,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (user_id, name, content),
                )

                if user_id not in self.cog.notes_cache:
                    self.cog.notes_cache[user_id] = {}
                self.cog.notes_cache[user_id][name] = content

                embed = discord.Embed(
                    title=name,
                    description=content,
                    color=discord.Color.green()
                )
                embed.set_footer(text=f"To see it, use /note get {name}.")
                embed.set_author(name="Note Created Successfully")
                undo_view = UndoButtonView(
                    user=interaction.user,
                    cog=self.cog,
                    user_id=user_id,
                    action_type="create",
                    data={"name": name},
                    interaction=interaction
                )
                await interaction.response.send_message(embed=embed, view=undo_view, ephemeral=True)

            except Exception as e:
                embed = discord.Embed(
                    title="Error: Failed to Save Note",
                    description=f"An error occurred while saving your note: {str(e)}",
                    color=discord.Color.red()
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)

    def data_features(self) -> list[DataFeatureMeta]:
        return [DataFeatureMeta(
            feature_id="notes",
            name="Notes",
            user_export=True,
            user_delete=True,
        )]

    async def data_export_user(self, user_id: int, *, guild_ids: list[int] | None) -> DataExportChunk:
        chunk = DataExportChunk(feature_id="notes")
        rows = await self.bot.db.execute(
            "SELECT note_name, note_content, created_at, updated_at FROM user_notes WHERE user_id = ?",
            (user_id,),
        )
        if rows:
            chunk.global_data["notes"] = rows
        return chunk

    async def data_export_guild(self, guild_id: int) -> DataExportChunk:
        return DataExportChunk(feature_id="notes")

    async def data_delete_user(self, user_id: int, *, guild_ids: list[int] | None, feature_id: str | None) -> DataDeleteResult:
        if feature_id and feature_id != "notes":
            return DataDeleteResult(feature_id="notes")
        count_rows = await self.bot.db.execute(
            "SELECT COUNT(*) AS cnt FROM user_notes WHERE user_id = ?", (user_id,))
        rows_affected = count_rows[0]["cnt"] if count_rows else 0
        await self.bot.db.execute_write("DELETE FROM user_notes WHERE user_id = ?", (user_id,))
        self.notes_cache.pop(user_id, None)
        return DataDeleteResult(feature_id="notes", deleted=True, rows_affected=rows_affected)

    async def data_delete_guild(self, guild_id: int, feature_id: str | None) -> DataDeleteResult:
        return DataDeleteResult(feature_id="notes")

    async def data_monitor_guild(self, guild: discord.Guild) -> DataMonitorResult:
        return DataMonitorResult(feature_id="notes")

    async def _get_names_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:

        user_notes = self.notes_cache.get(interaction.user.id, {})
        choices = [
            app_commands.Choice(name=name, value=name)
            for name in user_notes.keys()
            if current.lower() in name.lower()
        ]
        return choices[:25]

    @note_group.command(name="create", description="Open the UI to create a note")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def note_create(self, interaction: discord.Interaction):

        if not await self.check_vote_access(interaction.user.id):
            embed = discord.Embed(
                title="Vote to Use This Feature!",
                description=f"This command requires voting! To access this feature, please vote for Dopamine here: [top.gg](https://top.gg/bot/{interaction.client.user.id})",
                color=0xffaa00
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        await interaction.response.send_modal(self.NoteModal(self))

    @note_group.command(name="edit", description="Edit an existing note")
    @app_commands.autocomplete(name=_get_names_autocomplete)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def note_edit(self, interaction: discord.Interaction, name: str):
        if not await self.check_vote_access(interaction.user.id):
            return await interaction.response.send_message("Please vote to use this feature.", ephemeral=True)

        user_id = interaction.user.id
        current_content = self.notes_cache.get(user_id, {}).get(name)

        if current_content is None:
            return await interaction.response.send_message(f"No note found named '{name}'.", ephemeral=True)

        await interaction.response.send_modal(self.NoteEditModal(self, name, current_content))

    @note_group.command(name="get", description="Retrieve a note by name")
    @app_commands.autocomplete(name=_get_names_autocomplete)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def note_fetch(self, interaction: discord.Interaction, name: str):

        if not await self.check_vote_access(interaction.user.id):
            return await interaction.response.send_message("Please vote to use this feature.", ephemeral=True)

        user_id = interaction.user.id
        content = self.notes_cache.get(user_id, {}).get(name)

        if content:
            await interaction.response.send_message(content, ephemeral=True)
        else:
            embed = discord.Embed(
                title="Error: Note Not Found",
                description=f"No note found with the name '{name}'.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @note_group.command(name="list", description="List all of your saved notes")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def note_list(self, interaction: discord.Interaction):
        if not await self.check_vote_access(interaction.user.id):
            return await interaction.response.send_message("Please vote to use this feature.", ephemeral=True)

        user_notes = sorted(self.notes_cache.get(interaction.user.id, {}).keys())

        if not user_notes:
            embed = discord.Embed(
                title="Your Notes",
                description="No notes found. Use `/note create` to create one!",
                color=discord.Color.blurple()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        formatted_notes = [f"- {name}" for name in user_notes]

        view = ViewPaginator(
            title="Your Notes (Use /note get to see content)",
            data=formatted_notes,
            per_page=10,
            color=discord.Color(0x944ae8)
        )

        await interaction.response.send_message(
            embed=view.format_embed(),
            view=view,
            ephemeral=True
        )

    @note_group.command(name="delete", description="Delete a note by name")
    @app_commands.autocomplete(name=_get_names_autocomplete)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def note_delete(self, interaction: discord.Interaction, name: str):

        user_id = interaction.user.id
        user_notes = self.notes_cache.get(user_id, {})

        if name in user_notes:
            try:
                deleted_content = user_notes[name]

                await self.bot.db.execute_write(
                    "DELETE FROM user_notes WHERE user_id = ? AND note_name = ?",
                    (user_id, name),
                )

                del self.notes_cache[user_id][name]

                embed = discord.Embed(
                    title="Note Deleted Successfully",
                    description=f"Note '{name}' has been deleted.",
                    color=discord.Color.green()
                )

                undo_view = UndoButtonView(
                    user=interaction.user,
                    cog=self,
                    user_id=user_id,
                    action_type="delete",
                    data={"name": name, "content": deleted_content},
                    interaction=interaction
                )

                await interaction.response.send_message(embed=embed, view=undo_view, ephemeral=True)

            except Exception as e:
                await interaction.response.send_message(f"Error deleting note: {e}", ephemeral=True)
        else:
            embed = discord.Embed(
                title="Error: Note Not Found",
                description=f"No note found with the name '{name}'.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    bot.tree.add_command(note_group, override=True)
    await bot.add_cog(Notes(bot))
