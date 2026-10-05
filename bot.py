"""
Discord bot: daily team hours reports with day-off tracking and per-user subscriptions.

Morning routine (per-user configured time, default 09:00 UTC+3, Mon–Fri only):
  1. Personalized report for the PREVIOUS WORKDAY (on Monday — for Friday) — sent to every
     subscriber. Lists only their members with an hours shortfall (⚠️/🔴) or no daily report
     in REPORTS_CHANNEL_ID (any message 00:00–23:59 UTC+3). People on a day off are skipped.
     Weekly progress is appended only to the report for Friday.
  2. Day-off selector for TODAY — sent to PM only (at PM's configured time).

Commands:
  !subscribe              — choose which team members appear in your daily reports
  !settime [HH:MM]        — set your daily report time (UTC+3). No arg = show current.
  !report                 — trigger your personalized morning report right now
  !dayoff [DD.MM]         — open day-off selector for a specific date (PM only)
  !weekly                 — show current-week progress for your subscribed members
  !members                — list all people available for tracking (with Discord links)
  !linkdiscord <name> <@user|nick|id> — link a member to Discord (for daily-report checks)
  !addmember <id> <name>  — add a person by Renormalize ID (visible to everyone)
  !removemember <id>      — remove a custom member (PM only)
  !findmembers            — list all Renormalize workspace members (PM only)
"""

import asyncio
import logging
import os
import random
import re
import sqlite3
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

load_dotenv()

TOKEN             = os.getenv("DISCORD_BOT_TOKEN", "")
PM_USER_ID        = int(os.getenv("PM_USER_ID", "0"))
# Support both variable names (RENORMALIZE_TOKEN is the real JWT, RENORMALIZE_API_KEY is legacy)
RENORMALIZE_API_KEY = os.getenv("RENORMALIZE_TOKEN") or os.getenv("RENORMALIZE_API_KEY", "")
# Channel with daily reports (text channel or forum). 0 → report check is disabled.
REPORTS_CHANNEL_ID = int(os.getenv("REPORTS_CHANNEL_ID") or 0)

MOSCOW  = ZoneInfo("Europe/Moscow")
DB_PATH = os.getenv("DB_PATH") or "hours.db"   # on Railway point it to the Volume, e.g. /data/hours.db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Team roster
# ---------------------------------------------------------------------------

# (display_name_ru, display_name_en, daily_target_hours, weekly_target_hours)
TEAM: list[tuple[str, str, float, float]] = [
    ("Лёша Седин",          "Aleksey Siedin",        8.0, 40.0),
    ("Лёша Думалин",        "Alexey Dumailenko",     8.0, 40.0),
    ("Самвел",              "Samvel Hovhannisyan",   8.0, 40.0),
    ("Андрей Соколовский",  "Andrii Sokolovskyi",    8.0, 40.0),
    ("Давид",               "David Dohru",           8.0, 40.0),
    ("Георгий",             "George Kokashvilli",    8.0, 40.0),
    ("Сергей Безруков",     "Sergii Bezrukov",       5.0, 25.0),
    ("Станислав Селиванов", "Stanislav Selivanov",   2.0, 10.0),
]

MEMBER_NAMES   = [m[0] for m in TEAM]
DAILY_TARGET:  dict[str, float] = {m[0]: m[2] for m in TEAM}
WEEKLY_TARGET: dict[str, float] = {m[0]: m[3] for m in TEAM}

# Renormalize user IDs — найти их можно командой !findmembers
# или вручную: открой отчёт сотрудника в Renormalize, ID в URL: ?id=XXXXX
RENORMALIZE_IDS: dict[str, Optional[int]] = {
    "Лёша Седин":          76544,
    "Лёша Думалин":        76542,
    "Самвел":              76632,
    "Андрей Соколовский":  76607,
    "Давид":               76537,
    "Георгий":             76536,
    "Сергей Безруков":     76718,
    "Станислав Селиванов": 76657,
}

# ---------------------------------------------------------------------------
# Data layer — SQLite
# ---------------------------------------------------------------------------

