import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Tokens (nom nom nom, tasty 🤤😝)
TOKEN = os.getenv("DISCORD_TOKEN")
TOPGG_TOKEN = os.getenv("TOPGG_TOKEN")
OVERRIDE_VOTEWALL = os.getenv("OVERRIDE_VOTEWALL", True)
DEBUG_MODE = os.getenv("LOGGING_DEBUG_MODE")
if DEBUG_MODE.lower() == "true":
    LOGGING_DEBUG_MODE = True
else:
    LOGGING_DEBUG_MODE = False
HEARTBEAT_URL = os.getenv("HEARTBEAT_URL", None)
if not TOKEN:
    raise SystemExit("Set DISCORD_TOKEN in .env")
DBL_TOKEN = os.getenv("DBL_TOKEN", None)
API_TOKEN = os.getenv("API_TOKEN", None)
HEARTBEAT_ID = os.getenv("HEARTBEAT_ID", None)
TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL", None)
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN", None)

# Base directory
BASE_DIR = Path(__file__).resolve().parent

# Turso database path
TURSO_LOCAL_PATH = str(BASE_DIR / "databases" / "dopamine.db")
DATABASES_DIR = BASE_DIR / "databases"

# Asset paths (fonts, images, etc.)
MAX_PATH = BASE_DIR / "databases" / "MAXWITHSTRAPON.jpg"
FONT_PATH = BASE_DIR / "databases" / "max.ttf"
WELCOMECARD_PATH = BASE_DIR / "databases" / "welcomecard.png"
LEAVECARD_PATH = BASE_DIR / "databases" / "welcomecard.png"
BOLDFONT_PATH = BASE_DIR / "databases" / "Bold.ttf"
MEDIUMFONT_PATH = BASE_DIR / "databases" / "Medium.ttf"

# Top.gg settings
TOPGG_API_URL = "https://top.gg/api/bots/{bot_id}/check"

# Bot settings
COMMAND_PREFIX = "!!"
