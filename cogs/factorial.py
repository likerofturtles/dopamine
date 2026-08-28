from __future__ import annotations

import ast
import math
import re
from typing import Any, List, Optional

import discord
from beacon import beacon_commands
from discord.ext import commands


class FactorialCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.enabled_cache: set[int] = set()
        self.regex = re.compile(r'([0-9\.\+\-\*\/\(\)\^\s]+)!')

    async def cog_load(self) -> None:
        await self.bot.db.wait_ready()
        rows = await self.bot.db.execute("SELECT guild_id FROM enabled_guilds")
        self.enabled_cache = {row["guild_id"] for row in rows}

    async def cog_unload(self) -> None:
        pass

    def safe_eval_math(self, expr_str: str) -> float | int | None:
        operators = {
            ast.Add: float.__add__,
            ast.Sub: float.__sub__,
            ast.Mult: float.__mul__,
            ast.Div: float.__truediv__,
            ast.Pow: float.__pow__,
            ast.USub: float.__neg__,
            ast.UAdd: float.__pos__
        }

        def eval_node(node: ast.AST) -> Any:
            if isinstance(node, ast.Num):
                return node.n
            elif isinstance(node, ast.Constant):
                return node.value
            elif isinstance(node, ast.BinOp):
                left = eval_node(node.left)
                right = eval_node(node.right)
                return operators[type(node.op)](left, right)
            elif isinstance(node, ast.UnaryOp):
                operand = eval_node(node.operand)
                return operators[type(node.op)](operand)
            else:
                raise TypeError("Unsupported operation")

        try:
            cleaned_expr = expr_str.replace("^", "**").strip()
            return eval_node(ast.parse(cleaned_expr, mode='eval').body)
        except Exception:
            return None

    def calculate_factorial(self, n: float | int) -> tuple[Optional[str], bool]:
        try:
            if n < 0:
                return None, False

            if n > 3000:
                return None, False

            if n <= 40:
                if abs(n - round(n)) < 1e-9:
                    return str(math.factorial(int(round(n)))), False
                else:
                    res = math.gamma(n + 1)
                    return f"{res:.4f}", False

            log_factorial = math.lgamma(n + 1) / math.log(10)
            exponent = int(math.floor(log_factorial))
            mantissa_log = log_factorial - exponent
            mantissa = 10 ** mantissa_log

            return f"{mantissa:.4f} × 10^{exponent}", True

        except Exception:
            return None, False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return

        if message.guild.id not in self.enabled_cache:
            return

        match = self.regex.search(message.content)
        if not match:
            return

        math_string = match.group(1)
        number = self.safe_eval_math(math_string)

        if number is None or not isinstance(number, (int, float)):
            return

        result_str, is_sci = self.calculate_factorial(number)

        if result_str:
            clean_num = int(number) if number == int(number) else number
            await message.reply(f"{clean_num}! = {result_str} 🤓\n-# Use `/factorial` to disable.")

    @beacon_commands.command(name="factorial", description="Toggle accidental factorial detection for this server.", permissions_preset="manager")
    async def factorial_toggle(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        if not guild_id:
            return

        rows = await self.bot.db.execute("SELECT 1 FROM enabled_guilds WHERE guild_id = ?", (guild_id,))
        exists = bool(rows)

        if exists:
            await self.bot.db.execute("DELETE FROM enabled_guilds WHERE guild_id = ?", (guild_id,))
            if guild_id in self.enabled_cache:
                self.enabled_cache.remove(guild_id)
            await interaction.response.send_message("Factorial detection has been **DISABLED** for this server.",
                                                    ephemeral=False)
        else:
            await self.bot.db.execute("INSERT OR IGNORE INTO enabled_guilds (guild_id) VALUES (?)", (guild_id,))
            self.enabled_cache.add(guild_id)
            await interaction.response.send_message("Factorial detection has been **ENABLED** for this server.",
                                                    ephemeral=False)

    def data_features(self) -> list:
        from utils.data_protocol import DataFeatureMeta
        return [DataFeatureMeta(feature_id="factorial", name="Factorial Detection", guild_export=True, guild_delete=True)]

    async def data_export_user(self, user_id: int, *, guild_ids: list[int] | None) -> Any:
        from utils.data_protocol import DataExportChunk
        return DataExportChunk(feature_id="factorial")

    async def data_export_guild(self, guild_id: int) -> Any:
        from utils.data_protocol import DataExportChunk
        chunk = DataExportChunk(feature_id="factorial")
        if guild_id in self.enabled_cache:
            chunk.guild_data[guild_id] = {"enabled": True}
        return chunk

    async def data_delete_user(self, user_id: int, *, guild_ids: list[int] | None, feature_id: str | None) -> Any:
        from utils.data_protocol import DataDeleteResult
        return DataDeleteResult(feature_id="factorial")

    async def data_delete_guild(self, guild_id: int, feature_id: str | None) -> Any:
        from utils.data_protocol import DataDeleteResult
        rows_before = len(self.enabled_cache)
        await self.bot.db.execute("DELETE FROM enabled_guilds WHERE guild_id = ?", (guild_id,))
        self.enabled_cache.discard(guild_id)
        rows_affected = rows_before - len(self.enabled_cache)
        return DataDeleteResult(feature_id="factorial", deleted=True, rows_affected=rows_affected)

    async def data_monitor_guild(self, guild: discord.Guild) -> Any:
        from utils.data_protocol import DataMonitorResult
        return DataMonitorResult(feature_id="factorial")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FactorialCog(bot))