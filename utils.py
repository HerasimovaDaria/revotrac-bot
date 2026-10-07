"""Small stateless helpers: member/user resolution and workday arithmetic."""

import re
from datetime import date, timedelta
from typing import Optional

import discord

from client import bot
from config import log
from db import _all_members


def _find_member(query: str) -> Optional[str]:
    """Match *query* against member names (RU or EN, case-insensitive).

    Exact match first, otherwise a unique partial match (e.g. "Aleksey" → "Лёша Седин").
    """
    q = query.strip().strip("<>").casefold()
    all_m = _all_members()
    for name, en_name, _, _ in all_m:
        if q in (name.casefold(), en_name.casefold()):
            return name
    partial = [name for name, en_name, _, _ in all_m
               if q in name.casefold() or q in en_name.casefold()]
    return partial[0] if len(partial) == 1 else None


async def _resolve_discord_user(spec: str, mentions: list = ()) -> Optional[discord.abc.User]:
    """Resolve a mention, numeric ID or username (with or without @) to a Discord user.

    In DMs "@nick" stays plain text (no real mention), so we also search the bot's servers.
    """
    if mentions:
        return mentions[0]
    m = re.fullmatch(r"<@!?(\d+)>|(\d{15,20})", spec)
    if m:
        try:
            return await bot.fetch_user(int(m.group(1) or m.group(2)))
        except discord.NotFound:
            return None
    nick = spec.lstrip("@").casefold()
    if not nick:
        return None
    for guild in bot.guilds:
        try:
            found = await guild.query_members(query=nick, limit=10)
        except Exception as exc:
            log.warning("query_members failed in %s: %s", guild, exc)
            continue
        for member in found:
            names = {member.name, member.global_name or "", member.nick or ""}
            if nick in {n.casefold() for n in names}:
                return member
    return None


def week_start(d: date) -> date:
    """Return the Monday of the week containing *d*."""
    return d - timedelta(days=d.weekday())


def previous_workday(d: date) -> date:
    """Return the last Mon–Fri day before *d* (Monday → Friday)."""
    prev = d - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    return prev