def init_db() -> None:
    if os.path.dirname(DB_PATH):
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS day_offs (
                member TEXT NOT NULL,
                day    TEXT NOT NULL,   -- ISO YYYY-MM-DD
                PRIMARY KEY (member, day)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                discord_user_id INTEGER NOT NULL,
                member          TEXT    NOT NULL,
                PRIMARY KEY (discord_user_id, member)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS preferences (
                discord_user_id INTEGER PRIMARY KEY,
                report_hour     INTEGER NOT NULL DEFAULT 9,
                report_minute   INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS custom_members (
                renormalize_id INTEGER PRIMARY KEY,
                display_name   TEXT    NOT NULL,
                daily_hours    REAL    NOT NULL DEFAULT 8.0,
                weekly_hours   REAL    NOT NULL DEFAULT 40.0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS discord_links (
                member          TEXT    PRIMARY KEY,
                discord_user_id INTEGER NOT NULL
            )
            """
        )
        conn.commit()


# --- day-off helpers --------------------------------------------------------

def save_day_offs(members: list[str], day: date) -> None:
    day_str = day.isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM day_offs WHERE day = ?", (day_str,))
        conn.executemany(
            "INSERT OR IGNORE INTO day_offs (member, day) VALUES (?, ?)",
            [(m, day_str) for m in members],
        )
        conn.commit()


def get_day_offs(day: date) -> set[str]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT member FROM day_offs WHERE day = ?", (day.isoformat(),)
        ).fetchall()
    return {r[0] for r in rows}


# --- subscription helpers ---------------------------------------------------

def save_subscription(user_id: int, members: list[str]) -> None:
    """Replace all subscription entries for *user_id* with *members*."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "DELETE FROM subscriptions WHERE discord_user_id = ?", (user_id,)
        )
        conn.executemany(
            "INSERT INTO subscriptions (discord_user_id, member) VALUES (?, ?)",
            [(user_id, m) for m in members],
        )
        conn.commit()


def get_subscription(user_id: int) -> list[str]:
    """Return the list of member names this Discord user subscribes to."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT member FROM subscriptions WHERE discord_user_id = ? ORDER BY rowid",
            (user_id,),
        ).fetchall()
    return [r[0] for r in rows]


def get_all_subscribers() -> dict[int, list[str]]:
    """Return {discord_user_id: [member, ...]} for all users with ≥1 subscription."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT discord_user_id, member FROM subscriptions ORDER BY discord_user_id, rowid"
        ).fetchall()
    result: dict[int, list[str]] = {}
    for uid, member in rows:
        result.setdefault(uid, []).append(member)
    return result


# --- custom member helpers --------------------------------------------------

def add_custom_member(renorm_id: int, display_name: str,
                      daily: float = 8.0, weekly: float = 40.0) -> None:
    """Add or update a person in the shared member pool."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO custom_members (renormalize_id, display_name, daily_hours, weekly_hours)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(renormalize_id) DO UPDATE SET
                display_name = excluded.display_name,
                daily_hours  = excluded.daily_hours,
                weekly_hours = excluded.weekly_hours
            """,
            (renorm_id, display_name, daily, weekly),
        )
        conn.commit()


def remove_custom_member(renorm_id: int) -> bool:
    """Remove a custom member. Returns True if something was deleted."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            "DELETE FROM custom_members WHERE renormalize_id = ?", (renorm_id,)
        )
        conn.commit()
        return cursor.rowcount > 0


def get_custom_members() -> list[tuple[int, str, float, float]]:
    """Return [(renormalize_id, display_name, daily_h, weekly_h)] from DB."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT renormalize_id, display_name, daily_hours, weekly_hours "
            "FROM custom_members ORDER BY rowid"
        ).fetchall()
    return [(r[0], r[1], float(r[2]), float(r[3])) for r in rows]


def _all_members() -> list[tuple[str, str, float, float]]:
    """Return TEAM + custom members as (name, en_name, daily_h, weekly_h)."""
    result: list[tuple[str, str, float, float]] = list(TEAM)
    for renorm_id, name, daily, weekly in get_custom_members():
        result.append((name, name, daily, weekly))
    return result


def _all_renormalize_ids() -> dict[str, Optional[int]]:
    """Return RENORMALIZE_IDS merged with custom member IDs."""
    result: dict[str, Optional[int]] = dict(RENORMALIZE_IDS)
    for renorm_id, name, _, _ in get_custom_members():
        result[name] = renorm_id
    return result


# --- discord link helpers ---------------------------------------------------

def save_discord_link(member: str, discord_user_id: int) -> None:
    """Link a member (by display name) to a Discord user ID."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO discord_links (member, discord_user_id) VALUES (?, ?)
            ON CONFLICT(member) DO UPDATE SET discord_user_id = excluded.discord_user_id
            """,
            (member, discord_user_id),
        )
        conn.commit()


def get_discord_links() -> dict[str, int]:
    """Return {member: discord_user_id}."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT member, discord_user_id FROM discord_links").fetchall()
    return {r[0]: r[1] for r in rows}


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


async def _resolve_discord_user(ctx: commands.Context, spec: str) -> Optional[discord.abc.User]:
    """Resolve a mention, numeric ID or username (with or without @) to a Discord user.

    In DMs "@nick" stays plain text (no real mention), so we also search the bot's servers.
    """
    if ctx.message.mentions:
        return ctx.message.mentions[0]
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


# --- preference helpers (report time per user) ------------------------------

def save_preference(user_id: int, hour: int, minute: int) -> None:
    """Save or update the user's daily report time (stored as UTC+3)."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO preferences (discord_user_id, report_hour, report_minute)
            VALUES (?, ?, ?)
            ON CONFLICT(discord_user_id) DO UPDATE SET
                report_hour   = excluded.report_hour,
                report_minute = excluded.report_minute
            """,
            (user_id, hour, minute),
        )
        conn.commit()


def get_preference(user_id: int) -> tuple[int, int]:
    """Return (hour, minute) for this user's report time. Default: 9:00."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT report_hour, report_minute FROM preferences WHERE discord_user_id = ?",
            (user_id,),
        ).fetchone()
    return (row[0], row[1]) if row else (9, 0)


def get_users_for_time(hour: int, minute: int) -> list[int]:
    """Return Discord user IDs of subscribers whose report fires at hour:minute (UTC+3).

    Users who never ran !settime default to 9:00 and are included when hour=9, minute=0.
    """
    with sqlite3.connect(DB_PATH) as conn:
        # Users who explicitly set this time
        explicit = conn.execute(
            """
            SELECT DISTINCT s.discord_user_id
            FROM subscriptions s
            JOIN preferences p ON s.discord_user_id = p.discord_user_id
            WHERE p.report_hour = ? AND p.report_minute = ?
            """,
            (hour, minute),
        ).fetchall()
        result = [r[0] for r in explicit]

        # At 9:00 also include users with no preference row (default = 9:00)
        if hour == 9 and minute == 0:
            default_users = conn.execute(
                """
                SELECT DISTINCT s.discord_user_id
                FROM subscriptions s
                WHERE s.discord_user_id NOT IN (SELECT discord_user_id FROM preferences)
                """
            ).fetchall()
            for r in default_users:
                if r[0] not in result:
                    result.append(r[0])
    return result


