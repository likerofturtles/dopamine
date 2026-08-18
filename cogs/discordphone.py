import asyncio
import contextlib
import io
import random
import time
from collections import deque

import aiohttp
import discord
import pyvips
from beacon import beacon_commands
from discord import app_commands
from discord.ext import commands

from utils.data_handlers import export_table
from utils.data_protocol import DataDeleteResult, DataExportChunk, DataFeatureMeta, DataMonitorResult

PERM_STORAGE_CHANNEL_ID = 1476933186461106187


class ConnectionPool:
    def __init__(self, bot, path: str, size: int = 5):
        self.bot = bot

    async def init(self):
        await self.bot.db.wait_ready()

    @contextlib.asynccontextmanager
    async def acquire(self):
        async with self.bot.db.acquire_db() as conn:
            yield conn

    async def close(self):
        pass

class CallSession:
    def __init__(self, chan_a, chan_b, user_a, user_b):
        self.chan_a = chan_a
        self.chan_b = chan_b
        self.user_a = user_a
        self.user_b = user_b
        self.history = deque(maxlen=21)
        self.timeout_task = None


def get_ordinal(n: int) -> str:
    """Helper to convert an integer to its ordinal string representation."""
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    return f"{n}" + {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')


class ReportView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    def extract_ids(self, embed: discord.Embed) -> dict:
        """Dynamically fetches target IDs directly from the report embed to avoid memory leaks."""
        data = {}
        for field in embed.fields:
            if field.name == "Reported User ID":
                data['author_id'] = int(field.value)
            elif field.name == "Reported Guild ID":
                data['author_guild_id'] = int(field.value)
            elif field.name == "Reporter User ID":
                data['reporter_id'] = int(field.value)
            elif field.name == "Reporter Guild ID":
                data['reporter_guild_id'] = int(field.value)
        return data

    @discord.ui.button(label="Warn Author", style=discord.ButtonStyle.secondary, custom_id="dp_warn_author")
    async def warn_author(self, interaction: discord.Interaction, button: discord.ui.Button):
        ids = self.extract_ids(interaction.message.embeds[0])
        cog = interaction.client.get_cog("DiscordPhone")
        if cog:
            await cog.increment_stat("users", ids['author_id'], "warned")
            user = interaction.client.get_user(ids['author_id'])
            if user:
                try:
                    await user.send("You have been warned regarding your conduct on Discordphone.")
                except discord.Forbidden:
                    pass
        await interaction.response.send_message("Author warned. (Stats updated and DM attempted)", ephemeral=True)

    @discord.ui.button(label="Ban Author", style=discord.ButtonStyle.danger, custom_id="dp_ban_author")
    async def ban_author(self, interaction: discord.Interaction, button: discord.ui.Button):
        ids = self.extract_ids(interaction.message.embeds[0])
        banning_cog = interaction.client.get_cog("BanningCog")
        if banning_cog:
            await banning_cog.ban_user_api(ids['author_id'])
            await interaction.response.send_message("Author banned from using the bot.", ephemeral=True)
        else:
            await interaction.response.send_message("BanningCog not found!", ephemeral=True)

    @discord.ui.button(label="Ban Author Guild", style=discord.ButtonStyle.danger, custom_id="dp_ban_author_guild")
    async def ban_author_guild(self, interaction: discord.Interaction, button: discord.ui.Button):
        ids = self.extract_ids(interaction.message.embeds[0])
        banning_cog = interaction.client.get_cog("BanningCog")
        if banning_cog:
            await banning_cog.ban_guild_api(ids['author_guild_id'])
            await interaction.response.send_message("Author's Guild has been banned.", ephemeral=True)

    @discord.ui.button(label="Warn Custom User", style=discord.ButtonStyle.primary, custom_id="dp_warn_custom")
    async def warn_custom(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CustomWarnModal())

    @discord.ui.button(label="Warn Reporter", style=discord.ButtonStyle.secondary, custom_id="dp_warn_reporter")
    async def warn_reporter(self, interaction: discord.Interaction, button: discord.ui.Button):
        ids = self.extract_ids(interaction.message.embeds[0])
        cog = interaction.client.get_cog("DiscordPhone")
        if cog:
            await cog.increment_stat("users", ids['reporter_id'], "warned")
            user = interaction.client.get_user(ids['reporter_id'])
            if user:
                try:
                    await user.send("You have been warned regarding your conduct/false reporting on Discordphone.")
                except discord.Forbidden:
                    pass
        await interaction.response.send_message("Reporter warned.", ephemeral=True)

    @discord.ui.button(label="Ban Reporter", style=discord.ButtonStyle.danger, custom_id="dp_ban_reporter")
    async def ban_reporter(self, interaction: discord.Interaction, button: discord.ui.Button):
        ids = self.extract_ids(interaction.message.embeds[0])
        banning_cog = interaction.client.get_cog("BanningCog")
        if banning_cog:
            await banning_cog.ban_user_api(ids['reporter_id'])
            await interaction.response.send_message("Reporter banned from using the bot.", ephemeral=True)

    @discord.ui.button(label="Ban Reporter Guild", style=discord.ButtonStyle.danger, custom_id="dp_ban_reporter_guild")
    async def ban_reporter_guild(self, interaction: discord.Interaction, button: discord.ui.Button):
        ids = self.extract_ids(interaction.message.embeds[0])
        banning_cog = interaction.client.get_cog("BanningCog")
        if banning_cog:
            await banning_cog.ban_guild_api(ids['reporter_guild_id'])
            await interaction.response.send_message("Reporter's Guild has been banned.", ephemeral=True)


class ReportModal(discord.ui.Modal, title='Report Message'):
    reason = discord.ui.TextInput(
        label='Reason for report',
        style=discord.TextStyle.paragraph,
        placeholder="Why are you reporting this message/conversation?"
    )

    def __init__(self, cog, target_msg_data: dict, call_session: CallSession):
        super().__init__()
        self.cog = cog
        self.target_msg_data = target_msg_data
        self.call_session = call_session

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message("Thank you for your report and for keeping the community safe! Your report will be processed by a moderator at Dopamine Studios shortly and appropriate action will be taken.",
                                                ephemeral=True)
        await self.cog.process_report(interaction, self.reason.value, self.target_msg_data, self.call_session)


class DiscordPhone(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.pool = ConnectionPool(bot, DP_PATH, size=5)

        self.users_cache = {}
        self.guilds_cache = {}
        self.settings_cache = {}

        self.waiting_channel = None
        self.waiting_user = None
        self.active_calls = {}
        self.message_map = {}
        self.queue = []
        self.active_calls = {}
        self.message_map = {}

        self.skip_cooldowns = {}
        self.last_partner = {}

        self.report_ctx_menu = app_commands.ContextMenu(
            name="Report DiscordPhone Message",
            callback=self.report_context
        )
        self.bot.tree.add_command(self.report_ctx_menu)

    async def cog_load(self):
        self.bot.loop.create_task(self.init_db())
        self.bot.add_view(ReportView())

    async def cog_unload(self):

        for call in self.active_calls.values():
            if call.timeout_task:
                call.timeout_task.cancel()

        await self.pool.close()

    async def init_db(self):
        await self.pool.init()
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY, reported INTEGER DEFAULT 0, created INTEGER DEFAULT 0, warned INTEGER DEFAULT 0
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS guilds (
                    id INTEGER PRIMARY KEY, reported INTEGER DEFAULT 0, created INTEGER DEFAULT 0, warned INTEGER DEFAULT 0
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY, value TEXT
                )
            """)
            await conn.commit()

            async with conn.execute("SELECT id, reported, created, warned FROM users") as cursor:
                async for row in cursor:
                    self.users_cache[row[0]] = {"reported": row[1], "created": row[2], "warned": row[3]}

            async with conn.execute("SELECT id, reported, created, warned FROM guilds") as cursor:
                async for row in cursor:
                    self.guilds_cache[row[0]] = {"reported": row[1], "created": row[2], "warned": row[3]}

            async with conn.execute("SELECT key, value FROM settings") as cursor:
                async for row in cursor:
                    if row[0] == "log_channel":
                        self.settings_cache["log_channel"] = int(row[1])

    async def try_match(self, channel, user, interaction: discord.Interaction | commands.Context):
        rules_str = "[DiscordPhone Rules](<https://docs.google.com/document/d/1ZuoKDQCrLMcY72PLW9kzTM7a1sS0y6mzyF_eNwV3low/edit?tab=t.0>)"
        tos_str = "[Terms of Service](<https://docs.google.com/document/d/1kUC1P9aRNAwJD-HiP5v8HO-xowRiUGEbOZ-DfvOR2Tk/edit?usp=sharing>)"
        if isinstance(interaction, discord.Interaction):
            await interaction.response.send_message(
                    f"<a:loading:1475121732108025929> You have successfully joined the queue! Waiting for another user...\nTo leave the queue, use `!!hangup` or `/discordphone hangup`.\n-# By continuing, you agree to the {rules_str} and {tos_str}. If you don't agree, stop using the bot.",
                    )
        else:
            await interaction.send(
                f"<a:loading:1475121732108025929> You have successfully joined the queue! Waiting for another user...\nTo leave the queue, use `!!hangup` or `/discordphone hangup`.\n-# By continuing, you agree to the {rules_str} and {tos_str}. If you don't agree, stop using the bot.",
            )
        valid_partners = [
            (c_id, u_obj) for c_id, u_obj in self.queue
            if u_obj.id != self.last_partner.get(user.id) and u_obj.id != user.id
        ]

        if valid_partners:
            partner_chan_id, partner_user = random.choice(valid_partners)

            self.queue = [(c, u) for c, u in self.queue if u.id != partner_user.id]

            chan_a = self.bot.get_channel(partner_chan_id) or await self.bot.fetch_channel(partner_chan_id)
            chan_b = channel

            call = CallSession(chan_a.id, chan_b.id, partner_user, user)
            self.active_calls[chan_a.id] = call
            self.active_calls[chan_b.id] = call
            call.timeout_task = self.bot.loop.create_task(self.timeout_handler(call))

            safe_msg = f"Connected! Say hi to the people on the other side!\n\nYou can report problematic users by:\n* Click on three dots -> Apps -> Report DiscordPhone Message.\n* Or reply to the problematic message with `!!report your-reason-here`."

            await chan_a.send(f"{partner_user.mention} {safe_msg}")
            await chan_b.send(f"{user.mention} {safe_msg}")

            log_chan_id = self.settings_cache.get("log_channel")
            log_channel = self.bot.get_channel(log_chan_id) if log_chan_id else None
            if log_channel:
                await log_channel.send(
                    f" [CONNECT] Connect channel {chan_b.name} from {chan_b.guild.name} to {chan_a.name} from {chan_a.guild.name}")
            return True


        return False

    async def increment_stat(self, table: str, id_: int, field: str):
        cache = self.users_cache if table == "users" else self.guilds_cache
        if id_ not in cache:
            cache[id_] = {"reported": 0, "created": 0, "warned": 0}

        cache[id_][field] += 1

        self.bot.loop.create_task(self._db_write(table, id_, field, cache[id_][field]))

    async def _db_write(self, table: str, id_: int, field: str, new_val: int):
        async with self.pool.acquire() as conn:
            await conn.execute(f"""
                INSERT INTO {table} (id, reported, created, warned)
                VALUES (?, 0, 0, 0)
                ON CONFLICT(id) DO UPDATE SET {field} = ?
            """, (id_, new_val))
            await conn.commit()

    async def download_image(self, url: str) -> bytes or None:
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        return await resp.read()
            except Exception as e:
                print(f"Error downloading image for compression: {e}")
        return None

    def compress_image(self, data: bytes) -> io.BytesIO:
        try:
            image = pyvips.Image.new_from_buffer(data, "")

            max_dim = 1000
            scale = min(max_dim / image.width, max_dim / image.height)

            if scale < 1:
                image = image.resize(scale)

            buffer = image.write_to_buffer(".jpg[Q=60,optimize_coding=True,strip=True]")

            return io.BytesIO(buffer)
        except Exception as e:
            print(f"Pyvips compression error: {e}")
            return io.BytesIO(data)

    async def get_webhook(self, channel: discord.TextChannel) -> discord.Webhook:
        webhooks = await channel.webhooks()
        for wh in webhooks:
            if wh.name == "Dopamine":
                return wh
        return await channel.create_webhook(name="Dopamine")

    async def timeout_handler(self, call: CallSession):
        await asyncio.sleep(1800)
        await self.end_call(call, "☎️ Call disconnected due to 30 minutes of inactivity.")

    async def end_call(self, call: CallSession, reason: str):
        if call.timeout_task:
            call.timeout_task.cancel()

        self.active_calls.pop(call.chan_a, None)
        self.active_calls.pop(call.chan_b, None)

        chan_a = self.bot.get_channel(call.chan_a) or await self.bot.fetch_channel(call.chan_a)
        chan_b = self.bot.get_channel(call.chan_b) or await self.bot.fetch_channel(call.chan_b)

        if chan_a: await chan_a.send(reason)
        if chan_b: await chan_b.send(reason)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.channel.id not in self.active_calls:
            return

        prefix = await self.bot.get_prefix(message)
        if isinstance(prefix, str):
            if message.content.startswith(prefix):
                return
        elif isinstance(prefix, list):
            if any(message.content.startswith(p) for p in prefix):
                return

        call = self.active_calls[message.channel.id]
        peer_channel_id = call.chan_b if call.chan_a == message.channel.id else call.chan_a
        peer_channel = self.bot.get_channel(peer_channel_id) or await self.bot.fetch_channel(peer_channel_id)
        if not peer_channel:
            return

        if call.timeout_task:
            call.timeout_task.cancel()
        call.timeout_task = self.bot.loop.create_task(self.timeout_handler(call))

        content = discord.utils.escape_mentions(message.content)

        for sticker in message.stickers:
            content += f"\n{sticker.url}"

        embeds = []
        if message.reference and message.reference.resolved:
            ref = message.reference.resolved
            if isinstance(ref, discord.Message):
                emb = discord.Embed(description=ref.content)
                emb.set_author(
                    name=f"Replying to {ref.author.display_name}",
                    icon_url=ref.author.display_avatar.url if ref.author.display_avatar else None
                )

                if ref.attachments:
                    image_att = next(
                        (a for a in ref.attachments if a.content_type and a.content_type.startswith('image')), None)
                    if image_att:
                        emb.set_image(url=image_att.url)

                elif ref.embeds:
                    image_embed = next((e for e in ref.embeds if e.image or e.thumbnail), None)
                    if image_embed:
                        img_url = image_embed.image.url if image_embed.image else image_embed.thumbnail.url
                        emb.set_image(url=img_url)

                embeds.append(emb)

        files_to_forward = []
        attachments_to_process = []
        for att in message.attachments:
            is_image = att.content_type and att.content_type.startswith("image")

            att_data = await att.read()
            files_to_forward.append(discord.File(io.BytesIO(att_data), filename=att.filename))

            if is_image:
                attachments_to_process.append((att.filename, att_data))

        wh = await self.get_webhook(peer_channel)
        if not wh:
            return

        sent_msg = await wh.send(
            content=content,
            username=message.author.display_name,
            avatar_url=message.author.display_avatar.url if message.author.display_avatar else None,
            embeds=embeds,
            files=files_to_forward,
            allowed_mentions=discord.AllowedMentions.none(),
            wait=True
        )

        async def process_and_store_history():
            compressed_images = []

            for filename, data in attachments_to_process:
                loop = asyncio.get_running_loop()
                compressed_io = await loop.run_in_executor(None, self.compress_image, data)
                if compressed_io:
                    new_filename = f"cmp_{filename.rsplit('.', 1)[0]}.jpg"
                    compressed_images.append((new_filename, compressed_io.getvalue()))

            for sticker in message.stickers:
                sticker_data = await self.download_image(sticker.url)
                if sticker_data:
                    loop = asyncio.get_running_loop()
                    compressed_io = await loop.run_in_executor(None, self.compress_image, sticker_data)
                    if compressed_io:
                        compressed_images.append((f"sticker_{sticker.id}.jpg", compressed_io.getvalue()))

            msg_data = {
                "content": content,
                "author_name": message.author.display_name,
                "author_id": message.author.id,
                "guild_id": message.guild.id,
                "timestamp": message.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                "compressed_attachments": compressed_images
            }
            call.history.append(msg_data)
            self.message_map[sent_msg.id] = msg_data

        self.bot.loop.create_task(process_and_store_history())

    @commands.Cog.listener()
    async def on_typing(self, channel: discord.abc.Messageable, user: discord.User, when):
        if user.bot:
            return

        if channel.id not in self.active_calls:
            return

        call = self.active_calls[channel.id]

        peer_channel_id = call.chan_b if call.chan_a == channel.id else call.chan_a
        peer_channel = self.bot.get_channel(peer_channel_id) or await self.bot.fetch_channel(peer_channel_id)

        if peer_channel:
            try:
                await peer_channel.typing()
            except discord.Forbidden:
                pass

    dp_group = beacon_commands.Group(name="discordphone", description="Discordphone core commands")

    @dp_group.command(name="start", description="Start a DiscordPhone call")
    async def start(self, interaction: discord.Interaction):
        if interaction.channel.id in self.active_calls:
            return await interaction.response.send_message("This channel is already in a call!", ephemeral=True)

        if any(c_id == interaction.channel.id for c_id, _ in self.queue):
            return await interaction.response.send_message("This channel is already in the matchmaking queue!", ephemeral=True)

        log_chan_id = self.settings_cache.get("log_channel")
        log_channel = self.bot.get_channel(log_chan_id) or await self.bot.fetch_channel(log_chan_id) if log_chan_id else None
        if log_channel:
            await log_channel.send(
                f" [QUEUE] Channel {interaction.channel.name} from {interaction.guild.name} joined the queue.")

        matched = await self.try_match(interaction.channel, interaction.user, interaction)
        if not matched:
            self.queue.append((interaction.channel.id, interaction.user))

    @dp_group.command(name="skip", description="Skip the current user")
    async def skip(self, interaction: discord.Interaction):
        now = time.time()
        user_skips = self.skip_cooldowns.get(interaction.user.id, [])
        user_skips = [t for t in user_skips if now - t < 300]

        if len(user_skips) >= 2:
            return await interaction.response.send_message("You're skipping too fast! Please wait a few minutes.",
                                                           ephemeral=True)

        if interaction.channel.id not in self.active_calls:
            return await interaction.response.send_message("You are not in a call.", ephemeral=True)

        call = self.active_calls[interaction.channel.id]

        user_a = call.user_a
        user_b = call.user_b
        chan_a_id = call.chan_a
        chan_b_id = call.chan_b

        self.last_partner[user_a.id] = user_b.id
        self.last_partner[user_b.id] = user_a.id

        user_skips.append(now)
        self.skip_cooldowns[interaction.user.id] = user_skips

        await interaction.response.send_message("Skipping this user...")
        await self.end_call(call, f"{interaction.author.display_name} has skipped the call.\n\n<a:loading:1475121732108025929> Re-joining queue...\n-# To leave the queue, use `!!hangup` or `/discordphone hangup`.")

        for c_id, u_obj in [(chan_a_id, user_a), (chan_b_id, user_b)]:
            chan = self.bot.get_channel(c_id) or await self.bot.fetch_channel(c_id)
            if not await self.try_match(chan, u_obj):
                self.queue.append((c_id, u_obj))
    @dp_group.command(name="hangup", description="Hangup the active DiscordPhone call")
    async def hangup(self, interaction: discord.Interaction):
        if self.waiting_channel and self.waiting_channel.id == interaction.channel.id:
            self.waiting_channel = None
            self.waiting_user = None
            return await interaction.response.send_message("Removed from queue.", ephemeral=False)

        if interaction.channel.id not in self.active_calls:
            return await interaction.response.send_message("You are not currently in a call.", ephemeral=True)

        call = self.active_calls[interaction.channel.id]
        await interaction.response.send_message("Hanging up...")
        await self.end_call(call, f"Call disconnected by {interaction.user.display_name}.")

    @commands.command(name="report")
    async def report_prefix(self, ctx: commands.Context, *, reason: str = None):
        if ctx.channel.id not in self.active_calls:
            return await ctx.send("No active call to report.")
        if reason is None:
            return await ctx.send("You need to provide a reason for the report!")
        if not ctx.message.reference:
            return await ctx.send("Please **reply** to the specific message you want to report.")

        call = self.active_calls[ctx.channel.id]
        replied_msg_id = ctx.message.reference.message_id
        msg_data = self.message_map.get(replied_msg_id)

        if not msg_data:
            return await ctx.send("Could not find the data for that message. Are you trying to report a message that is too old, or a message sent by a person in your own server? This command only supports reporting messages sent by the opposite side.", delete_after=30)

        if msg_data['author_id'] == ctx.author.id:
            return await ctx.send("You cannot report yourself.")

        await self.process_report(ctx, reason, msg_data, call)
        await ctx.send("Thank you for your report and for keeping the community safe! Your report will be processed by a moderator at Dopamine Studios shortly and appropriate action will be taken.", delete_after=10)

    async def report_context(self, interaction: discord.Interaction, message: discord.Message):
        if interaction.channel.id not in self.active_calls:
            return await interaction.response.send_message("No active call.", ephemeral=True)

        call = self.active_calls[interaction.channel.id]

        msg_data = self.message_map.get(message.id)

        if not msg_data:
            msg_data = next((m for m in reversed(call.history) if m.get('content') == message.content), None)

        if not msg_data or msg_data['author_id'] == interaction.user.id:
            return await interaction.response.send_message(
                "You can't report yourself! If you believe this is an error or if you *really badly* want to report yourself, please join the [support server](<https://discord.gg/Ztm9pKc8GM>).", ephemeral=True)

        await interaction.response.send_modal(ReportModal(self, msg_data, call))

    async def process_report(self, interaction, reason: str, target_msg: dict, session: CallSession):
        if isinstance(interaction, discord.Interaction):
            reporter_id = interaction.user.id
            reporter_guild_id = interaction.guild.id
        else:
            reporter_id = interaction.author.id
            reporter_guild_id = interaction.guild.id

        author_id = target_msg['author_id']
        author_guild_id = target_msg['guild_id']

        await self.increment_stat("users", reporter_id, "created")
        await self.increment_stat("guilds", reporter_guild_id, "created")
        await self.increment_stat("users", author_id, "reported")
        await self.increment_stat("guilds", author_guild_id, "reported")

        author_reported = self.users_cache[author_id]["reported"]
        ordinal = get_ordinal(author_reported)

        author_guild = self.bot.get_guild(author_guild_id) or await self.bot.fetch_guild(author_guild_id)
        reporter = self.bot.get_user(reporter_id) or await self.bot.fetch_user(reporter_id)
        reporter_guild = self.bot.get_guild(reporter_guild_id) or await self.bot.fetch_guild(reporter_guild_id)
        embed = discord.Embed(
            title=f"{target_msg['author_name']} from {author_guild.name} has been reported for the #{ordinal} time. Report made by {reporter.display_name} from {reporter_guild}.",
            color=discord.Color.red(),
            description=(
                f"### ➤ Reason for Report: {reason}\n"
                f"**Warns for Author:** {self.users_cache[author_id]['warned']}\n"
                f"**Reports created by Reporter:** {self.users_cache[reporter_id]['created']}\n"
                f"**Warns for Reporter:** {self.users_cache[reporter_id]['warned']}\n\n"
            )
        )
        embed.add_field(name="Reported User ID", value=str(author_id), inline=True)
        embed.add_field(name="Reported Guild ID", value=str(author_guild_id), inline=True)
        embed.add_field(name="Reporter User ID", value=str(reporter_id), inline=True)
        embed.add_field(name="Reporter Guild ID", value=str(reporter_guild_id), inline=True)

        chat_log_content = ""
        for m in session.history:
            chat_log_content += f"[{m['timestamp']}] {m['author_name']} ({m['author_id']}) from {m['guild_id']}: {m['content']}\n"

        file = discord.File(io.BytesIO(chat_log_content.encode('utf-8')), filename="report_context.txt")

        perm_storage_chan = self.bot.get_channel(PERM_STORAGE_CHANNEL_ID)
        attachments_field_content = ""

        if perm_storage_chan:
            current_batch_files = []

            storage_header = f"Report Case Image Evidence\n**Reported User: {author_id} (Reporter: {reporter_id})**\n"

            for m in session.history:
                if "compressed_attachments" in m and m["compressed_attachments"]:
                    author_mention = f"<@{m['author_id']}>"

                    for filename, img_bytes in m["compressed_attachments"]:
                        discord_file = discord.File(io.BytesIO(img_bytes), filename=filename)
                        current_batch_files.append(discord_file)

                        if len(current_batch_files) == 10:
                            storage_msg = await perm_storage_chan.send(content=storage_header,
                                                                       files=current_batch_files)
                            for i, att in enumerate(storage_msg.attachments):
                                attachments_field_content += f"[{att.filename}]({storage_msg.jump_url}) (sent by {author_mention})\n"
                            current_batch_files = []
                            storage_header = ""

            if current_batch_files:
                storage_msg = await perm_storage_chan.send(content=storage_header, files=current_batch_files)
                for att in storage_msg.attachments:
                    attachments_field_content += f"[{att.filename}]({storage_msg.jump_url}) (sent by {author_mention})\n"
        else:
            print(f"Error: Permanent storage channel {PERM_STORAGE_CHANNEL_ID} not found.")
            attachments_field_content = "Error: Could not access permanent storage channel."

        if attachments_field_content:
            embed.add_field(name="Attachments (Last 21 Msgs)", value=attachments_field_content,
                            inline=False)
        elif perm_storage_chan:
            embed.add_field(name="Attachments", value="No images.",
                            inline=False)

        log_chan_id = self.settings_cache.get("log_channel")
        if log_chan_id:
            log_chan = self.bot.get_channel(log_chan_id)
            if log_chan:
                await log_chan.send(embed=embed, file=file, view=ReportView())

    @beacon_commands.command(name="zt", description=".", permissions_preset="bot_owner")
    async def zt_command(self, interaction: discord.Interaction):
        self.settings_cache["log_channel"] = interaction.channel.id

        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
                ("log_channel", str(interaction.channel.id), str(interaction.channel.id)))
            await conn.commit()

        await interaction.response.send_message("Log and Reports channel has been set to this channel.", ephemeral=True)

    @commands.command(name="start")
    async def start_prefix(self, ctx: commands.Context):
        if ctx.channel.id in self.active_calls:
            return await ctx.send("This channel is already in a call!")

        if any(c_id == ctx.channel.id for c_id, _ in self.queue):
            return await ctx.send("This channel is already in the matchmaking queue!")
        log_chan_id = self.settings_cache.get("log_channel")
        log_channel = self.bot.get_channel(log_chan_id) or await self.bot.fetch_channel(log_chan_id) if log_chan_id else None
        if log_channel:
            await log_channel.send(f" [QUEUE] Channel {ctx.channel.name} from {ctx.guild.name} joined the queue.")
        matched = await self.try_match(ctx.channel, ctx.author, ctx)
        if not matched:
            self.queue.append((ctx.channel.id, ctx.author))


    @commands.command(name="skip")
    async def skip_prefix(self, ctx: commands.Context):
        now = time.time()
        user_skips = self.skip_cooldowns.get(ctx.author.id, [])
        user_skips = [t for t in user_skips if now - t < 300]

        if len(user_skips) >= 2:
            return await ctx.send("You're skipping too fast! Please wait a few minutes.")

        if ctx.channel.id not in self.active_calls:
            return await ctx.send("You are not in a call.")

        call = self.active_calls[ctx.channel.id]

        self.last_partner[call.user_a.id] = call.user_b.id
        self.last_partner[call.user_b.id] = call.user_a.id
        user_skips.append(now)
        self.skip_cooldowns[ctx.author.id] = user_skips

        await ctx.send("Skipping this user...")
        await self.end_call(call,
                            f"{ctx.author.display_name} has skipped the call.\n\n<a:loading:1475121732108025929> Re-joining queue...\n-# To leave the queue, use `!!hangup` or `/discordphone hangup`.")

        for c_id, u_obj in [(call.chan_a, call.user_a), (call.chan_b, call.user_b)]:
            chan = self.bot.get_channel(c_id) or await self.bot.fetch_channel(c_id)
            if not await self.try_match(chan, u_obj):
                self.queue.append((c_id, u_obj))

    @commands.command(name="hangup")
    async def hangup_prefix(self, ctx: commands.Context):
        if any(c_id == ctx.channel.id for c_id, _ in self.queue):
            self.queue = [(c_id, u_obj) for c_id, u_obj in self.queue if c_id != ctx.channel.id]
            return await ctx.send("Removed from queue.")

        if ctx.channel.id not in self.active_calls:
            return await ctx.send("You are not currently in a call.")

        call = self.active_calls[ctx.channel.id]
        await ctx.send("Hanging up...")
        await self.end_call(call, f"Call disconnected by {ctx.author.display_name}.")


    def data_features(self) -> list[DataFeatureMeta]:
        return [DataFeatureMeta(
            feature_id="discordphone",
            name="DiscordPhone",
            user_export=True,
            user_delete=True,
            guild_export=True,
            guild_delete=True,
        )]

    async def data_export_user(self, user_id: int, *, guild_ids: list[int] | None) -> DataExportChunk:
        chunk = DataExportChunk(feature_id="discordphone")
        async with self.pool.acquire() as conn:
            rows = await export_table(conn, "SELECT * FROM users WHERE id = ?", (user_id,))
        if rows:
            chunk.global_data["user"] = rows[0]
        return chunk

    async def data_export_guild(self, guild_id: int) -> DataExportChunk:
        chunk = DataExportChunk(feature_id="discordphone")
        async with self.pool.acquire() as conn:
            guild_rows = await export_table(conn, "SELECT * FROM guilds WHERE id = ?", (guild_id,))
        chunk.guild_data[guild_id] = {
            "guild": guild_rows[0] if guild_rows else None,
        }
        return chunk

    async def data_delete_user(self, user_id: int, *, guild_ids: list[int] | None, feature_id: str | None) -> DataDeleteResult:
        if feature_id and feature_id != "discordphone":
            return DataDeleteResult(feature_id="discordphone")
        async with self.pool.acquire() as conn:
            cur = await conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            await conn.commit()
            rows_affected = cur.rowcount
        self.users_cache.pop(user_id, None)
        return DataDeleteResult(feature_id="discordphone", deleted=True, rows_affected=rows_affected)

    async def data_delete_guild(self, guild_id: int, feature_id: str | None) -> DataDeleteResult:
        if feature_id and feature_id != "discordphone":
            return DataDeleteResult(feature_id="discordphone")
        async with self.pool.acquire() as conn:
            cur = await conn.execute("DELETE FROM guilds WHERE id = ?", (guild_id,))
            await conn.commit()
            rows_affected = cur.rowcount
        self.guilds_cache.pop(guild_id, None)
        return DataDeleteResult(feature_id="discordphone", deleted=True, rows_affected=rows_affected)

    async def data_monitor_guild(self, guild: discord.Guild) -> DataMonitorResult:
        return DataMonitorResult(feature_id="discordphone")


class CustomWarnModal(discord.ui.Modal, title='Warn Custom User'):
    user_id = discord.ui.TextInput(label='User ID', placeholder='Paste the Discord User ID here...')
    reason = discord.ui.TextInput(label='Reason/Warning Message', style=discord.TextStyle.paragraph)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            target_id = int(self.user_id.value)
            cog = interaction.client.get_cog("DiscordPhone")
            if cog:
                await cog.increment_stat("users", target_id, "warned")
                user = interaction.client.get_user(target_id) or await interaction.client.fetch_user(target_id)
                if user:
                    await user.send(f"Admin Warning: {self.reason.value}")
                    await interaction.response.send_message(f"Warned <@{target_id}> and logged stat.", ephemeral=True)
                else:
                    await interaction.response.send_message("User not found, but stat was updated.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)

async def setup(bot):
    await bot.add_cog(DiscordPhone(bot))