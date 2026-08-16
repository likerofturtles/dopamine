import os
import logging
import asyncio
import discord
from config import TOKEN, LOGGING_DEBUG_MODE, TURSO_LOCAL_PATH, TURSO_DATABASE_URL, TURSO_AUTH_TOKEN
from logging.handlers import RotatingFileHandler
from beacon import BeaconAutoShardedBot
from utils.database import DatabaseManager
import traceback

if not TOKEN:
    raise SystemExit("ERROR: Set DISCORD_TOKEN in a .env in root folder.")

logger = logging.getLogger("discord")
if LOGGING_DEBUG_MODE:
    logger.setLevel(logging.DEBUG)
    print("Running logger in DEBUG mode")
else:
    logger.setLevel(logging.INFO)
    print("Running logger in PRODUCTION mode")
log_path = os.path.join(os.path.dirname(__file__), "discord.log")
handler = RotatingFileHandler(
    filename=log_path,
    encoding="utf-8",
    mode="a",
    maxBytes=1 * 1024 * 1024,
    backupCount=5
)
logger.addHandler(handler)

log_format = '%(asctime)s||%(levelname)s: %(message)s'
date_format = '%H:%M:%S %d-%m'

formatter = logging.Formatter(log_format, datefmt=date_format)

handler.setFormatter(formatter)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.reactions = True


bot = BeaconAutoShardedBot(
    command_prefix="!!",
    cogs_path="cogs",
    version_file="VERSION.py",
    accent_colour=discord.Colour(0x944ae8),
    minimal_caching=True,
    intents=intents,
    bot_logger=logger
)

bot.db = DatabaseManager(
    db_path=TURSO_LOCAL_PATH,
    sync_url=TURSO_DATABASE_URL,
    auth_token=TURSO_AUTH_TOKEN
)

bot.back_emoji = discord.PartialEmoji.from_str("<:back:1529498596402008225>")

@bot.tree.context_menu(name="Get User ID")
async def get_user_id(interaction: discord.Interaction, message: discord.Message):
    author = message.author
    await interaction.response.send_message(
        f"{author.id}",
        ephemeral=True
    )

@bot.tree.context_menu(name="Get Message ID")
async def get_message_id(interaction: discord.Interaction, message: discord.Message):
    await interaction.response.send_message(
        f"{message.id}",
        ephemeral=True
    )

if __name__ == "__main__":
    async def main_async():
        try:
            await bot.db.connect()
            await bot.db.ensure_schema()
            async with bot:
                await bot.start(TOKEN)
        except Exception as e:
            print(f"ERROR: Failed to start the bot: {e}")
            traceback.print_exc()


    asyncio.run(main_async())