def week_start(d: date) -> date:
    """Return the Monday of the week containing *d*."""
    return d - timedelta(days=d.weekday())


def previous_workday(d: date) -> date:
    """Return the last Mon–Fri day before *d* (Monday → Friday)."""
    prev = d - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    return prev


# ---------------------------------------------------------------------------
# Hours data source
# ---------------------------------------------------------------------------

def _mock_hours() -> dict[str, float]:
    """Fallback: random hours near each person's daily target."""
    result: dict[str, float] = {}
    for name, _en, daily, _ in _all_members():
        hours = random.gauss(daily, 1.5)
        result[name] = round(max(0.0, min(daily + 2, hours)), 2)
    return result


async def fetch_hours(target_date: date) -> dict[str, float]:
    """
    Return hours worked by each team member on *target_date*.

    Real data comes from the Renormalize API (api.renormalize.com).
    Members without a Renormalize ID in RENORMALIZE_IDS fall back to mock data.
    If RENORMALIZE_API_KEY is not set, all members use mock data.

    API endpoint (discovered from browser network tab):
      GET https://api.renormalize.com/charts/time-use-report
      Headers: Authorization: Bearer {RENORMALIZE_API_KEY}
      Params:  start_at, end_at (YYYY-MM-DD), entity_id, entity_type=engineer, page_size=10000
      Response: {"data": [{"time_by_date": [{"date": "...", "total_auto_time": <seconds>,
                                             "total_manual_time": <seconds>}]}]}
    """
    if not RENORMALIZE_API_KEY:
        log.warning("RENORMALIZE_API_KEY not set — using mock data")
        return _mock_hours()

    results:  dict[str, float] = {}
    date_str  = target_date.isoformat()
    # end_at is EXCLUSIVE in the API — use target_date + 1 to include target_date entries
    end_at    = (target_date + timedelta(days=1)).isoformat()

    import httpx

    all_ids = _all_renormalize_ids()
    async with httpx.AsyncClient() as client:
        headers = {"Authorization": f"Bearer {RENORMALIZE_API_KEY}"}

        for name, _en, daily, _ in _all_members():
            renorm_id = all_ids.get(name)
            if renorm_id is None:
                hours = random.gauss(daily, 1.5)
                results[name] = round(max(0.0, min(daily + 2, hours)), 2)
                continue

            try:
                resp = await client.get(
                    "https://api.renormalize.com/v1/time/progression",
                    params={
                        "user_ids": str(renorm_id),
                        "start_at": date_str,
                        "end_at":   end_at,
                    },
                    headers=headers,
                    timeout=15,
                )
                resp.raise_for_status()
                entries = resp.json().get(str(renorm_id), [])
                # Filter by date field (confirmed reliable by testapi)
                total_sec = sum(
                    e.get("total_time", 0)
                    for e in entries
                    if e.get("date") == date_str
                )
                results[name] = round(total_sec / 3600, 2)
                log.info("%s on %s: %.2fh (%d entries)", name, date_str, results[name], len(entries))

            except httpx.HTTPStatusError as exc:
                log.error("Renormalize %s for %s: %s", exc.response.status_code, name, exc.response.text[:100])
                results[name] = 0.0
            except Exception as exc:
                log.exception("fetch_hours failed for %s: %s", name, exc)
                results[name] = 0.0

    return results


def _sum_entries(data: dict, user_id: int) -> float:
    """Sum all total_time seconds for a user in the API response (no date filter)."""
    return float(sum(
        e.get("total_time", 0)
        for e in data.get(str(user_id), [])
    ))


async def fetch_week_hours(week_begin: date) -> dict[str, float]:
    """
    Return total hours worked per team member for the week starting *week_begin*.
    Makes 1 API call per member (not 7) for efficiency.
    """
    today      = datetime.now(MOSCOW).date()
    end        = min(week_begin + timedelta(days=6), today)
    all_m      = _all_members()
    all_ids    = _all_renormalize_ids()
    totals: dict[str, float] = {m[0]: 0.0 for m in all_m}

    if not RENORMALIZE_API_KEY:
        for name, _en, daily, weekly in all_m:
            totals[name] = round(random.gauss(weekly * 0.85, weekly * 0.1), 2)
        return totals

    import httpx
    async with httpx.AsyncClient() as client:
        headers = {"Authorization": f"Bearer {RENORMALIZE_API_KEY}"}
        for name, _en, _, _ in all_m:
            renorm_id = all_ids.get(name)
            if renorm_id is None:
                continue
            try:
                resp = await client.get(
                    "https://api.renormalize.com/v1/time/progression",
                    params={
                        "user_ids": str(renorm_id),
                        "start_at": week_begin.isoformat(),
                        "end_at":   (end + timedelta(days=1)).isoformat(),  # exclusive → +1
                    },
                    headers=headers,
                    timeout=15,
                )
                resp.raise_for_status()
                entries = resp.json().get(str(renorm_id), [])
                total_sec = sum(e.get("total_time", 0) for e in entries)
                totals[name] = round(total_sec / 3600, 2)
            except Exception as exc:
                log.exception("fetch_week_hours failed for %s: %s", name, exc)

    return totals


# ---------------------------------------------------------------------------
# Daily reports in Discord
# ---------------------------------------------------------------------------

