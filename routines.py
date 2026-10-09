"""Core routine: collecting data, building/delivering reports, scheduled checks."""

from datetime import date, datetime
from typing import Optional

from discord.ext import commands

from config import MEMBER_NAMES, MIDDAY_HOUR, MIDDAY_MINUTE, REMINDER_HOUR, REMINDER_MINUTE, UTC2, log
from db import (
    _all_renormalize_ids,
    get_all_subscribers,
    get_discord_links,
    get_reminder_subscribers,
    get_reports_channel,
    get_subscription,
    get_users_for_time,
)
from renormalize import fetch_day_offs, fetch_hours, fetch_month_hours, fetch_week_hours
from reports.authors import fetch_report_authors
from reports.formatting import MONTHS, format_daily_report, format_weekly_report
from utils import previous_workday, week_start


async def _collect_report_data(
    bot:         commands.Bot,
    report_date: date,
    members:     Optional[list[str]] = None,   # None → everyone; pass the union of people
                                                # actually tracked by this batch of subscribers
                                                # to skip fetching data for the rest of the roster
) -> tuple[dict[str, tuple[float, float]], set[str], Optional[dict[str, tuple[float, float]]],
           dict[str, tuple[float, float, float]], dict]:
    """Fetch (hours, day_offs, week_hours, month_hours, authors_cache) once for all subscribers.

    week_hours is fetched only when *report_date* is Friday (weekly summary day).
    month_hours (month-to-date, for the "behind this month" figure) is fetched every day.
    authors_cache starts empty and is filled per reports channel in _deliver_report,
    so each channel is read once even when several subscribers share it.
    """
    try:
        hours = await fetch_hours(report_date, members)
    except Exception as exc:
        log.exception("fetch_hours failed: %s", exc)
        hours = {name: (0.0, 0.0) for name in MEMBER_NAMES}

    day_offs = await fetch_day_offs(report_date, members)

    try:
        month_hours = await fetch_month_hours(report_date, members)
    except Exception as exc:
        log.exception("fetch_month_hours failed: %s", exc)
        month_hours = {name: (0.0, 0.0, 0.0) for name in MEMBER_NAMES}

    week_hours: Optional[dict[str, tuple[float, float]]] = None
    if report_date.weekday() == 4:
        try:
            week_hours = await fetch_week_hours(week_start(report_date), members)
        except Exception as exc:
            log.exception("fetch_week_hours failed: %s", exc)
            week_hours = {name: (0.0, 0.0) for name in MEMBER_NAMES}

    return hours, day_offs, week_hours, month_hours, {}


async def _build_report_text(
    bot:            commands.Bot,
    user_id:        int,
    report_date:    date,
    hours:          dict[str, tuple[float, float]],
    week_hours:     Optional[dict[str, tuple[float, float]]],
    month_hours:    dict[str, tuple[float, float, float]],
    day_offs:       set[str],
    filter_members: Optional[list[str]],
    authors_cache:  dict,
) -> str:
    """Daily report (+ weekly progress on Fridays) for one subscriber, using their reports channel."""
    channel_id = get_reports_channel(user_id)
    if channel_id not in authors_cache:
        authors_cache[channel_id] = await fetch_report_authors(bot, report_date, channel_id)
    report_authors = authors_cache[channel_id]

    text = format_daily_report(report_date, hours, day_offs, filter_members, report_authors, month_hours)
    if week_hours is not None:
        weekly_text = format_weekly_report(week_start(report_date), week_hours, filter_members, report_date)
        if weekly_text:
            text += "\n\n" + weekly_text
    return text


async def _deliver_report(
    bot:            commands.Bot,
    user_id:        int,
    report_date:    date,
    hours:          dict[str, tuple[float, float]],
    week_hours:     Optional[dict[str, tuple[float, float]]],   # None → no weekly section
    month_hours:    dict[str, tuple[float, float, float]],
    day_offs:       set[str],
    filter_members: Optional[list[str]],
    authors_cache:  dict,                         # {channel_id: authors} shared within one run
) -> None:
    """Fetch user and send them the daily DM (+ weekly progress on Fridays)."""
    try:
        text = await _build_report_text(bot, user_id, report_date, hours, week_hours, month_hours,
                                        day_offs, filter_members, authors_cache)
        user = await bot.fetch_user(user_id)
        await user.send(text)
    except Exception as exc:
        log.exception("Failed to send report to user %s: %s", user_id, exc)


