import asyncio
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional
import turso, turso.sync

class AsyncExecuteWrapper:
    def __init__(self, db_manager, sql, params):
        self.db_manager = db_manager
        self.sql = sql
        self.params = params
        self.cursor = None

    async def __aenter__(self):
        await self.db_manager.wait_ready()
        def _exec():
            cur = self.db_manager.conn.cursor()
            cur.execute(self.sql, self.params)
            return cur
        self.cursor = await asyncio.to_thread(_exec)
        return self.cursor

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None and self.db_manager.conn:
            await asyncio.to_thread(self.db_manager.conn.commit)

    def __await__(self):
        async def _awaitable():
            await self.db_manager.wait_ready()
            async with self.db_manager._lock:
                def _exec():
                    cur = self.db_manager.conn.cursor()
                    cur.execute(self.sql, self.params)
                    if cur.description:
                        rows = cur.fetchall()
                        return [self.db_manager._dict_factory(cur, r) for r in rows]
                    self.db_manager.conn.commit()
                    return cur.rowcount
                return await asyncio.to_thread(_exec)
        return _awaitable().__await__()

class AsyncExecutemanyWrapper:
    def __init__(self, db_manager, sql, seq_of_params):
        self.db_manager = db_manager
        self.sql = sql
        self.seq_of_params = seq_of_params

    def __await__(self):
        async def _awaitable():
            await self.db_manager.wait_ready()
            async with self.db_manager._lock:
                def _exec():
                    cur = self.db_manager.conn.cursor()
                    cur.executemany(self.sql, self.seq_of_params)
                    self.db_manager.conn.commit()
                    return cur.rowcount
                return await asyncio.to_thread(_exec)
        return _awaitable().__await__()

class AsyncConnectionProxy:
    def __init__(self, db_manager):
        self.db_manager = db_manager
        self.row_factory = None

    def execute(self, sql, params=()):
        return AsyncExecuteWrapper(self.db_manager, sql, params)

    def executemany(self, sql, seq_of_params):
        return AsyncExecutemanyWrapper(self.db_manager, sql, seq_of_params)

    async def commit(self):
        if self.db_manager.conn:
            await asyncio.to_thread(self.db_manager.conn.commit)