async def fetch_report_authors(bot: commands.Bot, target_date: date) -> Optional[set[int]]:
    """
    Return Discord IDs of everyone who posted in REPORTS_CHANNEL_ID on *target_date*
    (00:00–23:59 UTC+3). Any non-bot message counts as a report.

    Text channel → channel history. Forum → messages in all posts (active + archived).
    Returns None if the channel is not configured or can't be read (check is skipped).
    """
    if not REPORTS_CHANNEL_ID:
        return None

    start = datetime.combine(target_date, datetime.min.time(), tzinfo=MOSCOW)
    end   = start + timedelta(days=1)
    authors: set[int] = set()

    async def collect(messageable) -> None:
        async for msg in messageable.history(after=start, before=end, limit=None):
            if not msg.author.bot:
                authors.add(msg.author.id)

    try:
        channel = bot.get_channel(REPORTS_CHANNEL_ID) or await bot.fetch_channel(REPORTS_CHANNEL_ID)
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

    log.info("Daily reports on %s: %d authors", target_date, len(authors))
    return authors


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def status_emoji(worked: float, target: float, day_off: bool) -> str:
    if day_off:
        return "✅"
    diff = worked - target
    if diff >= 0:      return "✅"
    if diff >= -1:     return "🟡"
    return "🔴"


MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]
WEEKDAYS   = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]


def _h(hours: float) -> str:
    """6.8 → '6,8' (Russian decimal comma)."""
    return f"{hours:.1f}".replace(".", ",")


def _day_title(d: date) -> str:
    """date(2026, 10, 2) → 'Пятница, 2 октября'."""
    return f"{WEEKDAYS[d.weekday()]}, {d.day} {MONTHS_GEN[d.month - 1]}"


def format_daily_report(
    report_date:    date,
    hours:          dict[str, float],
    day_offs:       set[str],
    filter_members: Optional[list[str]] = None,   # None → all members
    report_authors: Optional[set[int]] = None,    # None → report check disabled
) -> str:
    """Show only members with an hours shortfall or a missing daily report."""
    header = f"### {_day_title(report_date)}\n"

    active = [(n, d, w) for n, _en, d, w in _all_members()
              if filter_members is None or n in filter_members]

    links = get_discord_links()

    lines:    list[str] = []
    unlinked: list[str] = []
    for name, daily, _ in active:
        if name in day_offs:
            continue                      # day off is not a problem
        worked    = hours.get(name, 0.0)
        emoji     = status_emoji(worked, daily, False)
        uid       = links.get(name)
        no_report = report_authors is not None and uid is not None and uid not in report_authors
        if report_authors is not None and uid is None:
            unlinked.append(name)
        if emoji == "✅" and not no_report:
            continue
        marker = "🟡" if emoji == "✅" else emoji
        line   = f"{marker} **{name}** · {_h(worked)} из {daily:g} ч"
        if no_report:
            line += " · нет отчёта"
        lines.append(line)

    ok = len(active) - len(lines)
    if not lines:
        lines.append("Замечаний нет" if ok == 1 else f"Замечаний нет — все {ok} в норме")
    elif ok:
        lines.append(f"-# Остальные {ok} — без замечаний")
    if unlinked:
        lines.append(
            f"-# Отчёт не проверен, нет привязки Discord: {', '.join(unlinked)}. "
            f"Привязать: `!linkdiscord <имя> <ID>`"
        )

    return header + "\n".join(lines)


def format_weekly_report(
    week_begin:     date,
    week_hours:     dict[str, float],
    filter_members: Optional[list[str]] = None,   # None → all members
) -> str:
    header = f"### Неделя с {week_begin.day} {MONTHS_GEN[week_begin.month - 1]}\n"

    active = [(n, d, w) for n, _en, d, w in _all_members()
              if filter_members is None or n in filter_members]

    lines: list[str] = []
    for name, _, weekly in active:
        done      = week_hours.get(name, 0.0)
        remaining = max(weekly - done, 0.0)
        pct       = int(min(done / weekly, 1.0) * 100) if weekly else 0
        tail      = f"осталось {_h(remaining)} ч" if remaining > 0 else "норма выполнена"
        lines.append(f"**{name}** · {_h(done)} из {weekly:g} ч · {pct}%\n-# {tail}")

    return header + "\n".join(lines)


# ---------------------------------------------------------------------------
# Discord UI — subscription selector
# ---------------------------------------------------------------------------

def _subscribe_prompt(current: list[str]) -> str:
    if current:
        return f"📋 **Твоя подписка:** {', '.join(current)}\n\nВыбери, кого хочешь отслеживать:"
    return "📋 У тебя пока нет подписки.\nВыбери сотрудников, чьи часы ты хочешь видеть:"


SELECT_LIMIT      = 25   # Discord: max options per select
MAX_SELECTS       = 4    # Discord: 5 rows per view, one is taken by the buttons


def _member_chunks(context: str) -> list[list[tuple[str, str, float, float]]]:
    """Split all members into chunks of SELECT_LIMIT (at most MAX_SELECTS chunks)."""
    all_m = _all_members()
    if len(all_m) > SELECT_LIMIT * MAX_SELECTS:
        log.warning("Too many members (%d) — only first %d shown in %s",
                    len(all_m), SELECT_LIMIT * MAX_SELECTS, context)
    return [all_m[i:i + SELECT_LIMIT]
            for i in range(0, min(len(all_m), SELECT_LIMIT * MAX_SELECTS), SELECT_LIMIT)]


