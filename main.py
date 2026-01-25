import os
import logging
import asyncio
import time
import sys
import signal
import discord
from discord.ext import commands, tasks
from config import TOKEN, LOGGING_DEBUG_MODE
from logging.handlers import RotatingFileHandler

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
    maxBytes=5 * 1024 * 1024,
    backupCount=5
)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.reactions = True

bot = commands.Bot(command_prefix="!!", intents=intents, help_command=None)
bot.synced = False

async def setup_hook():
    cogs_dir = os.path.join(os.path.dirname(__file__), "cogs")

    if os.path.exists(cogs_dir):
        for filename in os.listdir(cogs_dir):
            if filename.endswith(".py") and not filename.startswith("__"):
                extension = f"cogs.{filename[:-3]}"
                try:
                    await bot.load_extension(extension)
                    print(f"> Loaded {extension} Successfully")
                except Exception as e:
                    print(f"ERROR: Failed to load {extension}: {e}")
    else:
        print("WARNING: 'cogs' directory not found.")

    try:
        await bot.tree.sync()
        print("Synced slash commands globally.")
    except Exception as e:
        print(f"Error: Failed to sync commands: {e}")
    for s in (signal.SIGINT, signal.SIGTERM):
        bot.loop.add_signal_handler(
            s, lambda: asyncio.create_task(signal_handler())
        )

    async def signal_handler():
        print("\nBot shutdown requested...")
        extensions = list(bot.extensions.keys())
        for extension in extensions:
            try:
                await bot.unload_extension(extension)
                print(f"Unloaded {extension}")
            except Exception as e:
                print(f"Error unloading {extension}: {e}")

        print("👋 Goodbye!")
        await bot.close()

bot.setup_hook = setup_hook

@bot.event
async def on_ready():
    if bot.owner_id is None:
        app_info = await bot.application_info()
        bot.owner_id = app_info.owner.id
        owner_username = bot.get_user(bot.owner_id)
        if not owner_username:
            owner_username = await bot.fetch_user(bot.owner_id)

    print(f"---------------------------------------------------")
    print(f"Bot ready: {bot.user} (ID: {bot.user.id})")
    print(f"Bot Owner identified: {owner_username}")
    print(f"---------------------------------------------------")

    await bot.change_presence(
        status=discord.Status.dnd,
        activity=discord.CustomActivity(name="Flirting with your neurons")
    )

@bot.tree.command(name="fuckoff", description="Is the bot annoying you? Tell it to fuck off and shut itself down using this.")
async def fuckoff(interaction: discord.Interaction):
    if interaction.user.id != bot.owner_id:
        await interaction.response.send_message(
            "What do you think you're doing? Who do you think you are?? Why do you want to kill me???\nYou're not my dev. Don't tell me what to do. Go away.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        "K 👍\nFucking off now.",
        ephemeral=True
    )

    async def shutdown():
        for extension in list(bot.extensions.keys()):
            try:
                await bot.unload_extension(extension)
                print(f"Unloaded {extension}")
            except Exception as e:
                print(f"Failed to unload {extension}: {e}")
        await bot.close()

    asyncio.create_task(shutdown())

if __name__ == "__main__":
    async def main_async():
        try:
            async with bot:
                await bot.start(TOKEN)
        except Exception as e:
            print(f"ERROR: Failed to start the bot: {e}")

    asyncio.run(main_async())
