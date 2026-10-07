"""The shared Discord bot and scheduler instances, plus the global access gate.

Kept in their own module so commands.py, routines.py and bot.py can all
attach to the same objects without import cycles.
"""

import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from discord import app_commands
from discord.ext import commands

from config import LEAD_USER_ID, UTC2
from db import is_allowed_user

# ---------------------------------------------------------------------------
# Access control — only LEAD_USER_ID and people in the allowed_users DB table
# (managed with !alloweduser) can use any command, ! or /.
# ---------------------------------------------------------------------------

_DENY_MSG = "🚫 You don't have access to this bot."


def _is_authorized(user_id: int) -> bool:
    return user_id == LEAD_USER_ID or is_allowed_user(user_id)


class _GatedCommandTree(app_commands.CommandTree):
    """Gates every / command — hybrid ones and ones registered straight on the tree
    (linkdiscord/addperson), which never go through Bot.check below."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if _is_authorized(interaction.user.id):
            return True
        await interaction.response.send_message(_DENY_MSG, ephemeral=True)
        return False


intents = discord.Intents.default()
intents.message_content = True

bot       = commands.Bot(command_prefix="!", intents=intents, tree_cls=_GatedCommandTree)
scheduler = AsyncIOScheduler(timezone=UTC2)


@bot.check
async def _global_prefix_check(ctx: commands.Context) -> bool:
    """Gates ! commands, and hybrid commands invoked via / (HybridCommand.can_run calls this too —
    harmless overlap with _GatedCommandTree.interaction_check, which already stops denied slash
    calls before this ever runs)."""
    if _is_authorized(ctx.author.id):
        return True
    if ctx.interaction:
        await ctx.send(_DENY_MSG, ephemeral=ctx.guild is not None)
    else:
        await ctx.send(_DENY_MSG)
    return False


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    """Suppress the CheckFailure traceback for denied users — they already got _DENY_MSG above."""
    if isinstance(error, commands.CheckFailure):
        return
    await commands.Bot.on_command_error(bot, ctx, error)
