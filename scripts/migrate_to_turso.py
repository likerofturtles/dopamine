import os
import sys
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import turso, turso.sync

DB_DIR = BASE_DIR / "databases"
TARGET_DB = str(DB_DIR / "dopamine.db")

TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN")


SOURCE_DB_TABLE_MAP = {
    "points.db": {
        "users": "moderation_users",
        "actions": "actions",
        "ban_schedule": "ban_schedule",
        "settings": "moderation_settings",
        "infractions": "infractions",
        "pending_punishments": "pending_punishments",
    },
    "discordphone.db": {
        "users": "discordphone_users",
        "guilds": "discordphone_guilds",
        "settings": "discordphone_settings",
    },
    "daily.db": {
        "cat_channels": "cat_channels",
        "cat_images": "cat_images",
        "settings": "daily_settings",
    },
    "selfpurge.db": {
        "guild_settings": "selfpurge_guild_settings",
        "scheduled_purges": "scheduled_purges",
    },
    "starboard.db": {
        "guild_settings": "starboard_guild_settings",
        "star_posts": "star_posts",
    },
    "skullboard.db": {
        "guild_settings": "skullboard_guild_settings",
        "skull_posts": "skull_posts",
    },
    "welcome.db": {
        "welcome_settings": "welcome_settings",
    },
    "topgg.db": {
        "voters": "voters",
    },
    "temp.db": {
        "temp_messages": "temp_messages",
    },
    "sticky_messages.db": {
        "sticky_panels": "sticky_panels",
    },
    "slowmode.db": {
        "slowmode_schedules": "slowmode_schedules",
    },
    "scheduled_messages.db": {
        "scheduled_messages": "scheduled_messages",
    },
    "notes.db": {
        "user_notes": "user_notes",
    },
    "member_count_tracker.db": {
        "member_tracker": "member_tracker",
    },
    "leave.db": {
        "leave_settings": "leave_settings",
    },
    "giveaway.db": {
        "giveaways": "giveaways",
        "giveaway_participants": "giveaway_participants",
        "giveaway_winners": "giveaway_winners",
        "templates": "templates",
        "review_config": "review_config",
    },
    "haiku_detection.db": {
        "haiku_settings": "haiku_settings",
    },
    "haiku_words.db": {
        "haiku_words": "haiku_words",
    },
    "factorial.db": {
        "enabled_guilds": "enabled_guilds",
    },
    "embeds.db": {
        "embeds": "embeds",
    },
    "data.db": {
        "guild_inviters": "guild_inviters",
        "removal_feedback": "removal_feedback",
        "export_queue": "export_queue",
        "export_rate_limits": "export_rate_limits",
        "monitor_log": "monitor_log",
        "usage_daily": "usage_daily",
        "guild_removal_schedule": "guild_removal_schedule",
    },
    "ban.db": {
        "banned_users": "banned_users",
        "banned_guilds": "banned_guilds",
    },
    "autoresponse.db": {
        "autoresponses": "autoresponses",
    },
    "autoreact.db": {
        "autoreact_panels": "autoreact_panels",
        "autoreact_whitelist": "autoreact_whitelist",
    },
    "autopublish.db": {
        "autopublish_channels": "autopublish_channels",
    },
    "alerts.db": {
        "alerts": "alerts",
        "alert_reads": "alert_reads",
    },
    "afk.db": {
        "afk_users": "afk_users",
        "missed_pings": "missed_pings",
        "return_notifications": "return_notifications",
    },
    "nickname.db": {
        "serversettings": "serversettings",
        "profanity": "profanity",
        "verified": "verified",
    },
    "logging.db": {
        "log_channels": "log_channels",
    },
}


