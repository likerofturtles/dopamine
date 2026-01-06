from dataclasses import dataclass, field
from typing import Optional, List
import discord
from discord import app_commands, Interaction
from discord.ext import commands, tasks
from discord.ui import View

@dataclass
class GiveawayDraft:
    guild_id: int
    creator_id: int
    channel_id: int
    prize: str
    winners: int
    duration_seconds: int
    # Optional settings
    host_id: Optional[int] = None
    required_roles: List[int] = field(default_factory=list)
    any_role_requirement: bool = False
    blacklisted_roles: List[int] = field(default_factory=list)
    extra_entry_roles: List[int] = field(default_factory=list)
    winner_role_id: Optional[int] = None
    image_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    color: int = 0x2b2d31


