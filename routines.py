"""Core routine: collecting data, building/delivering reports, scheduled checks."""

from datetime import date, datetime
from typing import Optional

from discord.ext import commands

from config import MEMBER_NAMES, PM_USER_ID, REMINDER_HOUR, REMINDER_MINUTE, UTC3, log
from db import (
    get_all_subscribers,
    get_day_offs,
    get_discord_links,
    get_preference,
    get_reminder_subscribers,
    get_reports_channel,
    get_subscription,
    get_users_for_time,
)
from renormalize import fetch_hours, fetch_week_hours
from reports.authors import fetch_report_authors
from reports.formatting import MONTHS, format_daily_report, format_weekly_report
from ui.dayoff import DayOffView
from utils import previous_workday, week_start


async def _collect_report_data(
    bot:         commands.Bot,
    report_date: date,
) -> tuple[dict[str, float], set[str], Optional[dict[str, float]], dict]:
    """Fetch (hours, day_offs, week_hours, authors_cache) once for all subscribers.

    week_hours is fetched only when *report_date* is Friday (weekly summary day).
    authors_cache starts empty and is filled per reports channel in _deliver_report,
    so each channel is read once even when several subscribers share it.
    """
    try:
        hours = await fetch_hours(report_date)
    except Exception as exc:
        log.exception("fetch_hours failed: %s", exc)
        hours = {name: 0.0 for name in MEMBER_NAMES}

    day_offs = get_day_offs(report_date)

    week_hours: Optional[dict[str, float]] = None
    if report_date.weekday() == 4:
        try:
            week_hours = await fetch_week_hours(week_start(report_date))
        except Exception as exc:
            log.exception("fetch_week_hours failed: %s", exc)
            week_hours = {name: 0.0 for name in MEMBER_NAMES}

    return hours, day_offs, week_hours, {}


async def _build_report_text(
    bot:            commands.Bot,
    user_id:        int,
    report_date:    date,
    hours:          dict[str, float],
    week_hours:     Optional[dict[str, float]],
    day_offs:       set[str],
    filter_members: Optional[list[str]],
    authors_cache:  dict,
) -> str:
    """Daily report (+ weekly progress on Fridays) for one subscriber, using their reports channel."""
    channel_id = get_reports_channel(user_id)
    if channel_id not in authors_cache:
        authors_cache[channel_id] = await fetch_report_authors(bot, report_date, channel_id)
    report_authors = authors_cache[channel_id]

    text = format_daily_report(report_date, hours, day_offs, filter_members, report_authors)
    if week_hours is not None:
        weekly_text = format_weekly_report(week_start(report_date), week_hours, filter_members)
        if weekly_text:
            text += "\n\n" + weekly_text
    return text


async def _deliver_report(
    bot:            commands.Bot,
    user_id:        int,
    report_date:    date,
    hours:          dict[str, float],
    week_hours:     Optional[dict[str, float]],   # None → no weekly section
    day_offs:       set[str],
    filter_members: Optional[list[str]],
    authors_cache:  dict,                         # {channel_id: authors} shared within one run
) -> None:
    """Fetch user and send them the daily DM (+ weekly progress on Fridays)."""
    try:
        text = await _build_report_text(bot, user_id, report_date, hours, week_hours,
                                        day_offs, filter_members, authors_cache)
        user = await bot.fetch_user(user_id)
        await user.send(text)
    except Exception as exc:
        log.exception("Failed to send report to user %s: %s", user_id, exc)


async def send_morning_routine(
    bot:         commands.Bot,
    report_date: Optional[date] = None,
) -> None:
    """
    Send personalized reports to every subscriber, then
    send the day-off selector to the PM.
    """
    today     = datetime.now(UTC3).date()
    yesterday = report_date or previous_workday(today)

    # Fetch data once; all subscribers share the same raw numbers
    hours, day_offs, week_hours, authors = await _collect_report_data(bot, yesterday)

    # --- reports to all subscribers ---
    subscribers = get_all_subscribers()
    if not subscribers:
        log.warning("No subscribers found — nobody will receive a morning report.")

    for user_id, members in subscribers.items():
        await _deliver_report(bot, user_id, yesterday, hours, week_hours, day_offs, members, authors)

    # --- day-off selector to PM only ---
    try:
        pm   = await bot.fetch_user(PM_USER_ID)
        view = DayOffView(today)
        await pm.send(
            f"📅 **Who's off today ({today.strftime('%d.%m.%Y')})?**",
            view=view,
        )
    except Exception as exc:
        log.exception("Failed to send day-off selector to PM: %s", exc)


async def send_reminders(bot: commands.Bot, day: date) -> None:
    """DM everyone (from subscriptions with reminders on) who hasn't posted a report on *day*."""
    links    = get_discord_links()
    day_offs = get_day_offs(day)
    cache:   dict[int, Optional[set[int]]] = {}
    missing: dict[int, set[int]] = {}          # person's discord id → channels they missed

    for sub_id in get_reminder_subscribers():
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
                f"in {where}.\n-# A report counts up until 23:59 UTC+3."
            )
        except Exception as exc:
            log.exception("Failed to send reminder to %s: %s", uid, exc)
    log.info("Reminders for %s: sent to %d people", day, len(missing))


async def check_report_time(bot: commands.Bot) -> None:
    """
    Called every minute by the scheduler.
    Sends personalized reports to every subscriber whose report time matches now,
    and sends the day-off selector to the PM at the PM's configured time.
    """
    now  = datetime.now(UTC3)
    h, m = now.hour, now.minute
    today     = now.date()
    if today.weekday() >= 5:          # no reports on Saturday / Sunday
        return
    yesterday = previous_workday(today)   # Monday → Friday

    if (h, m) == (REMINDER_HOUR, REMINDER_MINUTE):
        await send_reminders(bot, today)

    user_ids              = get_users_for_time(h, m)
    pm_h, pm_m            = get_preference(PM_USER_ID)
    is_pm_time            = (h == pm_h and m == pm_m)

    if not user_ids and not is_pm_time:
        return

    # --- Fetch data once for all subscribers at this time slot ---
    if user_ids:
        hours, day_offs, week_hours, authors = await _collect_report_data(bot, yesterday)

        for user_id in user_ids:
            members = get_subscription(user_id)
            if members:
                await _deliver_report(
                    bot, user_id, yesterday, hours, week_hours, day_offs, members, authors
                )

    # --- Day-off selector → PM (at PM's configured time) ---
    if is_pm_time:
        try:
            pm   = await bot.fetch_user(PM_USER_ID)
            view = DayOffView(today)
            await pm.send(
                f"📅 **Who's off today ({today.strftime('%d.%m.%Y')})?**",
                view=view,
            )
        except Exception as exc:
            log.exception("Failed to send day-off selector to PM: %s", exc)