def create_target_schema(target_conn):
    cursor = target_conn.cursor()
    cursor.executescript("""
        PRAGMA foreign_keys = ON;

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

        CREATE TABLE IF NOT EXISTS discordphone_users (
            id INTEGER PRIMARY KEY, reported INTEGER DEFAULT 0, created INTEGER DEFAULT 0, warned INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS discordphone_guilds (
            id INTEGER PRIMARY KEY, reported INTEGER DEFAULT 0, created INTEGER DEFAULT 0, warned INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS discordphone_settings (
            key TEXT PRIMARY KEY, value TEXT
        );

        CREATE TABLE IF NOT EXISTS cat_channels (channel_id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS cat_images (id INTEGER PRIMARY KEY AUTOINCREMENT, image_data BLOB);
        CREATE TABLE IF NOT EXISTS daily_settings (key TEXT PRIMARY KEY, value TEXT);

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

        CREATE TABLE IF NOT EXISTS voters (
            user_id INTEGER PRIMARY KEY,
            voted_at TIMESTAMP,
            last_checked TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_voters_voted_at ON voters(voted_at);
        CREATE INDEX IF NOT EXISTS idx_voters_last_checked ON voters(last_checked);

        CREATE TABLE IF NOT EXISTS temp_messages (
            message_id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            hidden_text TEXT NOT NULL,
            timestamp REAL NOT NULL
        );

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

        CREATE TABLE IF NOT EXISTS slowmode_schedules (
            id INTEGER PRIMARY KEY,
            guild_id INTEGER,
            channel_id INTEGER,
            delay_seconds INTEGER,
            start_min_utc INTEGER,
            end_min_utc INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_slow_channel ON slowmode_schedules(channel_id);

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

        CREATE TABLE IF NOT EXISTS user_notes (
            user_id INTEGER,
            note_name TEXT,
            note_content TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, note_name)
        );
        CREATE INDEX IF NOT EXISTS idx_user_notes_user_id ON user_notes (user_id);

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

        CREATE TABLE IF NOT EXISTS haiku_settings (
            guild_id INTEGER PRIMARY KEY,
            is_enabled INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS haiku_words (
            word TEXT PRIMARY KEY,
            syllables INTEGER
        );

        CREATE TABLE IF NOT EXISTS enabled_guilds (
            guild_id INTEGER PRIMARY KEY
        );

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

        CREATE TABLE IF NOT EXISTS banned_users (
            user_id INTEGER PRIMARY KEY,
            reason TEXT
        );
        CREATE TABLE IF NOT EXISTS banned_guilds (
            guild_id INTEGER PRIMARY KEY,
            reason TEXT
        );

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

        CREATE TABLE IF NOT EXISTS autopublish_channels (
            channel_id INTEGER PRIMARY KEY,
            guild_id INTEGER
        );

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

        CREATE TABLE IF NOT EXISTS log_channels (
            guild_id INTEGER PRIMARY KEY,
            channel_id INTEGER
        );
    """)
    target_conn.commit()


def migrate_table(src_conn: sqlite3.Connection, target_conn, src_table: str, tgt_table: str):
    src_cur = src_conn.cursor()
    try:
        src_cur.execute(f"SELECT * FROM {src_table}")
    except sqlite3.OperationalError:
        print(f"  [SKIP] Table {src_table} does not exist in source")
        return 0

    rows = src_cur.fetchall()
    if not rows:
        print(f"  [EMPTY] {src_table} -> {tgt_table}")
        return 0

    col_names = [desc[0] for desc in src_cur.description]
    placeholders = ", ".join(["?"] * len(col_names))
    col_sql = ", ".join(col_names)
    tgt_cur = target_conn.cursor()

    migrated = 0
    for row in rows:
        try:
            tgt_cur.execute(
                f"INSERT OR REPLACE INTO {tgt_table} ({col_sql}) VALUES ({placeholders})",
                tuple(row)
            )
            migrated += 1
        except Exception as e:
            print(f"  [WARN] Row insert failed in {tgt_table}: {e}")

    target_conn.commit()
    print(f"  [OK] {src_table} -> {tgt_table}: {migrated} rows")
    return migrated


def main():
    print("=" * 70)
    print("Turso Migration: Local *.db files -> dopamine.db (pyturso)")
    print("=" * 70)

    DB_DIR.mkdir(parents=True, exist_ok=True)

    print(f"\nConnecting to target: {TARGET_DB}")
    print(f"Turso sync URL: {TURSO_DATABASE_URL or '(local only)'}")

    target_conn = turso.sync.connect(
        TARGET_DB,
        remote_url=TURSO_DATABASE_URL,
        auth_token=TURSO_AUTH_TOKEN
    )
    target_conn.execute("PRAGMA foreign_keys = ON;")

    print("\nCreating consolidated schema on target...")
    create_target_schema(target_conn)

    total_rows = 0
    total_files = 0

    for src_filename, table_map in SOURCE_DB_TABLE_MAP.items():
        src_path = DB_DIR / src_filename
        if not src_path.exists():
            print(f"\n[MISSING] {src_path} — skipping file")
            continue

        print(f"\n[{src_filename}]")
        total_files += 1
        src_conn = sqlite3.connect(str(src_path))
        src_conn.row_factory = None

        for src_table, tgt_table in table_map.items():
            total_rows += migrate_table(src_conn, target_conn, src_table, tgt_table)

        src_conn.close()

    print("\n" + "=" * 70)
    print(f"Migration complete.")
    print(f"  Files processed : {total_files}")
    print(f"  Rows migrated   : {total_rows}")
    print(f"  Target DB       : {TARGET_DB}")
    print("=" * 70)

    try:
        target_conn.sync()
        print("[SYNC] Pushed to Turso Cloud primary (if configured)")
    except Exception as e:
        print(f"[SYNC] Cloud sync skipped: {e}")

    target_conn.close()


if __name__ == "__main__":
    main()
