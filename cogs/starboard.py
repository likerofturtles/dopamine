import asyncio
import time
from collections import deque
from typing import Optional, Dict

import discord
from beacon import PrivateLayoutView, beacon_commands
from discord import app_commands
from discord.ext import commands, tasks

from utils.data_handlers import export_table
from utils.data_protocol import DataDeleteResult, DataExportChunk, DataFeatureMeta, DataMonitorResult
from utils.discord_health import is_access_error, report_access_failure


class ThresholdModal(discord.ui.Modal, title="Edit Star Threshold"):
    def __init__(self, view: 'StarboardDashboard'):
        super().__init__()
        self.view = view
        self.threshold_input = discord.ui.TextInput(
            label="Star Threshold",
            placeholder="Enter a number (min 1)",
            min_length=1,
            max_length=3,
            required=True
        )
        self.add_item(self.threshold_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            val = int(self.threshold_input.value)
            if val < 1:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message("Please enter a valid number greater than 0.",
                                                           ephemeral=True)

        await self.view.cog.update_guild_setting(interaction.guild.id, star_threshold=val)

        self.view.build_layout()
        await interaction.response.edit_message(view=self.view)


class StarboardDashboard(PrivateLayoutView):
    def __init__(self, user, cog, guild_id):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.build_layout()

    def build_layout(self):
        self.clear_items()

        settings = self.cog.settings_cache.get(self.guild_id, {})
        is_enabled = bool(settings.get("enabled", 0))
        current_channel_id = settings.get("starboard_channel_id")
        current_threshold = settings.get("star_threshold", 3)

        channel_mention = f"<#{current_channel_id}>" if current_channel_id else "Not Set"

        container = discord.ui.Container()

        toggle_style = discord.ButtonStyle.secondary if is_enabled else discord.ButtonStyle.primary
        toggle_label = "Disable" if is_enabled else "Enable"
        toggle_btn = discord.ui.Button(label=toggle_label, style=toggle_style)
        toggle_btn.callback = self.toggle_callback

        container.add_item(discord.ui.Section(discord.ui.TextDisplay("## Starboard Dashboard"), accessory=toggle_btn))

        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(
            "A starboard is like a Hall Of Fame for Discord messages. Users can react to a message with a ⭐️ and once it reaches the set threshold, Dopamine will post a copy of it in the channel you choose."))

        if is_enabled:
            container.add_item(discord.ui.TextDisplay(
                f"* **Current Channel:** {channel_mention}\n* **Current Threshold:** {current_threshold}"))
            container.add_item(discord.ui.Separator())

            threshold_btn = discord.ui.Button(label="Edit Threshold", style=discord.ButtonStyle.primary)
            threshold_btn.callback = self.threshold_callback

            channel_btn = discord.ui.Button(label="Edit Channel", style=discord.ButtonStyle.secondary)
            channel_btn.callback = self.channel_edit_callback

            row = discord.ui.ActionRow()
            row.add_item(threshold_btn)
            row.add_item(channel_btn)
            container.add_item(row)

        self.add_item(container)

    async def toggle_callback(self, interaction: discord.Interaction):
        settings = self.cog.settings_cache.get(self.guild_id, {})
        current_state = bool(settings.get("enabled", 0))
        current_channel = settings.get("starboard_channel_id")

        if not current_state and not current_channel:
            await self.cog.update_guild_setting(self.guild_id, enabled=1)

            view = ChannelSelectView(self, self.user, self.cog, self.guild_id, interaction)
            return await interaction.response.edit_message(view=view)

        new_state = 0 if current_state else 1
        await self.cog.update_guild_setting(self.guild_id, enabled=new_state)

        self.build_layout()
        await interaction.response.edit_message(view=self)

    async def threshold_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ThresholdModal(self))

    async def channel_edit_callback(self, interaction: discord.Interaction):
        view = ChannelSelectView(self, self.user, self.cog, self.guild_id, interaction)
        await interaction.response.edit_message(view=view)


