from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import time
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

import discord
from beacon import beacon_commands, PrivateLayoutView
from discord.ext import commands, tasks

from VERSION import bot_version
from cogs.data_views import (
    DataHome,
    ExportQueuedView,
    InsightsDashboard,
    RemovalFeedbackView,
)
from utils.data_export_md import payload_to_markdown
from utils.data_handlers import (
    delete_usage_guild,
    delete_usage_user,
    export_usage_guild,
    export_usage_user,
)
from utils.data_protocol import (
    COG_NAME_BY_FEATURE,
    COMMAND_PREFIX_TO_FEATURE,
    EXPORT_COOLDOWN_SECONDS,
    EXPORT_DEBOUNCE_SECONDS,
    GUILD_RETENTION_DAYS,
    DataDeleteResult,
    DataExportChunk,
    DataFeatureMeta,
    DataMonitorResult,
)


class Data(commands.Cog):
    """Data management, usage analytics, and health monitoring."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cached_insights: dict[str, Any] = {}
        self.cached_feature_stats: list[tuple[str, int]] = []
        self.cached_command_stats: list[tuple[str, int]] = []
        self._initial_health_done = False

    async def cog_load(self):
        await self.bot.db.wait_ready()
        await self._init_db()
        if not self.export_worker.is_running():
            self.export_worker.start()
        if not self.health_monitor.is_running():
            self.health_monitor.start()
        if not self.retention_purge.is_running():
            self.retention_purge.start()
        if not self._initial_health_done:
            self.bot.loop.create_task(self._initial_health_pass())

    async def cog_unload(self):
        self.export_worker.cancel()
        self.health_monitor.cancel()
        self.retention_purge.cancel()

    async def _init_db(self):
        await self.bot.db.execute(
            "UPDATE export_queue SET status='pending', started_at=NULL WHERE status='processing'"
        )

    def iter_data_cogs(self):
        seen = set()
        for cog in list(self.bot.cogs.values()):
            if hasattr(cog, "data_features") and id(cog) not in seen:
                seen.add(id(cog))
                yield cog

    def get_all_features(self) -> list[DataFeatureMeta]:
        features: list[DataFeatureMeta] = []
        seen: set[str] = set()
        for cog in self.iter_data_cogs():
            for feat in cog.data_features():
                if feat.feature_id not in seen:
                    seen.add(feat.feature_id)
                    features.append(feat)
        return features

    def get_features_for_scope(self, scope: str) -> list[DataFeatureMeta]:
        feats = self.get_all_features()
        if scope == "user":
            return [f for f in feats if f.user_export or f.user_delete]
        return [f for f in feats if f.guild_export or f.guild_delete]

    def _get_cog_for_feature(self, feature_id: str):
        if feature_id == "usage":
            return self
        name = COG_NAME_BY_FEATURE.get(feature_id)
        return self.bot.get_cog(name) if name else None

    async def record_usage(
        self,
        feature_id: str,
        user_id: int,
        guild_id: Optional[int],
        command_name: Optional[str] = None,
    ):
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        await self.bot.db.execute(
            """INSERT INTO usage_daily (date, user_id, guild_id, feature_id, command_name, count)
               VALUES (?, ?, ?, ?, ?, 1)
               ON CONFLICT(date, user_id, guild_id, feature_id, command_name)
               DO UPDATE SET count = count + 1""",
            (date, user_id, guild_id, feature_id, command_name),
        )

    def resolve_feature_from_command(self, qualified_name: str) -> str:
        root = qualified_name.split()[0].split(":")[0] if qualified_name else "unknown"
        return COMMAND_PREFIX_TO_FEATURE.get(root, root)

    @commands.Cog.listener()
    async def on_app_command_completion(self, interaction: discord.Interaction, command):
        if interaction.user.bot:
            return
        feature_id = self.resolve_feature_from_command(command.qualified_name)
        guild_id = interaction.guild.id if interaction.guild else None
        await self.record_usage(
            feature_id, interaction.user.id, guild_id, command.qualified_name
        )

    def data_features(self) -> list[DataFeatureMeta]:
        return [DataFeatureMeta(
            feature_id="usage", name="Usage Analytics",
            user_export=True, user_delete=True, guild_export=True, guild_delete=True,
        )]

    async def data_export_user(self, user_id: int, *, guild_ids: list[int] | None) -> DataExportChunk:
        return await export_usage_user(self.bot.db, user_id, guild_ids)

    async def data_export_guild(self, guild_id: int) -> DataExportChunk:
        return await export_usage_guild(self.bot.db, guild_id)

    async def data_delete_user(
        self, user_id: int, *, guild_ids: list[int] | None, feature_id: str | None
    ) -> DataDeleteResult:
        return await delete_usage_user(self.bot.db, user_id, guild_ids)

    async def data_delete_guild(self, guild_id: int, feature_id: str | None) -> DataDeleteResult:
        return await delete_usage_guild(self.bot.db, guild_id)

    async def data_monitor_guild(self, guild: discord.Guild) -> DataMonitorResult:
        return DataMonitorResult(feature_id="usage")

    async def _check_rate_limit(
        self, requester_id: int, scope: str, guild_id: Optional[int]
    ) -> Optional[int]:
        rows = await self.bot.db.execute(
            """SELECT last_export_at FROM export_rate_limits
               WHERE requester_user_id=? AND scope=? AND
               (guild_id IS ? OR (guild_id IS NULL AND ? IS NULL))""",
            (requester_id, scope, guild_id, guild_id),
        )
        if rows and time.time() - rows[0]["last_export_at"] < EXPORT_COOLDOWN_SECONDS:
            return int(rows[0]["last_export_at"] + EXPORT_COOLDOWN_SECONDS)
        return None

    async def _set_rate_limit(self, requester_id: int, scope: str, guild_id: Optional[int]):
        now = int(time.time())
        await self.bot.db.execute(
            """INSERT INTO export_rate_limits (requester_user_id, scope, guild_id, last_export_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(requester_user_id, scope, guild_id) DO UPDATE SET last_export_at=excluded.last_export_at""",
            (requester_id, scope, guild_id, now),
        )

    async def queue_export(
        self,
        interaction: discord.Interaction,
        scope: str,
        feature_id: Optional[str] = None,
    ):
        rate_scope = "guild" if scope in ("guild", "feature_guild") else "user"
        gid = interaction.guild.id if rate_scope == "guild" and interaction.guild else None
        retry = await self._check_rate_limit(interaction.user.id, rate_scope, gid)
        if retry:
            return await interaction.response.send_message(
                f"You can request another export <t:{retry}:R>.", ephemeral=True
            )
        now = int(time.time())
        subject = interaction.user.id if rate_scope == "user" else None
        export_guild = gid
        await self.bot.db.execute(
            """INSERT INTO export_queue
               (requester_user_id, scope, subject_user_id, guild_id, feature_id, status, created_at, process_after)
               VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (
                interaction.user.id,
                scope if not feature_id else f"{scope}",
                subject,
                export_guild,
                feature_id,
                now,
                now + EXPORT_DEBOUNCE_SECONDS,
            ),
        )
        await interaction.response.edit_message(
            view=ExportQueuedView(
                self,
                interaction.user,
                scope,
                "Your data export has been queued. You'll receive it in your DMs within a few minutes.",
            )
        )

    async def _build_export_files(self, payload: dict, tmp: Path) -> list[Path]:
        json_path = tmp / "raw_dopamine_export.json"
        md_path = tmp / "dopamine_export.md"
        json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        md_path.write_text(payload_to_markdown(payload), encoding="utf-8")
        return [md_path, json_path]

    async def _zip_export_files(self, files: list[Path], zip_path: Path) -> None:
        def _zip():
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in files:
                    zf.write(f, f.name)

        await asyncio.to_thread(_zip)

    async def _build_export_payload(self, job: dict) -> dict:
        scope = job["scope"]
        feature_filter = job.get("feature_id")
        payload: dict[str, Any] = {
            "export_meta": {
                "about": "Dopamine Discord Bot by Dopamine Studios - Data Export",
                "bot_version": bot_version,
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "scope": scope,
            },
            "global": {},
            "guilds": {},
        }
        user_id = job.get("subject_user_id")
        guild_id = job.get("guild_id")
        guild_ids = json.loads(job["guild_ids_json"]) if job.get("guild_ids_json") else None

        cogs = [self._get_cog_for_feature(feature_filter)] if feature_filter else list(self.iter_data_cogs())
        if self not in cogs and (not feature_filter or feature_filter == "usage"):
            cogs.insert(0, self)

        for cog in cogs:
            if cog is None or not hasattr(cog, "data_features"):
                continue
            for feat in cog.data_features():
                if feature_filter and feat.feature_id != feature_filter:
                    continue
                if scope in ("user", "feature_user") and user_id is not None and feat.user_export:
                    chunk = await cog.data_export_user(user_id, guild_ids=guild_ids)
                    self._merge_chunk(payload, chunk)
                elif scope in ("guild", "feature_guild") and guild_id is not None and feat.guild_export:
                    chunk = await cog.data_export_guild(guild_id)
                    self._merge_chunk(payload, chunk, guild_id=guild_id)

        if user_id:
            payload["export_meta"]["subject_user_id"] = user_id
        if guild_id:
            payload["export_meta"]["guild_id"] = guild_id
            guild = self.bot.get_guild(guild_id)
            if guild:
                payload["export_meta"]["guild_name"] = guild.name
        return payload

    def _merge_chunk(self, payload: dict, chunk: DataExportChunk, guild_id: Optional[int] = None):
        fid = chunk.feature_id
        if chunk.global_data:
            payload["global"][fid] = chunk.global_data
        for gid, data in chunk.guild_data.items():
            gkey = str(gid)
            payload["guilds"].setdefault(gkey, {"guild_name": None})
            payload["guilds"][gkey][fid] = data
            if guild_id and not payload["guilds"][gkey].get("guild_name"):
                g = self.bot.get_guild(gid)
                payload["guilds"][gkey]["guild_name"] = g.name if g else str(gid)

    async def _process_export_job(self, job_id: int):
        job_rows = await self.bot.db.execute("SELECT * FROM export_queue WHERE id=?", (job_id,))
        if not job_rows:
            return
        job = job_rows[0]
        await self.bot.db.execute(
            "UPDATE export_queue SET status='processing', started_at=? WHERE id=?",
            (int(time.time()), job_id),
        )

        try:
            payload = await self._build_export_payload(job)
            tmp = Path(tempfile.mkdtemp(prefix="dopamine_export_"))
            files = await self._build_export_files(payload, tmp)
            zip_path = tmp / f"dopamine_export_{job_id}.zip"
            await self._zip_export_files(files, zip_path)
            user = await self.bot.fetch_user(job["requester_user_id"])
            container = discord.ui.Container()
            container.add_item(discord.ui.TextDisplay("## Your Data Export"))
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay(
                "Attached is your requested data export from Dopamine. "
                "Open **dopamine_export.md** for a readable summary, or **raw_dopamine_export.json** for the full raw data."
            ))
            container.add_item(discord.ui.File(media=f"attachment://{zip_path.name}"))
            view = PrivateLayoutView(user, timeout=None)
            view.add_item(container)
            await user.send(view=view, file=discord.File(zip_path, filename=zip_path.name))
            shutil.rmtree(zip_path.parent, ignore_errors=True)
            rate_scope = "guild" if job.get("guild_id") else "user"
            await self._set_rate_limit(job["requester_user_id"], rate_scope, job.get("guild_id"))
            await self.bot.db.execute(
                "UPDATE export_queue SET status='completed', completed_at=? WHERE id=?",
                (int(time.time()), job_id),
            )
        except discord.Forbidden:
            await self.bot.db.execute(
                "UPDATE export_queue SET status='failed', error='dm_closed', completed_at=? WHERE id=?",
                (int(time.time()), job_id),
            )
        except Exception as e:
            await self.bot.db.execute(
                "UPDATE export_queue SET status='failed', error=?, completed_at=? WHERE id=?",
                (str(e)[:500], int(time.time()), job_id),
            )

    @tasks.loop(seconds=30)
    async def export_worker(self):
        await self.bot.db.wait_ready()
        await self.bot.wait_until_ready()
        now = int(time.time())
        rows = await self.bot.db.execute(
            """SELECT id FROM export_queue WHERE status='pending' AND process_after <= ?
               ORDER BY created_at LIMIT 1""",
            (now,),
        )
        if rows:
            await self._process_export_job(rows[0]["id"])

    @export_worker.before_loop
    async def _before_export(self):
        await self.bot.wait_until_ready()

    async def discover_user_guilds(self, user_id: int) -> list[int]:
        guild_ids: set[int] = set()
        for cog in self.iter_data_cogs():
            if cog is self:
                continue
            chunk = await cog.data_export_user(user_id, guild_ids=None)
            guild_ids.update(chunk.guild_data.keys())
        return sorted(guild_ids)

    async def run_user_delete(
        self,
        user_id: int,
        *,
        guild_ids: list[int] | None = None,
        feature_id: str | None = None,
        include_global: bool = True,
    ):
        global_features = {"afk", "notes", "topgg", "alerts", "usage"}
        targets = [self._get_cog_for_feature(feature_id)] if feature_id else list(self.iter_data_cogs())
        if not feature_id:
            targets.insert(0, self)
        for cog in targets:
            if cog is None or not hasattr(cog, "data_delete_user"):
                continue
            for feat in cog.data_features():
                if feature_id and feat.feature_id != feature_id:
                    continue
                if not feat.user_delete:
                    continue
                if feat.feature_id in global_features:
                    if not include_global and guild_ids is not None:
                        continue
                    await cog.data_delete_user(user_id, guild_ids=None, feature_id=feature_id)
                else:
                    await cog.data_delete_user(
                        user_id, guild_ids=guild_ids, feature_id=feature_id
                    )

    async def run_guild_delete(self, guild_id: int, feature_id: str | None = None):
        targets = [self._get_cog_for_feature(feature_id)] if feature_id else list(self.iter_data_cogs())
        if not feature_id:
            targets.insert(0, self)
        for cog in targets:
            if cog is None or not hasattr(cog, "data_delete_guild"):
                continue
            for feat in cog.data_features():
                if feature_id and feat.feature_id != feature_id:
                    continue
                if not feat.guild_delete:
                    continue
                await cog.data_delete_guild(guild_id, feature_id)

    async def on_feature_access_failure(self, guild_id: int, feature_id: str, detail: str = ""):
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            try:
                guild = await self.bot.fetch_guild(guild_id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                guild = None
        cog = self._get_cog_for_feature(feature_id)
        if cog is None or not hasattr(cog, "data_monitor_guild"):
            return
        try:
            if guild is not None:
                result = await cog.data_monitor_guild(guild)
            else:
                result = await cog.data_monitor_guild_offline(guild_id) if hasattr(cog, "data_monitor_guild_offline") else None
                if result is None:
                    return
            if result.actions:
                for action in result.actions:
                    await self.bot.db.execute(
                        """INSERT INTO monitor_log (guild_id, feature_id, action, detail, created_at)
                           VALUES (?, ?, ?, ?, ?)""",
                        (guild_id, feature_id, action, detail[:200], int(time.time())),
                    )
        except Exception:
            pass

    async def _monitor_guild(self, guild: discord.Guild):
        if not self.bot.is_ready():
            return
        for cog in self.iter_data_cogs():
            if cog is self or not hasattr(cog, "data_monitor_guild"):
                continue
            try:
                result = await cog.data_monitor_guild(guild)
                if result.actions:
                    for action in result.actions:
                        await self.bot.db.execute(
                            """INSERT INTO monitor_log (guild_id, feature_id, action, detail, created_at)
                               VALUES (?, ?, ?, ?, ?)""",
                            (guild.id, result.feature_id, action, "", int(time.time())),
                        )
            except Exception:
                pass

    async def _initial_health_pass(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(15)
        self._initial_health_done = True
        for guild in list(self.bot.guilds):
            await self._monitor_guild(guild)

    @tasks.loop(hours=1)
    async def health_monitor(self):
        await self.bot.db.wait_ready()
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            await self._monitor_guild(guild)

    @tasks.loop(hours=24)
    async def retention_purge(self):
        await self.bot.db.wait_ready()
        await self.bot.wait_until_ready()
        cutoff = int(time.time()) - GUILD_RETENTION_DAYS * 86400
        rows = await self.bot.db.execute(
            "SELECT guild_id, guild_name FROM guild_removal_schedule WHERE removed_at <= ?",
            (cutoff,),
        )
        for row in rows:
            guild_id = row["guild_id"]
            guild_name = row["guild_name"]
            if self.bot.get_guild(guild_id) is not None:
                await self.bot.db.execute(
                    "DELETE FROM guild_removal_schedule WHERE guild_id = ?", (guild_id,)
                )
                continue
            await self.run_guild_delete(guild_id)
            await self.bot.db.execute(
                "DELETE FROM guild_removal_schedule WHERE guild_id = ?", (guild_id,)
            )

    @retention_purge.before_loop
    async def _before_retention(self):
        await self.bot.wait_until_ready()

    async def save_removal_feedback(
        self,
        guild_id: int,
        guild_name: str,
        user_id: int,
        reason: str,
        other_text: Optional[str] = None,
    ):
        await self.bot.db.execute(
            """INSERT INTO removal_feedback
               (guild_id, guild_name, responder_user_id, reason, other_text, responded_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (guild_id, guild_name, user_id, reason, other_text, int(time.time())),
        )

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        inviter_id = guild.owner_id
        try:
            async for entry in guild.audit_logs(limit=10, action=discord.AuditLogAction.bot_add):
                if entry.target.id == self.bot.user.id:
                    inviter_id = entry.user.id
                    break
        except (discord.Forbidden, discord.HTTPException):
            pass
        await self.bot.db.execute(
            """INSERT INTO guild_inviters (guild_id, inviter_user_id, guild_name, joined_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET
               inviter_user_id=excluded.inviter_user_id, guild_name=excluded.guild_name,
               joined_at=excluded.joined_at""",
            (guild.id, inviter_id, guild.name, int(time.time())),
        )
        await self.bot.db.execute("DELETE FROM guild_removal_schedule WHERE guild_id = ?", (guild.id,))

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        inviter_id = guild.owner_id
        rows = await self.bot.db.execute(
            "SELECT inviter_user_id FROM guild_inviters WHERE guild_id=?", (guild.id,)
        )
        if rows:
            inviter_id = rows[0]["inviter_user_id"]
        await self.bot.db.execute(
            """INSERT INTO guild_removal_schedule (guild_id, guild_name, removed_at)
               VALUES (?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET removed_at=excluded.removed_at, guild_name=excluded.guild_name""",
            (guild.id, guild.name, int(time.time())),
        )
        try:
            user = await self.bot.fetch_user(inviter_id)
            view = RemovalFeedbackView(self, user, guild.id, guild.name)
            msg = await user.send(view=view)
            view.message = msg
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            pass
        for cog in self.iter_data_cogs():
            if hasattr(cog, "data_monitor_guild"):
                try:
                    await cog.data_monitor_guild(guild)
                except Exception:
                    pass

    async def refresh_insights_cache(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        month_ago = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")

        async def _sum(since: Optional[str]) -> int:
            if since:
                rows = await self.bot.db.execute(
                    "SELECT COALESCE(SUM(count),0) AS s FROM usage_daily WHERE date >= ?", (since,)
                )
            else:
                rows = await self.bot.db.execute("SELECT COALESCE(SUM(count),0) AS s FROM usage_daily")
            return int(rows[0]["s"]) if rows else 0

        self.cached_insights = {
            "today": await _sum(today),
            "week": await _sum(week_ago),
            "month": await _sum(month_ago),
            "all_time": await _sum(None),
        }
        self.cached_feature_stats = [
            (row["feature_id"], int(row["c"]))
            for row in await self.bot.db.execute(
                """SELECT feature_id, SUM(count) AS c FROM usage_daily
                   GROUP BY feature_id ORDER BY c DESC LIMIT 50"""
            )
        ]
        cmd_rows = await self.bot.db.execute(
            """SELECT COALESCE(command_name, 'event') AS n, SUM(count) AS c FROM usage_daily
               WHERE command_name IS NOT NULL
               GROUP BY command_name ORDER BY c DESC LIMIT 25"""
        )
        self.cached_command_stats = [(r["n"], int(r["c"])) for r in cmd_rows]

        fb_rows = await self.bot.db.execute("SELECT COUNT(*) AS cnt FROM removal_feedback")
        self.cached_insights["feedback_count"] = int(fb_rows[0]["cnt"]) if fb_rows else 0

    @beacon_commands.command(name="data", description="Manage your data and privacy settings.")
    async def data_cmd(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message(
                "Use this command in a server.", ephemeral=True
            )
        await interaction.response.send_message(view=DataHome(self, interaction.user))

    @beacon_commands.command(name="di", description=".", permissions_preset="bot_owner")
    async def di_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.refresh_insights_cache()
        await interaction.edit_original_response(view=InsightsDashboard(self, interaction.user))


async def setup(bot: commands.Bot):
    await bot.add_cog(Data(bot))
