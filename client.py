"""The shared Discord bot and scheduler instances.

Kept in their own module so commands.py, routines.py and bot.py can all
attach to the same objects without import cycles.
"""

import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from discord.ext import commands

from config import MOSCOW

intents = discord.Intents.default()
intents.message_content = True

bot       = commands.Bot(command_prefix="!", intents=intents)
scheduler = AsyncIOScheduler(timezone=MOSCOW)