class ChannelSelectView(PrivateLayoutView):
    def __init__(self, view: 'StarboardDashboard', user, cog, guild_id, parent_interaction: discord.Interaction):
        super().__init__(user, timeout=None)
        self.cog = cog
        self.view = view
        self.guild_id = guild_id
        self.parent_interaction = parent_interaction
        self.build_layout()

    def build_layout(self):
        container = discord.ui.Container()

        select = discord.ui.ChannelSelect(
            placeholder="Select a channel...",
            channel_types=[discord.ChannelType.text],
            min_values=1, max_values=1
        )
        select.callback = self.select_callback

        row = discord.ui.ActionRow()
        row.add_item(select)

        container.add_item(discord.ui.TextDisplay("### Select a Channel"))
        container.add_item(discord.ui.TextDisplay("Choose the channel where you want the starboard to appear:"))
        container.add_item(row)
        self.add_item(container)

    async def select_callback(self, interaction: discord.Interaction):
        selected_channel = interaction.data['values'][0]

        await self.cog.update_guild_setting(self.guild_id, starboard_channel_id=int(selected_channel))

        self.view.build_layout()
        await self.parent_interaction.edit_original_response(view=self.view)


class StarboardCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.STAR_EMOJI = "⭐"

        self.settings_cache: Dict[int, dict] = {}
        self.star_posts_cache: Dict[int, Dict[int, int]] = {}

        self.starred_messages: deque[int] = deque(maxlen=10000)
        self.lfg_creators: dict[int, int] = {}
        self.guild_cooldowns: dict[int, float] = {}
        self.lfg_message_times: dict[int, float] = {}

        self._max_lfg_entries: int = 5000
        self._starboard_tasks: Dict[int, asyncio.Task] = {}

    async def cog_load(self):
        await self.bot.db.wait_ready()
        await self.populate_caches()
        if not self._cache_cleanup.is_running():
            self._cache_cleanup.start()

    async def cog_unload(self):
        self._cache_cleanup.cancel()

        for task in self._starboard_tasks.values():
            if not task.done():
                task.cancel()

        if self._starboard_tasks:
            await asyncio.gather(*self._starboard_tasks.values(), return_exceptions=True)

    async def populate_caches(self):
        """Load all data from DB into memory."""
        self.settings_cache.clear()
        self.star_posts_cache.clear()

        rows = await self.bot.db.execute("SELECT * FROM starboard_guild_settings")
        for data in rows:
            self.settings_cache[data["guild_id"]] = data

        rows = await self.bot.db.execute(
            "SELECT guild_id, source_message_id, starboard_message_id FROM star_posts")
        for row in rows:
            gid, src_id, sb_id = row["guild_id"], row["source_message_id"], row["starboard_message_id"]
            if gid not in self.star_posts_cache:
                self.star_posts_cache[gid] = {}
            self.star_posts_cache[gid][src_id] = sb_id

    async def get_guild_settings(self, guild_id: int) -> dict:
        """Fetch settings from cache, or create in DB and cache if missing."""
        if guild_id in self.settings_cache:
            return self.settings_cache[guild_id]

        await self.bot.db.execute(
            "INSERT OR IGNORE INTO starboard_guild_settings (guild_id, enabled) VALUES (?, 0)",
            (guild_id,)
        )

        rows = await self.bot.db.execute(
            "SELECT * FROM starboard_guild_settings WHERE guild_id = ?", (guild_id,))
        data = rows[0]

        self.settings_cache[guild_id] = data
        return data

    async def update_guild_setting(self, guild_id: int, **kwargs):
        """Update both DB and cache manually (Write-Through)."""
        if not kwargs:
            return

        settings = await self.get_guild_settings(guild_id)
        settings.update(kwargs)

        set_clause = ", ".join(f"{key} = ?" for key in kwargs.keys())
        values = list(kwargs.values()) + [guild_id]

        await self.bot.db.execute(
            f"UPDATE starboard_guild_settings SET {set_clause} WHERE guild_id = ?", values)

    def get_star_emoji(self, count: int) -> str:
        if count >= 15:
            return "✨"
        elif count >= 10:
            return "💫"
        elif count >= 5:
            return "🌟"
        else:
            return "⭐"

    async def upsert_star_post(self, guild_id: int, source_id: int, starboard_id: int):
        """Update both DB and cache manually for star posts."""
        if guild_id not in self.star_posts_cache:
            self.star_posts_cache[guild_id] = {}
        self.star_posts_cache[guild_id][source_id] = starboard_id

        await self.bot.db.execute("""
                             INSERT INTO star_posts (guild_id, source_message_id, starboard_message_id)
                             VALUES (?, ?, ?) ON CONFLICT(guild_id, source_message_id) DO
                             UPDATE SET
                                 starboard_message_id = excluded.starboard_message_id
                             """, (guild_id, source_id, starboard_id))

    async def delete_star_post(self, guild_id: int, source_id: int):
        """Remove from both DB and cache manually."""
        if guild_id in self.star_posts_cache:
            self.star_posts_cache[guild_id].pop(source_id, None)

        await self.bot.db.execute(
            "DELETE FROM star_posts WHERE guild_id = ? AND source_message_id = ?",
            (guild_id, source_id)
        )

    def get_star_post(self, guild_id: int, source_id: int) -> Optional[int]:
        """Pure cache read for performance."""
        return self.star_posts_cache.get(guild_id, {}).get(source_id)

    def get_source_from_starboard(self, guild_id: int, starboard_message_id: int) -> Optional[int]:
        """Reverse lookup in cache to find source ID from starboard ID."""
        if guild_id not in self.star_posts_cache:
            return None
        for src_id, sb_id in self.star_posts_cache[guild_id].items():
            if sb_id == starboard_message_id:
                return src_id
        return None

    @tasks.loop(minutes=5)
    async def _cache_cleanup(self):
        """Standard LFG/Cooldown cleanup (Settings/Star cache persist)."""
        await self.bot.db.wait_ready()
        current_time = time.time()

        max_age = 24 * 60 * 60
        to_remove_lfg = [m for m, t in self.lfg_message_times.items() if current_time - t > max_age]
        for m in to_remove_lfg:
            self.lfg_creators.pop(m, None)
            self.lfg_message_times.pop(m, None)

        to_remove_cd = [k for k, v in self.guild_cooldowns.items() if current_time - v > 600]
        for k in to_remove_cd:
            self.guild_cooldowns.pop(k, None)

    def build_starboard_embed(self, message: discord.Message) -> discord.Embed:
        text = message.content.strip() if message.content else ""
        embed = discord.Embed(description=text, color=discord.Color.gold())
        embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
        embed.add_field(name="Jump to Message", value=f"[Click Here]({message.jump_url})", inline=False)
        embed.timestamp = message.created_at

        image_url = None
        for att in message.attachments:
            if att.content_type and att.content_type.startswith("image/"):
                image_url = att.url
                break
        if not image_url:
            for e in message.embeds:
                if e.image and e.image.url: image_url = e.image.url; break
                if e.thumbnail and e.thumbnail.url: image_url = e.thumbnail.url; break
                if e.type == "image" and e.url: image_url = e.url; break
        if image_url:
            embed.set_image(url=image_url)
        return embed

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction: discord.Reaction, user: discord.User):
        if user.bot or str(reaction.emoji) != self.STAR_EMOJI:
            return

        message = reaction.message
        if not message.guild or message.id not in self.lfg_creators:
            return

        settings = await self.get_guild_settings(message.guild.id)
        count = reaction.count - (1 if reaction.me else 0)

        if count >= settings["lfg_threshold"]:
            reactors = [u async for u in reaction.users() if not u.bot]
            mentions = " ".join(u.mention for u in reactors) or "Threshold reached!"
            creator_id = self.lfg_creators.pop(message.id, None)
            self.lfg_message_times.pop(message.id, None)

            creator = message.guild.get_member(creator_id) or await message.guild.fetch_member(creator_id)
            embed = discord.Embed(
                title="LFG Group Ready!",
                description=f"**Created by:** {creator.mention if creator else 'Unknown User'}",
                color=discord.Color.green()
            )
            try:
                await message.channel.send(content=mentions, embed=embed)
            except Exception as e:
                if is_access_error(e):
                    await report_access_failure(self.bot, message.guild.id, "starboard", "lfg")

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id != self.bot.user.id and str(payload.emoji) == self.STAR_EMOJI:
            self._schedule_starboard_update(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if str(payload.emoji) == self.STAR_EMOJI:
            self._schedule_starboard_update(payload)

    def _schedule_starboard_update(self, payload: discord.RawReactionActionEvent):
        mid = payload.message_id
        if mid in self._starboard_tasks and not self._starboard_tasks[mid].done():
            self._starboard_tasks[mid].cancel()
        self._starboard_tasks[mid] = self.bot.loop.create_task(self._process_starboard_payload(payload))

    async def _process_starboard_payload(self, payload: discord.RawReactionActionEvent):
        try:
            guild = self.bot.get_guild(payload.guild_id) or await self.bot.fetch_guild(payload.guild_id)
            if not guild: return

            settings = await self.get_guild_settings(guild.id)

            if not settings.get("enabled", 0):
                return

            sb_id = settings.get("starboard_channel_id")
            if not sb_id: return

            source_id_from_sb = self.get_source_from_starboard(guild.id, payload.message_id)

            if source_id_from_sb:
                source_msg_id = source_id_from_sb
                sb_chan = guild.get_channel(payload.channel_id) or await guild.fetch_channel(payload.channel_id)
                sb_msg = await sb_chan.fetch_message(payload.message_id)

                try:
                    url = sb_msg.embeds[0].fields[0].value.split("(")[1].split(")")[0]
                    parts = url.split("/")
                    source_channel_id = int(parts[-2])
                except (IndexError, ValueError):
                    return
            else:
                if payload.channel_id == sb_id:
                    return
                source_msg_id = payload.message_id
                source_channel_id = payload.channel_id

            try:
                src_chan = guild.get_channel(source_channel_id) or await guild.fetch_channel(source_channel_id)
                msg = await src_chan.fetch_message(source_msg_id)
            except discord.NotFound:
                return


            star_react_source = next((r for r in msg.reactions if str(r.emoji) == self.STAR_EMOJI), None)
            count_source = star_react_source.count if star_react_source else 0

            existing_id = self.get_star_post(guild.id, msg.id)
            count_sb = 0
            sbc = guild.get_channel(sb_id)
            if not sbc:
                try:
                    sbc = await guild.fetch_channel(sb_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                    if is_access_error(e):
                        await report_access_failure(self.bot, guild.id, "starboard", str(sb_id))
                    return

            if existing_id:
                try:
                    sbm = await sbc.fetch_message(existing_id)
                    star_react_sb = next((r for r in sbm.reactions if str(r.emoji) == self.STAR_EMOJI), None)
                    if star_react_sb:
                        count_sb = star_react_sb.count
                except discord.NotFound:
                    await self.delete_star_post(guild.id, msg.id)
                    existing_id = None
                except:
                    pass

            total_count = count_source + count_sb

            if total_count < settings["star_threshold"]:
                if existing_id:
                    try:
                        sbm = await sbc.fetch_message(existing_id)
                        await sbm.delete()
                    except:
                        pass
                    await self.delete_star_post(guild.id, msg.id)
                return

            embed = self.build_starboard_embed(msg)
            dynamic_emoji = self.get_star_emoji(total_count)
            content_str = f"{dynamic_emoji} **{total_count}** | {msg.channel.mention}"

            try:
                if existing_id:
                    try:
                        sbm = await sbc.fetch_message(existing_id)
                        await sbm.edit(content=content_str, embed=embed)
                    except discord.NotFound:
                        new_sbm = await sbc.send(content=content_str, embed=embed)
                        await self.upsert_star_post(guild.id, msg.id, new_sbm.id)
                else:
                    new_sbm = await sbc.send(content=content_str, embed=embed)
                    await self.upsert_star_post(guild.id, msg.id, new_sbm.id)
            except Exception as e:
                if is_access_error(e):
                    await report_access_failure(self.bot, guild.id, "starboard", str(sb_id))

        finally:
            self._starboard_tasks.pop(payload.message_id, None)

    @commands.Cog.listener()
    async def on_raw_reaction_clear(self, payload: discord.RawReactionClearEvent):
        existing = self.get_star_post(payload.guild_id, payload.message_id)
        if not existing: return

        try:
            settings = await self.get_guild_settings(payload.guild_id)
            sbc = self.bot.get_channel(settings["starboard_channel_id"]) or await self.bot.fetch_channel(settings["starboard_channel_id"])
            sbm = await sbc.fetch_message(existing)
            await sbm.delete()
        except:
            pass
        await self.delete_star_post(payload.guild_id, payload.message_id)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if not after.guild: return
        existing = self.get_star_post(after.guild.id, after.id)
        if not existing: return

        settings = await self.get_guild_settings(after.guild.id)
        star_react = next((r for r in after.reactions if str(r.emoji) == self.STAR_EMOJI), None)
        count_source = star_react.count if star_react else 0

        try:
            sbc = after.guild.get_channel(settings["starboard_channel_id"])
            sbm = await sbc.fetch_message(existing)

            star_react_sb = next((r for r in sbm.reactions if str(r.emoji) == self.STAR_EMOJI), None)
            count_sb = star_react_sb.count if star_react_sb else 0

            total = count_source + count_sb

            embed = self.build_starboard_embed(after)
            dynamic_emoji = self.get_star_emoji(total)
            content_str = f"{dynamic_emoji} {total} | {after.channel.mention}"
            await sbm.edit(content=content_str, embed=embed)
        except:
            pass

    @beacon_commands.command(name="starboard", description="Configure the Starboard via Dashboard", permissions_preset="automation")
    async def starboard_dashboard(self, interaction: discord.Interaction):
        await self.get_guild_settings(interaction.guild.id)
        view = StarboardDashboard(interaction.user, self, interaction.guild.id)
        await interaction.response.send_message(view=view)

    lfg_group = app_commands.Group(name="lfg", description="LFG commands")

    @commands.command(name="teststarboard")
    async def teststarboard(self, ctx: commands.Context):
        if ctx.author.id != 758576879715483719 or not ctx.message.reference: return

        ref = await ctx.channel.fetch_message(ctx.message.reference.message_id)
        settings = await self.get_guild_settings(ctx.guild.id)
        sb_id = settings.get("starboard_channel_id")
        if not sb_id: return

        star_react = next((r for r in ref.reactions if str(r.emoji) == self.STAR_EMOJI), None)
        count = star_react.count if star_react else 0

        embed = self.build_starboard_embed(ref)
        content_str = f"⭐️ {count} in {ref.channel.mention}"

        channel = self.bot.get_channel(sb_id) or await self.bot.fetch_channel(sb_id)
        await channel.send(content=content_str, embed=embed)

    def data_features(self) -> list[DataFeatureMeta]:
        return [DataFeatureMeta(
            feature_id="starboard",
            name="Starboard",
            guild_export=True,
            guild_delete=True,
        )]

    async def data_export_user(self, user_id: int, *, guild_ids: list[int] | None) -> DataExportChunk:
        return DataExportChunk(feature_id="starboard")

    async def data_export_guild(self, guild_id: int) -> DataExportChunk:
        chunk = DataExportChunk(feature_id="starboard")
        settings = await export_table(
            self.bot.db, "SELECT * FROM starboard_guild_settings WHERE guild_id = ?", (guild_id,))
        posts = await export_table(
            self.bot.db, "SELECT * FROM star_posts WHERE guild_id = ?", (guild_id,))
        chunk.guild_data[guild_id] = {"settings": settings, "star_posts": posts}
        return chunk

    async def data_delete_user(self, user_id: int, *, guild_ids: list[int] | None, feature_id: str | None) -> DataDeleteResult:
        return DataDeleteResult(feature_id="starboard")

    async def data_delete_guild(self, guild_id: int, feature_id: str | None) -> DataDeleteResult:
        if feature_id and feature_id != "starboard":
            return DataDeleteResult(feature_id="starboard")

        async with self.bot.db.acquire_db() as db:
            res1 = await db.execute("DELETE FROM star_posts WHERE guild_id = ?", (guild_id,))
            res2 = await db.execute("DELETE FROM starboard_guild_settings WHERE guild_id = ?", (guild_id,))
            await db.commit()

        rows_affected = (res1 if isinstance(res1, int) else 0) + (res2 if isinstance(res2, int) else 0)

        self.settings_cache.pop(guild_id, None)
        self.star_posts_cache.pop(guild_id, None)
        return DataDeleteResult(feature_id="starboard", deleted=True, rows_affected=rows_affected)

    async def _board_channel_accessible(self, guild: discord.Guild, channel_id: int | None) -> bool:
        if not channel_id:
            return True
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return False
        if not isinstance(channel, discord.abc.GuildChannel) or channel.guild.id != guild.id:
            return False
        perms = channel.permissions_for(guild.me)
        return perms.view_channel and perms.send_messages and perms.embed_links

    async def data_monitor_guild(self, guild: discord.Guild) -> DataMonitorResult:
        result = DataMonitorResult(feature_id="starboard")
        settings = self.settings_cache.get(guild.id)
        if not settings or not settings.get("enabled"):
            return result
        channel_id = settings.get("starboard_channel_id")
        if await self._board_channel_accessible(guild, channel_id):
            return result
        await self.update_guild_setting(guild.id, enabled=0)
        result.actions.append("disabled_starboard")
        return result


async def setup(bot):
    await bot.add_cog(StarboardCog(bot))