"""Detecting who posted a daily report in a channel, including reports from other bots."""

import re
from datetime import date, datetime, timedelta
from typing import Optional

import discord
from discord.ext import commands

from config import UTC3, log

# Embed fields that name the report's author / date in reports posted by other bots
# (e.g. C&C Daily Reports: "Developer: @user", "Date: 02.10.2026").
REPORT_AUTHOR_FIELDS = {"developer", "author", "user", "employee", "member",
                        "разработчик", "сотрудник", "автор"}
REPORT_DATE_FIELDS   = {"date", "дата"}
MENTION_RE           = re.compile(r"<@!?(\d+)>")


def _parse_report_date(value: str, fallback_year: int) -> Optional[date]:
    value = value.strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    try:
        return datetime.strptime(value, "%d.%m").date().replace(year=fallback_year)
    except ValueError:
        return None


def _bot_report_authors(msg: discord.Message, target_date: date) -> set[int]:
    """Authors of a report posted by another bot as an embed.

    Takes users mentioned in an author-like field ("Developer", …); if there is no such
    field — all mentions in the embed plus whoever invoked the bot's command.
    A report whose "Date" field names another day is ignored.
    """
    found:    set[int] = set()
    fallback: set[int] = set()
    for embed in msg.embeds:
        for field in embed.fields:
            fname = (field.name or "").strip().casefold()
            if fname in REPORT_DATE_FIELDS:
                parsed = _parse_report_date(field.value or "", target_date.year)
                if parsed and parsed != target_date:
                    return set()
            ids = {int(i) for i in MENTION_RE.findall(field.value or "")}
            if fname in REPORT_AUTHOR_FIELDS:
                found |= ids
            fallback |= ids
        fallback |= {int(i) for i in MENTION_RE.findall(embed.description or "")}
    if found:
        return found
    meta = getattr(msg, "interaction_metadata", None)
    if meta is not None and getattr(meta, "user", None) is not None:
        fallback.add(meta.user.id)
    return fallback


async def fetch_report_authors(bot: commands.Bot, target_date: date,
                               channel_id: int) -> Optional[set[int]]:
    """
    Return Discord IDs of everyone who posted in *channel_id* on *target_date*
    (00:00–23:59 UTC+3). Any non-bot message counts as a report; for messages from
    other report bots the author is taken from the embed (see _bot_report_authors).

    Text channel → channel history. Forum → messages in all posts (active + archived).
    Returns None if the channel is not configured or can't be read (check is skipped).
    """
    if not channel_id:
        return None

    start = datetime.combine(target_date, datetime.min.time(), tzinfo=UTC3)
    end   = start + timedelta(days=1)
    authors: set[int] = set()

    async def collect(messageable) -> None:
        async for msg in messageable.history(after=start, before=end, limit=None):
            if not msg.author.bot:
                authors.add(msg.author.id)
            elif msg.embeds and msg.author.id != bot.user.id:
                authors.update(_bot_report_authors(msg, target_date))

    try:
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        if isinstance(channel, discord.ForumChannel):
            threads = list(channel.threads)
            # Archived threads come newest-archived first; older ones can't hold target_date messages
            async for t in channel.archived_threads(limit=None):
                if t.archive_timestamp and t.archive_timestamp < start:
                    break
                threads.append(t)
            for t in threads:
                if t.created_at and t.created_at >= end:
                    continue
                await collect(t)
        else:
            await collect(channel)
    except Exception as exc:
        log.exception("fetch_report_authors failed: %s", exc)
        return None

    log.info("Daily reports on %s in %s: %d authors", target_date, channel_id, len(authors))
    return authors
