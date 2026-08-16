"""Shared export/delete/monitor SQL helpers for the data management system."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

from utils.data_protocol import DataDeleteResult, DataExportChunk, DataFeatureMeta, DataMonitorResult

if TYPE_CHECKING:
    from utils.database import DatabaseManager


async def export_table(bot_db, query: str, params: tuple = ()) -> list[dict]:
    return await bot_db.execute(query, params)


async def export_usage_user(bot_db, user_id: int, guild_ids: Optional[list[int]]) -> DataExportChunk:
    chunk = DataExportChunk(feature_id="usage")
    if guild_ids is None:
        q = "SELECT date, guild_id, feature_id, command_name, count FROM usage_daily WHERE user_id = ?"
        params = (user_id,)
    else:
        placeholders = ",".join("?" * len(guild_ids))
        q = f"SELECT date, guild_id, feature_id, command_name, count FROM usage_daily WHERE user_id = ? AND (guild_id IS NULL OR guild_id IN ({placeholders}))"
        params = (user_id, *guild_ids)
    rows = await bot_db.execute(q, params)
    for row in rows:
        gid = row.pop("guild_id")
        if gid is None:
            chunk.global_data.setdefault("records", []).append(row)
        else:
            chunk.guild_data.setdefault(gid, {}).setdefault("records", []).append(row)
    return chunk


async def export_usage_guild(bot_db, guild_id: int) -> DataExportChunk:
    chunk = DataExportChunk(feature_id="usage")
    rows = await bot_db.execute(
        "SELECT date, user_id, feature_id, command_name, count FROM usage_daily WHERE guild_id = ?",
        (guild_id,),
    )
    chunk.guild_data[guild_id] = {"records": rows}
    return chunk


async def delete_usage_user(bot_db, user_id: int, guild_ids: Optional[list[int]]) -> DataDeleteResult:
    count_q = "SELECT COUNT(*) AS cnt FROM usage_daily WHERE user_id = ?"
    count_params: tuple = (user_id,)
    if guild_ids is None:
        del_q = "DELETE FROM usage_daily WHERE user_id = ?"
        del_params: tuple = (user_id,)
    else:
        placeholders = ",".join("?" * len(guild_ids))
        count_q = f"SELECT COUNT(*) AS cnt FROM usage_daily WHERE user_id = ? AND guild_id IN ({placeholders})"
        count_params = (user_id, *guild_ids)
        del_q = f"DELETE FROM usage_daily WHERE user_id = ? AND guild_id IN ({placeholders})"
        del_params = (user_id, *guild_ids)
    count_rows = await bot_db.execute(count_q, count_params)
    rows_affected = int(count_rows[0]["cnt"]) if count_rows else 0
    await bot_db.execute(del_q, del_params)
    return DataDeleteResult(feature_id="usage", deleted=True, rows_affected=rows_affected)


async def delete_usage_guild(bot_db, guild_id: int) -> DataDeleteResult:
    count_rows = await bot_db.execute(
        "SELECT COUNT(*) AS cnt FROM usage_daily WHERE guild_id = ?", (guild_id,)
    )
    rows_affected = int(count_rows[0]["cnt"]) if count_rows else 0
    await bot_db.execute("DELETE FROM usage_daily WHERE guild_id = ?", (guild_id,))
    return DataDeleteResult(feature_id="usage", deleted=True, rows_affected=rows_affected)