async def send_morning_routine(
    bot:         commands.Bot,
    report_date: Optional[date] = None,
) -> None:
    """Send personalized reports to every subscriber."""
    today     = datetime.now(UTC2).date()
    yesterday = report_date or previous_workday(today)

    subscribers = get_all_subscribers()
    if not subscribers:
        log.warning("No subscribers found — nobody will receive a morning report.")

    # Fetch data once, scoped to the union of everyone actually tracked, not the whole
    # roster; all subscribers share these same raw numbers
    tracked = sorted({m for members in subscribers.values() for m in members}) or None
    hours, day_offs, week_hours, month_hours, authors = await _collect_report_data(
        bot, yesterday, tracked
    )

    for user_id, members in subscribers.items():
        await _deliver_report(bot, user_id, yesterday, hours, week_hours, month_hours,
                              day_offs, members, authors)


async def send_reminders(bot: commands.Bot, day: date) -> None:
    """DM everyone (from subscriptions with reminders on) who hasn't posted a report on *day*."""
    links    = get_discord_links()
    sub_ids  = get_reminder_subscribers()
    tracked  = sorted({m for sub_id in sub_ids for m in get_subscription(sub_id)}) or None
    day_offs = await fetch_day_offs(day, tracked)
    cache:   dict[int, Optional[set[int]]] = {}
    missing: dict[int, set[int]] = {}          # person's discord id → channels they missed

    for sub_id in sub_ids:
        channel_id = get_reports_channel(sub_id)
        if not channel_id:
            continue
        if channel_id not in cache:
            cache[channel_id] = await fetch_report_authors(bot, day, channel_id)
        authors = cache[channel_id]
        if authors is None:
            continue
        for member in get_subscription(sub_id):
            uid = links.get(member)
            if uid and member not in day_offs and uid not in authors:
                missing.setdefault(uid, set()).add(channel_id)

    for uid, channels in missing.items():
        where = ", ".join(f"<#{c}>" for c in sorted(channels))
        try:
            user = await bot.fetch_user(uid)
            await user.send(
                f"Hey! I don't see your daily report for today ({MONTHS[day.month - 1]} {day.day}) "
                f"in {where}.\n-# A report counts up until 23:59 UTC+2."
            )
        except Exception as exc:
            log.exception("Failed to send reminder to %s: %s", uid, exc)
    log.info("Reminders for %s: sent to %d people", day, len(missing))


async def send_midday_alert(bot: commands.Bot, day: date) -> None:
    """DM each subscriber about people in their subscription who have 0 hours logged so
    far today and aren't on a day off. Doesn't mean something's wrong — maybe they just
    haven't started yet, or Renormalize hasn't synced a day off/sick leave — but it's
    worth a quick check rather than finding out tomorrow morning.
    """
    subscribers = get_all_subscribers()
    tracked     = sorted({m for members in subscribers.values() for m in members}) or None

    try:
        hours = await fetch_hours(day, tracked)
    except Exception as exc:
        log.exception("send_midday_alert: fetch_hours failed: %s", exc)
        return
    day_offs = await fetch_day_offs(day, tracked)
    all_ids  = _all_renormalize_ids()

    sent = 0
    for user_id, members in subscribers.items():
        not_started = [
            m for m in members
            if all_ids.get(m)                      # has an hours target — skip report-only people
            and m not in day_offs
            and hours.get(m, (0.0, 0.0))[0] <= 0.0
        ]
        if not not_started:
            continue
        try:
            user  = await bot.fetch_user(user_id)
            names = ", ".join(f"**{m}**" for m in not_started)
            await user.send(
                f"⚠️ No hours logged yet today for: {names}.\n"
                f"-# Could be nothing — just checking in case something's off, or "
                f"Renormalize hasn't synced a day off/sick leave for them yet."
            )
            sent += 1
        except Exception as exc:
            log.exception("Failed to send midday alert to %s: %s", user_id, exc)
    log.info("Midday alert for %s: sent to %d subscribers", day, sent)


async def check_report_time(bot: commands.Bot) -> None:
    """
    Called every minute by the scheduler.
    Sends personalized reports to every subscriber whose report time matches now.
    """
    now  = datetime.now(UTC2)
    h, m = now.hour, now.minute
    today     = now.date()
    if today.weekday() >= 5:          # no reports on Saturday / Sunday
        return
    yesterday = previous_workday(today)   # Monday → Friday

    if (h, m) == (REMINDER_HOUR, REMINDER_MINUTE):
        await send_reminders(bot, today)

    if (h, m) == (MIDDAY_HOUR, MIDDAY_MINUTE):
        await send_midday_alert(bot, today)

    user_ids = get_users_for_time(h, m)
    if not user_ids:
        return

    batch   = {user_id: get_subscription(user_id) for user_id in user_ids}
    tracked = sorted({m for members in batch.values() for m in members}) or None

    # --- Fetch data once for this batch of subscribers, scoped to who they actually track ---
    hours, day_offs, week_hours, month_hours, authors = await _collect_report_data(
        bot, yesterday, tracked
    )

    for user_id, members in batch.items():
        if members:
            await _deliver_report(
                bot, user_id, yesterday, hours, week_hours, month_hours, day_offs, members, authors
            )