class DatabaseManager:
    def __init__(self, db_path: str, sync_url: Optional[str] = None, auth_token: Optional[str] = None):
        self.db_path = db_path
        self.sync_url = sync_url
        self.auth_token = auth_token
        self.conn = None
        self._lock = asyncio.Lock()
        self._ready = asyncio.Event()

    async def connect(self):
        async with self._lock:
            if self.conn is not None:
                return # Idempotent: prevent duplicate connections from Beacon callbacks
            
            # Connect via pyturso
            self.conn = turso.sync.connect(
                self.db_path,
                remote_url=self.sync_url,
                auth_token=self.auth_token
            )
            
            # Enforce foreign key constraints
            self.conn.execute("PRAGMA foreign_keys = ON;")
            self.conn.commit()

    def _ensure_schema_sync(self):
        cursor = self.conn.cursor()
        cursor.executescript("""
            -- moderation.py tables (collision renames applied)
            CREATE TABLE IF NOT EXISTS moderation_users (
                guild_id INTEGER,
                user_id INTEGER,
                points INTEGER DEFAULT 0,
                last_punishment INTEGER,
                last_decay INTEGER,
                total_decayed INTEGER DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                action_type TEXT,
                duration INTEGER DEFAULT 0,
                points INTEGER
            );
            CREATE TABLE IF NOT EXISTS ban_schedule (
                guild_id INTEGER,
                user_id INTEGER,
                unban_at INTEGER,
                PRIMARY KEY (guild_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS moderation_settings (
                guild_id INTEGER PRIMARY KEY,
                punishment_dm INTEGER DEFAULT 1,
                punishment_log INTEGER DEFAULT 1,
                decay_interval INTEGER DEFAULT 14,
                rejoin_points INTEGER DEFAULT 4,
                simple_mode INTEGER DEFAULT 1,
                msg_report_enabled INTEGER DEFAULT 0,
                msg_report_channel INTEGER,
                msg_report_roles TEXT,
                decay_log_enabled INTEGER DEFAULT 0,
                show_medals INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS infractions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                case_number INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                moderator_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                reason TEXT,
                punishment_type TEXT,
                punishment_duration INTEGER DEFAULT 0,
                points_after INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                UNIQUE (guild_id, case_number)
            );
            CREATE TABLE IF NOT EXISTS pending_punishments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                moderator_id INTEGER NOT NULL,
                reason TEXT,
                created_at INTEGER NOT NULL,
                timeout_until INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_infractions_guild_user ON infractions(guild_id, user_id);
            CREATE INDEX IF NOT EXISTS idx_infractions_guild_case ON infractions(guild_id, case_number);

            -- discordphone.py tables (collision renames applied)
            CREATE TABLE IF NOT EXISTS discordphone_users (
                id INTEGER PRIMARY KEY, reported INTEGER DEFAULT 0, created INTEGER DEFAULT 0, warned INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS discordphone_guilds (
                id INTEGER PRIMARY KEY, reported INTEGER DEFAULT 0, created INTEGER DEFAULT 0, warned INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS discordphone_settings (
                key TEXT PRIMARY KEY, value TEXT
            );

            -- daily.py tables (collision renames applied)
            CREATE TABLE IF NOT EXISTS cat_channels (channel_id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS cat_images (
                id INTEGER PRIMARY KEY AUTOINCREMENT, 
                image_data BLOB,
                user_id INTEGER DEFAULT 758576879715483719
            );
            -- ensure column exists for existing tables
            PRAGMA table_info(cat_images);
            CREATE TABLE IF NOT EXISTS daily_settings (key TEXT PRIMARY KEY, value TEXT);

            -- selfpurge.py tables (collision renames applied)
            CREATE TABLE IF NOT EXISTS selfpurge_guild_settings (
                guild_id INTEGER PRIMARY KEY,
                enabled INTEGER
            );
            CREATE TABLE IF NOT EXISTS scheduled_purges (
                guild_id INTEGER,
                user_id INTEGER,
                execute_at REAL,
                PRIMARY KEY (guild_id, user_id)
            );

            -- starboard.py tables (collision renames applied)
            CREATE TABLE IF NOT EXISTS starboard_guild_settings (
                guild_id INTEGER PRIMARY KEY,
                star_threshold INTEGER DEFAULT 3,
                starboard_channel_id INTEGER,
                lfg_threshold INTEGER DEFAULT 4,
                enabled INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS star_posts (
                guild_id INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL,
                starboard_message_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, source_message_id)
            );

            -- skullboard.py tables (collision renames applied)
            CREATE TABLE IF NOT EXISTS skullboard_guild_settings (
                guild_id INTEGER PRIMARY KEY,
                skull_threshold INTEGER DEFAULT 3,
                skullboard_channel_id INTEGER,
                enabled INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS skull_posts (
                guild_id INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL,
                skullboard_message_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, source_message_id)
            );

            -- welcome.py tables
            CREATE TABLE IF NOT EXISTS welcome_settings (
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
                embed_color TEXT,
                text_bg_opacity TEXT DEFAULT 'none',
                text_border TEXT DEFAULT 'none'
            );

            -- topgg.py tables
            CREATE TABLE IF NOT EXISTS voters (
                user_id INTEGER PRIMARY KEY,
                voted_at TIMESTAMP,
                last_checked TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_voters_voted_at ON voters(voted_at);
            CREATE INDEX IF NOT EXISTS idx_voters_last_checked ON voters(last_checked);

            -- temphide.py tables
            CREATE TABLE IF NOT EXISTS temp_messages (
                message_id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                hidden_text TEXT NOT NULL,
                timestamp REAL NOT NULL
            );

            -- sticky_messages.py tables
            CREATE TABLE IF NOT EXISTS sticky_panels (
                guild_id INTEGER, panel_id INTEGER, title TEXT, description TEXT, footer TEXT,
                image_url TEXT, embed_color TEXT, channel_id INTEGER, last_message_id INTEGER,
                conversation_duration INTEGER DEFAULT 10, include_bots INTEGER DEFAULT 1,
                response_type TEXT DEFAULT 'embed',
                response_text TEXT,
                embed_content TEXT,
                embed_data TEXT,
                conv_mode TEXT DEFAULT 'Dynamic',
                PRIMARY KEY (guild_id, panel_id)
            );

            -- slowmode.py tables
            CREATE TABLE IF NOT EXISTS slowmode_schedules (
                id INTEGER PRIMARY KEY,
                guild_id INTEGER,
                channel_id INTEGER,
                delay_seconds INTEGER,
                start_min_utc INTEGER,
                end_min_utc INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_slow_channel ON slowmode_schedules(channel_id);

            -- repeating_messages.py tables
            CREATE TABLE IF NOT EXISTS scheduled_messages (
                guild_id INTEGER,
                message_id INTEGER,
                name TEXT,
                channel_id INTEGER,
                message_content TEXT,
                frequency_seconds INTEGER,
                next_send_time REAL,
                is_active INTEGER DEFAULT 1,
                started_at REAL,
                response_type TEXT DEFAULT 'text',
                embed_content TEXT,
                embed_data TEXT,
                PRIMARY KEY (guild_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_sm_active ON scheduled_messages(is_active, next_send_time);

            -- notes.py tables
            CREATE TABLE IF NOT EXISTS user_notes (
                user_id INTEGER,
                note_name TEXT,
                note_content TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, note_name)
            );
            CREATE INDEX IF NOT EXISTS idx_user_notes_user_id ON user_notes (user_id);

            -- member_tracker.py tables
            CREATE TABLE IF NOT EXISTS member_tracker (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER,
                is_active INTEGER DEFAULT 0,
                member_goal INTEGER,
                custom_format TEXT,
                last_member_count INTEGER,
                color INTEGER,
                exclude_bots INTEGER DEFAULT 0
            );

            -- leave.py tables
            CREATE TABLE IF NOT EXISTS leave_settings (
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
                embed_color TEXT,
                text_bg_opacity TEXT DEFAULT 'none',
                text_border TEXT DEFAULT 'none'
            );

            -- giveaway.py tables
            CREATE TABLE IF NOT EXISTS giveaways (
                guild_id INTEGER,
                giveaway_id INTEGER,
                channel_id INTEGER,
                message_id INTEGER,
                prize TEXT,
                winners_count INTEGER,
                end_time INTEGER,
                host_id INTEGER,
                required_roles TEXT,
                req_behaviour INTEGER,
                blacklisted_roles TEXT,
                extra_entry_roles TEXT,
                winner_role_id TEXT,
                image_url TEXT,
                thumbnail_url TEXT,
                color TEXT,
                ended INTEGER DEFAULT 0,
                PRIMARY KEY (guild_id, giveaway_id)
            );
            CREATE TABLE IF NOT EXISTS giveaway_participants (
                guild_id INTEGER,
                giveaway_id INTEGER,
                user_id INTEGER,
                PRIMARY KEY (guild_id, giveaway_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS giveaway_winners (
                giveaway_id INTEGER,
                user_id INTEGER,
                PRIMARY KEY (giveaway_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS templates (
                template_id TEXT PRIMARY KEY,
                creator_id INTEGER,
                creation_guild_id INTEGER,
                prize TEXT,
                winners INTEGER,
                duration TEXT,
                channel_id INTEGER,
                host_id INTEGER,
                required_roles TEXT,
                req_behaviour INTEGER,
                blacklisted_roles TEXT,
                extra_entries TEXT,
                winner_role_id TEXT,
                image TEXT,
                thumbnail TEXT,
                color TEXT,
                usage_count INTEGER DEFAULT 0,
                is_published INTEGER DEFAULT 0,
                review_status TEXT DEFAULT 'none'
            );
            CREATE TABLE IF NOT EXISTS review_config (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER
            );

            -- haiku.py tables
            CREATE TABLE IF NOT EXISTS haiku_settings (
                guild_id INTEGER PRIMARY KEY,
                is_enabled INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS haiku_words (
                word TEXT PRIMARY KEY,
                syllables INTEGER
            );

            -- factorial.py tables
            CREATE TABLE IF NOT EXISTS enabled_guilds (
                guild_id INTEGER PRIMARY KEY
            );

            -- embed.py tables
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
            );

            -- data.py tables (backup_log excluded per removal plan)
            CREATE TABLE IF NOT EXISTS guild_inviters (
                guild_id INTEGER PRIMARY KEY,
                inviter_user_id INTEGER,
                guild_name TEXT,
                joined_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS removal_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                guild_name TEXT,
                responder_user_id INTEGER,
                reason TEXT,
                other_text TEXT,
                responded_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS export_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                requester_user_id INTEGER NOT NULL,
                scope TEXT NOT NULL,
                subject_user_id INTEGER,
                guild_id INTEGER,
                feature_id TEXT,
                guild_ids_json TEXT,
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                process_after INTEGER NOT NULL,
                started_at INTEGER,
                completed_at INTEGER,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS export_rate_limits (
                requester_user_id INTEGER NOT NULL,
                scope TEXT NOT NULL,
                guild_id INTEGER,
                last_export_at INTEGER NOT NULL,
                PRIMARY KEY (requester_user_id, scope, guild_id)
            );
            CREATE TABLE IF NOT EXISTS monitor_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                feature_id TEXT,
                action TEXT,
                detail TEXT,
                created_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS usage_daily (
                date TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                guild_id INTEGER,
                feature_id TEXT NOT NULL,
                command_name TEXT,
                count INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (date, user_id, guild_id, feature_id, command_name)
            );
            CREATE TABLE IF NOT EXISTS guild_removal_schedule (
                guild_id INTEGER PRIMARY KEY,
                guild_name TEXT,
                removed_at INTEGER NOT NULL
            );

            -- ban.py tables
            CREATE TABLE IF NOT EXISTS banned_users (
                user_id INTEGER PRIMARY KEY,
                reason TEXT
            );
            CREATE TABLE IF NOT EXISTS banned_guilds (
                guild_id INTEGER PRIMARY KEY,
                reason TEXT
            );

            -- autoresponse.py tables
            CREATE TABLE IF NOT EXISTS autoresponses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                trigger TEXT,
                response_type TEXT,
                response_text TEXT,
                embed_content TEXT,
                embed_data TEXT,
                channels TEXT,
                match_mode TEXT,
                fuzzy_threshold INTEGER DEFAULT 75,
                case_sensitive INTEGER DEFAULT 0,
                created_by INTEGER,
                created_at INTEGER
            );

            -- autoreact.py tables
            CREATE TABLE IF NOT EXISTS autoreact_panels (
                guild_id INTEGER,
                panel_id INTEGER,
                name TEXT,
                emoji TEXT,
                channel_id INTEGER,
                is_active INTEGER DEFAULT 0,
                member_whitelist INTEGER DEFAULT 0,
                image_only_mode INTEGER DEFAULT 0,
                started_at REAL,
                PRIMARY KEY (guild_id, panel_id)
            );
            CREATE TABLE IF NOT EXISTS autoreact_whitelist (
                guild_id INTEGER,
                panel_id INTEGER,
                user_id INTEGER,
                PRIMARY KEY (guild_id, panel_id, user_id)
            );

            -- autopublish.py tables
            CREATE TABLE IF NOT EXISTS autopublish_channels (
                channel_id INTEGER PRIMARY KEY,
                guild_id INTEGER
            );

            -- alerts.py tables
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                read_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS alert_reads (
                alert_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                PRIMARY KEY (alert_id, user_id)
            );

            -- afk.py tables
            CREATE TABLE IF NOT EXISTS afk_users (
                user_id INTEGER PRIMARY KEY,
                status TEXT,
                is_global INTEGER DEFAULT 1,
                role_id INTEGER,
                save_missed_pings INTEGER DEFAULT 1,
                started_at INTEGER NOT NULL,
                buffer_until INTEGER NOT NULL,
                origin_guild_id INTEGER,
                old_nick TEXT
            );
            CREATE TABLE IF NOT EXISTS missed_pings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                author_id INTEGER NOT NULL,
                guild_id INTEGER,
                channel_id INTEGER,
                message_id INTEGER,
                content TEXT,
                timestamp INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS return_notifications (
                afk_user_id INTEGER NOT NULL,
                observer_id INTEGER NOT NULL,
                PRIMARY KEY (afk_user_id, observer_id)
            );
            CREATE INDEX IF NOT EXISTS idx_missed_pings_user_id ON missed_pings (user_id, timestamp);

            -- nickname.py tables
            CREATE TABLE IF NOT EXISTS serversettings (
                guild_id INTEGER NOT NULL PRIMARY KEY,
                symbol_filter INTEGER DEFAULT 0,
                profanity_filter INTEGER DEFAULT 0,
                placeholder TEXT DEFAULT 'Change your nickname',
                last_scan INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS profanity (
                word TEXT NOT NULL PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS verified (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );

            -- utils/log.py tables
            CREATE TABLE IF NOT EXISTS log_channels (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER
            );
        """)
        self.conn.commit()
        if hasattr(self.conn, "push"):
            try:
                self.conn.push()
            except Exception:
                pass

    async def ensure_schema(self):
        async with self._lock:
            await asyncio.to_thread(self._ensure_schema_sync)
        self._ready.set()

    async def wait_ready(self):
        """Edge Case 2: Block background task loops until DB connection & schema are ready."""
        await self._ready.wait()

    def _dict_factory(self, cursor, row) -> Dict[str, Any]:
        """Edge Case 1: Convert raw tuples to dictionary rows."""
        return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}

    async def execute(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        await self.wait_ready()
        async with self._lock:
            return await asyncio.to_thread(self._execute_sync, sql, params)

    async def execute_write(self, sql: str, params: tuple = ()) -> int:
        await self.wait_ready()
        async with self._lock:
            return await asyncio.to_thread(self._execute_write_sync, sql, params)

    def _execute_sync(self, sql: str, params: tuple):
        cursor = self.conn.cursor()
        cursor.execute(sql, params)
        if cursor.description:
            rows = cursor.fetchall()
            return [self._dict_factory(cursor, r) for r in rows]
        self.conn.commit()
        return []

    def _execute_write_sync(self, sql: str, params: tuple) -> int:
        cursor = self.conn.cursor()
        cursor.execute(sql, params)
        rowcount = cursor.rowcount
        self.conn.commit()
        return rowcount

    @asynccontextmanager
    async def acquire_db(self):
        await self.wait_ready()
        async with self._lock:
            proxy = AsyncConnectionProxy(self)
            yield proxy

    async def close(self):
        async with self._lock:
            if self.conn:
                self.conn.close()
                self.conn = None
            self._ready.clear()
