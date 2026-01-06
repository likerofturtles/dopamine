import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Tokens (nom nom nom, tasty 🤤😝)
TOKEN = os.getenv("DISCORD_TOKEN")
TOPGG_TOKEN = os.getenv("TOPGG_TOKEN")

if not TOKEN:
    raise SystemExit("Set DISCORD_TOKEN in .env")

# Base directory
BASE_DIR = Path(__file__).resolve().parent

# Database paths
DB_PATH = str(BASE_DIR / "databases" / "points.db")
TDB_PATH = str(BASE_DIR / "databases" / "temp.db")
VDB_PATH = str(BASE_DIR / "databases" / "values.db")
TOPDB_PATH = str(BASE_DIR / "databases" / "topgg.db")
SMDB_PATH = str(BASE_DIR / "databases" / "scheduled_messages.db")
STICKYDB_PATH = str(BASE_DIR / "databases" / "sticky_messages.db")
ARDB_PATH = str(BASE_DIR / "databases" / "autoreact.db")
HDDB_PATH = str(BASE_DIR / "databases" / "haiku_detection.db")
HWDDB_PATH = str(BASE_DIR / "databases" / "haiku_words.db")
NOTEDB_PATH = str(BASE_DIR / "databases" / "notes.db")
MCTDB_PATH = str(BASE_DIR / "databases" / "member_count_tracker.db")
SDB_PATH = str(BASE_DIR / "databases" / "starboard.db")
ALERTDB_PATH = str(BASE_DIR / "databases" / "alerts.db")
MAX_PATH = BASE_DIR / "databases" / "MAXWITHSTRAPON.jpg"
FONT_PATH = BASE_DIR / "databases" / "max.ttf"
BDB_PATH = str(BASE_DIR / "databases" / "battery.db")
SSDB_PATH = str(BASE_DIR / "databases" / "slowmode.db")
NFDB_PATH = str(BASE_DIR / "databases" / "nickname.db")
TDB_PATH = str(BASE_DIR / "databases" / "timezone.db")

# Top.gg settings
TOPGG_API_URL = "https://top.gg/api/bots/{bot_id}/check"

# Bot settings
COMMAND_PREFIX = "!!"