def _chunk_placeholder(default: str, i: int, chunk: list, total_chunks: int) -> str:
    if total_chunks == 1:
        return default
    return f"Сотрудники {i * SELECT_LIMIT + 1}–{i * SELECT_LIMIT + len(chunk)}…"


class SubscribeSelect(discord.ui.Select):
    def __init__(self, current: list[str],
                 members: list[tuple[str, str, float, float]], placeholder: str) -> None:
        all_ids = _all_renormalize_ids()
        options = [
            discord.SelectOption(
                label=f"{en_name}  (id {all_ids.get(name, '?')})",
                value=name,
                default=name in current,
            )
            for name, en_name, _, _ in members
        ]
        super().__init__(
            placeholder=placeholder,
            min_values=0,
            max_values=len(options),
            options=options,
        )
        self.touched = False

    @property
    def chosen(self) -> list[str]:
        """Selected values; untouched select keeps its defaults (values would be empty)."""
        if self.touched:
            return list(self.values)
        return [o.value for o in self.options if o.default]

    async def callback(self, interaction: discord.Interaction) -> None:
        self.touched = True
        await interaction.response.defer()


class EditSubscriptionView(discord.ui.View):
    """Shown after saving — gives the user a chance to edit again."""

    def __init__(self, user_id: int) -> None:
        super().__init__(timeout=300)
        self.user_id = user_id

    @discord.ui.button(label="Изменить подписку", style=discord.ButtonStyle.secondary, emoji="✏️")
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        current = get_subscription(self.user_id)
        view    = SubscribeView(self.user_id)
        self.stop()
        await interaction.response.edit_message(
            content=_subscribe_prompt(current),
            view=view,
        )


class SubscribeView(discord.ui.View):
    def __init__(self, user_id: int) -> None:
        super().__init__(timeout=300)
        self.user_id = user_id
        current      = get_subscription(user_id)
        chunks       = _member_chunks("!subscribe")
        self.selects: list[SubscribeSelect] = []
        for i, chunk in enumerate(chunks):
            placeholder = _chunk_placeholder(
                "Выберите сотрудников для отслеживания…", i, chunk, len(chunks))
            select = SubscribeSelect(current, chunk, placeholder)
            select.row = i
            self.selects.append(select)
            self.add_item(select)

    @discord.ui.button(label="Сохранить", style=discord.ButtonStyle.success, emoji="💾", row=4)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        chosen = [n for s in self.selects for n in s.chosen]
        save_subscription(self.user_id, chosen)
        self.stop()

        if chosen:
            bullet_list = "\n".join(f"  • {n}" for n in chosen)
            h, m = get_preference(self.user_id)
            msg = (
                f"✅ **Подписка сохранена!**\n\n"
                f"Будешь получать отчёты по:\n{bullet_list}\n\n"
                f"⏰ Время отчёта: **{h:02d}:{m:02d} UTC+3**.\n"
                f"Изменить время: `!settime HH:MM`  (например, `!settime 08:30`)"
            )
        else:
            msg = (
                "⚠️ Подписка пустая — ты не будешь получать утренние отчёты.\n"
                "Нажми «Изменить подписку», чтобы добавить людей."
            )

        await interaction.response.edit_message(
            content=msg,
            view=EditSubscriptionView(self.user_id),
        )


# ---------------------------------------------------------------------------
# Discord UI — day-off selector  (PM only)
# ---------------------------------------------------------------------------

