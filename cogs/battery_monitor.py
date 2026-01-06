
import discord
import psutil
import aiosqlite
import asyncio
from discord import app_commands
from discord.ext import commands, tasks
from typing import List, Optional

from config import BDB_PATH


def is_developer():
    def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user.id == 758576879715483719
    return app_commands.check(predicate)

class BatteryMonitor(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._db_conn = None

    async def cog_load(self):
        """Initialize the database table when the cog is loaded."""
        await self.init_battery_monitor_table()
        if not self.update_battery_monitor.is_running():
            self.update_battery_monitor.start()
        if not self._db_keepalive.is_running():
            self._db_keepalive.start()

    async def cog_unload(self):
        """Cancel the background task when the cog is unloaded."""
        if self._db_keepalive.is_running():
            self._db_keepalive.cancel()
        self.update_battery_monitor.cancel()
        if self._db_conn:
            await self._db_conn.close()

    async def get_db_connection(self):
        """Get a database connection with optimized settings for I/O performance"""
        if self._db_conn is None:
            max_retries = 5
            for attempt in range(max_retries):
                try:
                    self._db_conn = await aiosqlite.connect(BDB_PATH, timeout=5.0)
                    await self._db_conn.execute("PRAGMA busy_timeout=5000")
                    await self._db_conn.execute("PRAGMA journal_mode=WAL")
                    await self._db_conn.execute("PRAGMA wal_autocheckpoint=1000")
                    await self._db_conn.execute("PRAGMA synchronous=NORMAL")
                    await self._db_conn.execute("PRAGMA cache_size=-64000")
                    await self._db_conn.execute("PRAGMA foreign_keys=ON")
                    await self._db_conn.execute("PRAGMA optimize")
                    await self._db_conn.commit()
                    break
                except Exception as e:
                    if attempt < max_retries - 1:
                        await asyncio.sleep(0.1 * (2 ** attempt))
                        continue
                    else:
                        raise
        return self._db_conn

    @tasks.loop(seconds=60)
    async def _db_keepalive(self):
        try:
            db = await self.get_db_connection()
            cur = await db.execute("SELECT 1")
            await cur.fetchone()
            await cur.close()
        except Exception:
            pass

    async def init_battery_monitor_table(self):
        db = await self.get_db_connection()
        await db.execute("""
            CREATE TABLE IF NOT EXISTS battery_monitor (
                channel_id INTEGER PRIMARY KEY,
                message_id INTEGER NOT NULL
            )
        """)
        await db.commit()

    async def db_get_all_monitors(self):
        db = await self.get_db_connection()
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT channel_id, message_id FROM battery_monitor")
        rows = await cursor.fetchall()
        await cursor.close()
        return rows

    async def db_set_battery_monitor(self, channel_id: int, message_id: int):
        db = await self.get_db_connection()
        await db.execute(
            "INSERT OR REPLACE INTO battery_monitor(channel_id, message_id) VALUES (?, ?)",
            (channel_id, message_id)
        )
        await db.commit()

    async def db_clear_battery_monitor(self, channel_id: int):
        db = await self.get_db_connection()
        await db.execute("DELETE FROM battery_monitor WHERE channel_id = ?", (channel_id,))
        await db.commit()

    async def monitor_autocomplete(self, interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
        monitors = await self.db_get_all_monitors()
        choices = []
        for monitor in monitors:
            channel = self.bot.get_channel(monitor['channel_id'])
            if channel:
                if channel.guild.id == interaction.guild.id:
                    choices.append(app_commands.Choice(name=f"#{channel.name} in {channel.guild.name}", value=str(channel.id)))

        if current:
            return [choice for choice in choices if current.lower() in choice.name.lower()]
        return choices

    battery = app_commands.Group(name="battery", description="Commands for the battery monitor.")

    @battery.command(name="start", description="Start a live battery monitor in a specific channel.")
    @is_developer()
    @app_commands.describe(channel="The channel to send the live monitor to.")
    async def battery_monitor_start(self, interaction: discord.Interaction, channel: discord.TextChannel):
        try:
            battery = psutil.sensors_battery()
            if battery:
                percent = battery.percent
                charging = battery.power_plugged
                battery_status = f"`{percent}% ({'Charging' if charging else 'Discharging'})`"
            else:
                battery_status = "Not available (Does the host device have a battery?)"
        except Exception:
            battery_status = "Unable to determine"

        embed = discord.Embed(

            description=f"**Host Device Status:** {battery_status}",
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="Last updated")

        try:
            message = await channel.send(embed=embed)
            await self.db_set_battery_monitor(channel.id, message.id)
            await interaction.response.send_message(f"Battery monitor started in {channel.mention}.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("I don't have permission to send messages in that channel.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"An error occurred: {e}", ephemeral=True)


    @battery.command(name="stop", description="Stop a live battery monitor in a channel.")
    @is_developer()
    @app_commands.autocomplete(channel=monitor_autocomplete)
    @app_commands.describe(channel="The channel where the monitor should be stopped.")
    async def battery_monitor_stop(self, interaction: discord.Interaction, channel: str):
        try:
            channel_id = int(channel)
            await self.db_clear_battery_monitor(channel_id)
            target_channel = self.bot.get_channel(channel_id)
            await interaction.response.send_message(f"Battery monitor stopped for {target_channel.mention if target_channel else f'channel ID `{channel_id}`'}.", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("Invalid channel selection.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"An error occurred: {e}", ephemeral=True)


    @battery_monitor_start.error
    @battery_monitor_stop.error
    async def on_battery_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            embed = discord.Embed(
                description="You are not authorized to use this command.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(f"An unexpected error occurred: {error}", ephemeral=True)

    @tasks.loop(seconds=60)
    async def update_battery_monitor(self):
        """Background task to update battery monitor messages"""
        try:
            try:
                monitors = await self.db_get_all_monitors()
            except Exception as e:
                print(f"Error getting database connection in update_battery_monitor: {e}")
                return

            if not monitors:
                return

            try:
                battery = psutil.sensors_battery()
                if battery:
                    percent = battery.percent
                    charging = battery.power_plugged
                    battery_status = f"`{percent}% ({'Charging' if charging else 'Discharging'})`"
                else:
                    battery_status = "Not available (Does the host device have a battery?)"
            except Exception:
                battery_status = "Unable to determine"

            for monitor in monitors:
                channel_id = monitor["channel_id"]
                message_id = monitor["message_id"]

                channel = self.bot.get_channel(channel_id)
                if not channel:
                    await self.db_clear_battery_monitor(channel_id)
                    continue

                try:
                    message = await channel.fetch_message(message_id)
                    
                    embed = message.embeds[0]
                    embed.description = f"**Host Device Status:** {battery_status}"
                    embed.timestamp = discord.utils.utcnow()

                    await message.edit(embed=embed)

                except discord.NotFound:
                    await self.db_clear_battery_monitor(channel_id)
                except (discord.Forbidden, IndexError):
                    pass
                except Exception as e:
                    print(f"Error updating battery monitor in {channel_id}: {e}")

        except Exception as e:
            print(f"Error in update_battery_monitor task: {e}")

    @update_battery_monitor.before_loop
    async def before_update_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(BatteryMonitor(bot))