class DayOffSelect(discord.ui.Select):
    def __init__(self, target_date: date,
                 members: list[tuple[str, str, float, float]], placeholder: str) -> None:
        self.target_date = target_date
        super().__init__(
            placeholder=placeholder,
            min_values=0,
            max_values=len(members),
            options=[
                discord.SelectOption(label=f"{en_name} ({name})", value=name)
                for name, en_name, _, _ in members
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()


class DayOffView(discord.ui.View):
    def __init__(self, target_date: date) -> None:
        super().__init__(timeout=600)
        self.target_date = target_date
        chunks           = _member_chunks("day-off selector")
        self.selects: list[DayOffSelect] = []
        for i, chunk in enumerate(chunks):
            select = DayOffSelect(
                target_date, chunk, _chunk_placeholder("Выберите сотрудников…", i, chunk, len(chunks)))
            select.row = i
            self.selects.append(select)
            self.add_item(select)

    @discord.ui.button(label="Сохранить", style=discord.ButtonStyle.success, emoji="💾", row=4)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        chosen = [n for s in self.selects for n in s.values]
        save_day_offs(chosen, self.target_date)
        msg = (
            f"✅ Выходной на {self.target_date.strftime('%d.%m.%Y')} сохранён: {', '.join(chosen)}"
            if chosen else
            f"✅ Выходных на {self.target_date.strftime('%d.%m.%Y')} не сохранено."
        )
        self.stop()
        await interaction.response.edit_message(content=msg, view=None)

    @discord.ui.button(label="Сегодня все работают", style=discord.ButtonStyle.secondary, emoji="🚫", row=4)
    async def no_day_off(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        save_day_offs([], self.target_date)
        self.stop()
        await interaction.response.edit_message(
            content=f"👍 Выходных на {self.target_date.strftime('%d.%m.%Y')} нет.",
            view=None,
        )


# ---------------------------------------------------------------------------
# Core routine
# ---------------------------------------------------------------------------

async def _collect_report_data(
    bot:         commands.Bot,
    report_date: date,
) -> tuple[dict[str, float], set[str], Optional[dict[str, float]], Optional[set[int]]]:
    """Fetch (hours, day_offs, week_hours, report_authors) once for all subscribers.

    week_hours is fetched only when *report_date* is Friday (weekly summary day).
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

    report_authors = await fetch_report_authors(bot, report_date)
    return hours, day_offs, week_hours, report_authors


async def _deliver_report(
    bot:            commands.Bot,
    user_id:        int,
    report_date:    date,
    hours:          dict[str, float],
    week_hours:     Optional[dict[str, float]],   # None → no weekly section
    day_offs:       set[str],
    filter_members: Optional[list[str]],
    report_authors: Optional[set[int]] = None,
) -> None:
    """Fetch user and send them the daily DM (+ weekly progress on Fridays)."""
    try:
        user = await bot.fetch_user(user_id)
        text = format_daily_report(report_date, hours, day_offs, filter_members, report_authors)
        if week_hours is not None:
            text += "\n\n" + format_weekly_report(week_start(report_date), week_hours, filter_members)
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
    today     = datetime.now(MOSCOW).date()
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
            f"📅 **Кто сегодня ({today.strftime('%d.%m.%Y')}) не работает?**",
            view=view,
        )
    except Exception as exc:
        log.exception("Failed to send day-off selector to PM: %s", exc)


async def check_report_time(bot: commands.Bot) -> None:
    """
    Called every minute by the scheduler.
    Sends personalized reports to every subscriber whose report time matches now,
    and sends the day-off selector to the PM at the PM's configured time.
    """
    now  = datetime.now(MOSCOW)
    h, m = now.hour, now.minute
    today     = now.date()
    if today.weekday() >= 5:          # no reports on Saturday / Sunday
        return
    yesterday = previous_workday(today)   # Monday → Friday

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
                f"📅 **Кто сегодня ({today.strftime('%d.%m.%Y')}) не работает?**",
                view=view,
            )
        except Exception as exc:
            log.exception("Failed to send day-off selector to PM: %s", exc)


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True

bot       = commands.Bot(command_prefix="!", intents=intents)
scheduler = AsyncIOScheduler(timezone=MOSCOW)


@bot.event
async def on_ready() -> None:
    log.info("Logged in as %s (id=%s)", bot.user, bot.user.id)  # type: ignore[union-attr]

    init_db()

    scheduler.add_job(
        check_report_time,
        trigger="cron",
        minute="*",          # fires every minute; sends only to users whose time matches
        id="check_report",
        replace_existing=True,
        kwargs={"bot": bot},
    )
    scheduler.start()
    log.info("Scheduler started — checking report times every minute (UTC+3)")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@bot.command(name="subscribe")
async def cmd_subscribe(ctx: commands.Context) -> None:
    """!subscribe — choose which team members appear in your daily reports."""
    user_id = ctx.author.id
    current = get_subscription(user_id)
    view    = SubscribeView(user_id)

    user = await bot.fetch_user(user_id)
    await user.send(_subscribe_prompt(current), view=view)

    if ctx.guild:
        await ctx.message.add_reaction("📨")


@bot.command(name="report")
async def cmd_report(ctx: commands.Context) -> None:
    """!report — trigger your personalized morning report right now."""
    user_id = ctx.author.id
    members = get_subscription(user_id)

    if not members:
        user = await bot.fetch_user(user_id)
        await user.send(
            "⚠️ У тебя нет подписки.\n"
            "Используй `!subscribe`, чтобы выбрать, чьи часы ты хочешь видеть в отчёте."
        )
        if ctx.guild:
            await ctx.message.add_reaction("📨")
        return

    await ctx.message.add_reaction("⏳")

    today     = datetime.now(MOSCOW).date()
    yesterday = previous_workday(today)

    hours, day_offs, week_hours, authors = await _collect_report_data(bot, yesterday)

    await _deliver_report(bot, user_id, yesterday, hours, week_hours, day_offs, members, authors)
    await ctx.message.add_reaction("✅")


@bot.command(name="dayoff")
async def cmd_dayoff(ctx: commands.Context, date_str: Optional[str] = None) -> None:
    """!dayoff [DD.MM] — open day-off selector for a given date (PM only)."""
    if ctx.author.id != PM_USER_ID:
        await ctx.message.add_reaction("🚫")
        return

    if date_str:
        try:
            today  = datetime.now(MOSCOW).date()
            parsed = datetime.strptime(date_str, "%d.%m").replace(year=today.year).date()
        except ValueError:
            await ctx.send("❌ Неверный формат. Пример: `!dayoff 25.05`")
            return
    else:
        parsed = datetime.now(MOSCOW).date()

    pm   = await bot.fetch_user(PM_USER_ID)
    view = DayOffView(parsed)
    await pm.send(f"📅 **Кто {parsed.strftime('%d.%m.%Y')} не работает?**", view=view)

    if ctx.guild:
        await ctx.message.add_reaction("📨")


@bot.command(name="start")
async def cmd_start(ctx: commands.Context) -> None:
    """!start — onboarding: show what this bot does and how to set it up."""
    text = (
        "👋 **Привет! Я слежу за часами команды в Renormalize.**\n"
        "Каждое утро буду присылать тебе в личку отчёт по нужным людям.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "**🚀 Быстрый старт — 3 шага:**\n\n"
        "**1. Посмотри, кто уже есть в списке**\n"
        "```\n!members\n```\n"
        "**2. Если нужного человека нет — добавь его по Renormalize ID**\n"
        "```\n!addmember 12345 Имя Фамилия\n```\n"
        "*(ID найдёшь в URL профиля сотрудника в Renormalize: `?entity_id=XXXXX`)*\n\n"
        "**3. Подпишись на нужных людей и выбери время отчёта**\n"
        "```\n!subscribe\n!settime 09:00\n```\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "**📋 Все команды:**\n\n"
        "`!members` — список всех доступных сотрудников\n"
        "`!addmember <id> <имя>` — добавить человека по Renormalize ID\n"
        "`!subscribe` — выбрать, чьи часы видеть в отчёте\n"
        "`!linkdiscord <имя> @user` — привязать Discord для проверки daily-отчётов\n"
        "`!settime HH:MM` — время ежедневного отчёта (UTC+3, по умолчанию 09:00)\n"
        "`!report` — получить отчёт прямо сейчас\n"
        "`!weekly` — прогресс за текущую неделю\n"
        "`!start` — показать эту инструкцию снова\n"
    )
    user = await bot.fetch_user(ctx.author.id)
    await user.send(text)
    if ctx.guild:
        await ctx.message.add_reaction("📨")


@bot.command(name="weekly")
async def cmd_weekly(ctx: commands.Context) -> None:
    """!weekly — show current-week progress for your subscribed members."""
    user_id = ctx.author.id
    members = get_subscription(user_id)

    if not members:
        user = await bot.fetch_user(user_id)
        await user.send(
            "⚠️ У тебя нет подписки.\n"
            "Используй `!subscribe`, чтобы выбрать, чьи часы ты хочешь видеть."
        )
        if ctx.guild:
            await ctx.message.add_reaction("📨")
        return

    await ctx.message.add_reaction("⏳")

    today      = datetime.now(MOSCOW).date()
    wb         = week_start(today)
    week_hours = await fetch_week_hours(wb)
    text       = format_weekly_report(wb, week_hours, members)

    user = await bot.fetch_user(user_id)
    await user.send(text)

    if ctx.guild:
        await ctx.message.add_reaction("✅")


@bot.command(name="members")
async def cmd_members(ctx: commands.Context) -> None:
    """!members — list all people available for tracking."""
    lines = ["**👥 Все доступные сотрудники:**\n"]
    all_ids = _all_renormalize_ids()
    links   = get_discord_links()

    def link_str(name: str) -> str:
        uid = links.get(name)
        return f" · <@{uid}>" if uid else " · ⛓️‍💥 нет Discord"

    for name, en_name, daily, _ in TEAM:
        rid = all_ids.get(name, "?")
        lines.append(f"`{rid}` — {en_name}  ({daily:.0f}h/day){link_str(name)}")

    custom = get_custom_members()
    if custom:
        lines.append("\n**Добавлены вручную:**")
        for rid, dname, daily, _ in custom:
            lines.append(f"`{rid}` — {dname}  ({daily:.0f}h/day){link_str(dname)}")

    lines.append("\n➕ Добавить: `!addmember <renormalize_id> <имя>`")
    lines.append("🔗 Привязать Discord: `!linkdiscord <имя> @user`")
    # Show mentions without pinging people
    await ctx.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())


@bot.command(name="linkdiscord")
async def cmd_linkdiscord(ctx: commands.Context, *, args: str = "") -> None:
    """!linkdiscord <name> <@user | nick | id> — link a member to their Discord account."""
    parts = args.split()
    if len(parts) < 2:
        await ctx.send(
            "❌ Формат: `!linkdiscord <имя> <@user | ник | Discord ID>`\n"
            "Например: `!linkdiscord Самвел @samvel` или `!linkdiscord Aleksey Siedin 954344819092783184`"
        )
        return

    name, spec = " ".join(parts[:-1]), parts[-1]
    member = _find_member(name)
    if member is None:
        await ctx.send(f"❌ Сотрудник «{name}» не найден (или подходит несколько). Список: `!members`")
        return

    user = await _resolve_discord_user(ctx, spec)
    if user is None:
        await ctx.send(
            f"❌ Не нашёл пользователя Discord «{spec}».\n"
            "Укажи ник (как в профиле, без пробелов) или Discord ID "
            "(ПКМ по человеку → «Копировать ID», нужен режим разработчика)."
        )
        return

    save_discord_link(member, user.id)
    await ctx.send(
        f"✅ **{member}** привязан к {user.mention}.",
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.command(name="addmember")
async def cmd_addmember(ctx: commands.Context, renorm_id: int, *, name: str) -> None:
    """!addmember <renormalize_id> <name> — add a person by their Renormalize ID."""
    name = name.strip()
    if not name:
        await ctx.send("❌ Укажи имя. Пример: `!addmember 12345 Ivan Petrov`")
        return

    add_custom_member(renorm_id, name)
    await ctx.send(
        f"✅ Добавлен: **{name}** (id `{renorm_id}`)\n"
        f"Теперь его можно выбрать через `!subscribe`."
    )


@bot.command(name="removemember")
async def cmd_removemember(ctx: commands.Context, renorm_id: int) -> None:
    """!removemember <renormalize_id> — remove a custom member (PM only)."""
    if ctx.author.id != PM_USER_ID:
        await ctx.message.add_reaction("🚫")
        return

    deleted = remove_custom_member(renorm_id)
    if deleted:
        await ctx.send(f"✅ Сотрудник с id `{renorm_id}` удалён из списка.")
    else:
        await ctx.send(f"⚠️ Сотрудник с id `{renorm_id}` не найден среди добавленных вручную.")


@bot.command(name="settime")
async def cmd_settime(ctx: commands.Context, time_str: Optional[str] = None) -> None:
    """!settime [HH:MM] — set your daily report time (UTC+3). No arg = show current."""
    if time_str is None:
        h, m = get_preference(ctx.author.id)
        await ctx.send(
            f"🕐 Твоё текущее время отчёта: **{h:02d}:{m:02d} UTC+3**.\n"
            f"Изменить: `!settime 08:30`"
        )
        return

    try:
        parts  = time_str.strip().split(":")
        hour   = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("out of range")
    except (ValueError, IndexError):
        await ctx.send("❌ Неверный формат. Пример: `!settime 09:00` или `!settime 8:30`")
        return

    save_preference(ctx.author.id, hour, minute)
    await ctx.send(
        f"✅ Время ежедневного отчёта установлено: **{hour:02d}:{minute:02d} UTC+3**.\n"
        f"Если ещё не выбрал сотрудников — используй `!subscribe`."
    )


@bot.command(name="testapi")
async def cmd_test_api(ctx: commands.Context) -> None:
    """!testapi — test Renormalize API with Samvel's ID, show raw response (PM only)."""
    if ctx.author.id != PM_USER_ID:
        await ctx.message.add_reaction("🚫")
        return

    await ctx.message.add_reaction("⏳")
    import httpx

    today    = datetime.now(MOSCOW).date()
    test_id  = 76632  # Samvel
    date_str = (today - timedelta(days=1)).isoformat()

    results: list[str] = []
    async with httpx.AsyncClient() as client:
        headers = {"Authorization": f"Bearer {RENORMALIZE_API_KEY}"}
        base    = "https://api.renormalize.com/v1/time/progression"

        # Test Alexey Dumailenko (76542) — UI shows 9h38m on Jun 2, bot shows 6.8h
        alexey_id = 76542

        # Approach A: wide range Jun 1→Jun 3, filter by time_start date
        try:
            resp = await client.get(
                base,
                params={"user_ids": str(alexey_id), "start_at": "2026-06-01", "end_at": "2026-06-03"},
                headers=headers, timeout=10,
            )
            all_entries = resp.json().get(str(alexey_id), [])
            by_date: dict[str, int] = {}
            by_ts_date: dict[str, int] = {}
            for e in all_entries:
                d = e.get("date", "?")
                ts_d = str(e.get("time_start", ""))[:10]
                by_date[d] = by_date.get(d, 0) + e.get("total_time", 0)
                by_ts_date[ts_d] = by_ts_date.get(ts_d, 0) + e.get("total_time", 0)
            results.append(
                f"**Alexey (76542) Jun1→Jun3**\n"
                f"Entries: {len(all_entries)}\n"
                f"By `date` field: {by_date}\n"
                f"By `time_start` date: {by_ts_date}\n"
                f"→ Jun2 by date: {by_date.get('2026-06-02',0)/3600:.2f}h\n"
                f"→ Jun2 by time_start: {by_ts_date.get('2026-06-02',0)/3600:.2f}h"
            )
        except Exception as exc:
            results.append(f"**Alexey test** Error: {exc}")

    user = await bot.fetch_user(PM_USER_ID)
    await user.send("🔬 **API test (Samvel, yesterday):**\n\n" + "\n\n".join(results))
    await ctx.message.add_reaction("✅")


@bot.command(name="findmembers")
async def cmd_find_members(ctx: commands.Context) -> None:
    """!findmembers — list all Renormalize workspace members with their IDs (PM only)."""
    if ctx.author.id != PM_USER_ID:
        await ctx.message.add_reaction("🚫")
        return
    if not RENORMALIZE_API_KEY:
        await ctx.send("❌ RENORMALIZE_API_KEY не задан в .env")
        return

    await ctx.message.add_reaction("⏳")

    import httpx

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://api.renormalize.com/members",
                params={
                    "roles":  "engineer,manager,sales,qa,field_worker",
                    "status": "active,pending",
                    "page":   1,
                    "count":  200,
                },
                headers={"Authorization": f"Bearer {RENORMALIZE_API_KEY}"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        log.exception("findmembers API error: %s", exc)
        user = await bot.fetch_user(PM_USER_ID)
        await user.send(f"❌ Ошибка запроса к Renormalize:\n```{exc}```")
        await ctx.message.add_reaction("🔴")
        return

    # Handle various response shapes
    members = (
        data if isinstance(data, list)
        else data.get("data", data.get("members", data.get("users", [])))
    )

    if not members:
        user = await bot.fetch_user(PM_USER_ID)
        await user.send(
            "⚠️ Пустой список или неизвестная структура ответа.\n"
            f"Сырой ответ (первые 500 символов):\n```{str(data)[:500]}```"
        )
        await ctx.message.add_reaction("⚠️")
        return

    lines = ["👥 **Сотрудники в Renormalize (ID — Имя):**\n"]
    for m in members:
        uid  = m.get("id") or m.get("user_id") or "?"
        name = (
            m.get("full_name") or m.get("name")
            or f"{m.get('first_name','')} {m.get('last_name','')}".strip()
            or m.get("username") or "?"
        )
        role = m.get("role") or (m.get("roles") or [""])[0]
        lines.append(f"`{uid}` — **{name}** ({role})")

    text = "\n".join(lines)
    user = await bot.fetch_user(PM_USER_ID)
    # Split if over Discord's 2000-char limit
    for chunk in [text[i:i+1900] for i in range(0, len(text), 1900)]:
        await user.send(chunk)

    await ctx.message.add_reaction("✅")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN is not set. Copy .env.example → .env and fill it in.")
    if not PM_USER_ID:
        raise SystemExit("PM_USER_ID is not set.")
    bot.run(TOKEN, log_handler=None)
