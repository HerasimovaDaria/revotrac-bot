"""
Discord bot: daily team hours reports with day-off tracking and per-user subscriptions.

Morning routine (09:00 Moscow):
  1. Personalized report for YESTERDAY — sent to every subscriber (only their chosen members).
  2. Day-off selector for TODAY — sent to PM only.

Commands:
  !subscribe      — choose which team members appear in your daily reports
  !report         — trigger your personalized morning report right now
  !dayoff [DD.MM] — open day-off selector for a specific date (PM only)
  !weekly         — show current-week progress for your subscribed members
  !findmembers    — list all Renormalize workspace members with their IDs (PM only)
"""

import asyncio
import logging
import os
import random
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

MOSCOW  = ZoneInfo("Europe/Moscow")
DB_PATH = "hours.db"

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


def week_start(d: date) -> date:
    """Return the Monday of the week containing *d*."""
    return d - timedelta(days=d.weekday())


# ---------------------------------------------------------------------------
# Hours data source
# ---------------------------------------------------------------------------

def _mock_hours() -> dict[str, float]:
    """Fallback: random hours near each person's daily target."""
    result: dict[str, float] = {}
    for name, _en, daily, _ in TEAM:
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

    date_str = target_date.isoformat()
    results:  dict[str, float] = {}

    import httpx  # local import — already in requirements.txt

    async with httpx.AsyncClient() as client:
        headers = {"Authorization": f"Bearer {RENORMALIZE_API_KEY}"}

        for name, _en, daily, _ in TEAM:
            renorm_id = RENORMALIZE_IDS.get(name)

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
                        "end_at":   date_str,
                    },
                    headers=headers,
                    timeout=15,
                )
                resp.raise_for_status()
                data = resp.json()
                log.info("Renormalize OK for %s: %s", name, str(data)[:200])

                total_seconds = _extract_seconds(data, date_str)
                results[name] = round(total_seconds / 3600, 2)

            except httpx.HTTPStatusError as exc:
                log.error("Renormalize %s for %s (id=%s): %s",
                          exc.response.status_code, name, renorm_id, exc.response.text[:200])
                results[name] = 0.0
            except Exception as exc:
                log.exception("fetch_hours failed for %s: %s", name, exc)
                results[name] = 0.0

    return results


def _extract_seconds(data: dict, date_str: str) -> float:
    """
    Extract total worked seconds from a Renormalize API response.
    Handles multiple possible response shapes from /v1/time/progression.
    """
    # Shape 1: {"data": [{"date": "...", "total_time": <sec>, ...}]}
    if isinstance(data.get("data"), list):
        for item in data["data"]:
            if isinstance(item, dict):
                if item.get("date") == date_str:
                    return (item.get("total_time") or
                            item.get("total_auto_time", 0) +
                            item.get("total_manual_time", 0))
                # No date field — might be a single-item summary
                if "date" not in item:
                    return (item.get("total_time") or
                            item.get("total_auto_time", 0) +
                            item.get("total_manual_time", 0))

    # Shape 2: {"total_time": <sec>}  or  {"worked_time": <sec>}
    for key in ("total_time", "total_auto_time", "worked_time", "duration"):
        if key in data:
            return data[key]

    # Shape 3: flat number
    if isinstance(data, (int, float)):
        return float(data)

    log.warning("Unknown Renormalize response shape: %s", str(data)[:300])
    return 0.0


async def fetch_week_hours(week_begin: date) -> dict[str, float]:
    """Sum daily hours Mon–today (or Mon–Sun if the week is already over)."""
    today = datetime.now(MOSCOW).date()
    end = min(week_begin + timedelta(days=6), today)
    totals: dict[str, float] = {name: 0.0 for name in MEMBER_NAMES}
    for offset in range((end - week_begin).days + 1):
        daily = await fetch_hours(week_begin + timedelta(days=offset))
        for name, h in daily.items():
            totals[name] = round(totals[name] + h, 2)
    return totals


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def status_emoji(worked: float, target: float, day_off: bool) -> str:
    if day_off:
        return "✅"
    diff = worked - target
    if diff >= 0:      return "✅"
    if diff >= -1:     return "⚠️"
    return "🔴"


def progress_bar(done: float, total: float, width: int = 10) -> str:
    ratio  = min(done / total, 1.0) if total else 0.0
    filled = round(ratio * width)
    return "█" * filled + "░" * (width - filled)


def format_daily_report(
    report_date:    date,
    hours:          dict[str, float],
    day_offs:       set[str],
    filter_members: Optional[list[str]] = None,   # None → all members
) -> str:
    dow_ru = {
        "Monday": "Понедельник", "Tuesday": "Вторник", "Wednesday": "Среда",
        "Thursday": "Четверг",  "Friday": "Пятница",  "Saturday": "Суббота",
        "Sunday": "Воскресенье",
    }.get(report_date.strftime("%A"), report_date.strftime("%A"))

    header = (
        f"📊 **Отчёт за {report_date.strftime('%d.%m.%Y')} ({dow_ru})**\n"
        + "━" * 38 + "\n"
    )

    active = [(n, d, w) for n, _en, d, w in TEAM
              if filter_members is None or n in filter_members]

    lines: list[str] = []
    for name, daily, _ in active:
        worked = hours.get(name, 0.0)
        off    = name in day_offs
        emoji  = status_emoji(worked, daily if not off else worked, off)
        if off:
            lines.append(f"  {emoji} **{name}** — выходной")
        else:
            lines.append(f"  {emoji} **{name}** — {worked:.1f}h / {daily:.0f}h")

    return header + "\n".join(lines)


def format_weekly_report(
    week_begin:     date,
    week_hours:     dict[str, float],
    filter_members: Optional[list[str]] = None,   # None → all members
) -> str:
    header = (
        f"📈 **Прогресс за неделю** (c {week_begin.strftime('%d.%m')})\n"
        + "━" * 38 + "\n"
    )

    active = [(n, d, w) for n, _en, d, w in TEAM
              if filter_members is None or n in filter_members]

    lines: list[str] = []
    for name, _, weekly in active:
        done      = week_hours.get(name, 0.0)
        remaining = max(weekly - done, 0.0)
        bar       = progress_bar(done, weekly)
        pct       = int(min(done / weekly, 1.0) * 100) if weekly else 0
        lines.append(
            f"  **{name}**\n"
            f"    `{bar}` {pct}%\n"
            f"    {done:.1f}h / {weekly:.0f}h  (осталось: {remaining:.1f}h)"
        )

    return header + "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Discord UI — subscription selector
# ---------------------------------------------------------------------------

def _subscribe_prompt(current: list[str]) -> str:
    if current:
        return f"📋 **Твоя подписка:** {', '.join(current)}\n\nВыбери, кого хочешь отслеживать:"
    return "📋 У тебя пока нет подписки.\nВыбери сотрудников, чьи часы ты хочешь видеть:"


class SubscribeSelect(discord.ui.Select):
    def __init__(self, current: list[str]) -> None:
        options = [
            discord.SelectOption(
                label=f"{en_name}  (id {RENORMALIZE_IDS.get(name, '?')})",
                value=name,
                default=name in current,
            )
            for name, en_name, _, _ in TEAM
        ]
        super().__init__(
            placeholder="Выберите сотрудников для отслеживания…",
            min_values=0,
            max_values=len(options),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
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
        self.select  = SubscribeSelect(get_subscription(user_id))
        self.add_item(self.select)

    @discord.ui.button(label="Сохранить", style=discord.ButtonStyle.success, emoji="💾")
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        chosen = self.select.values
        save_subscription(self.user_id, chosen)
        self.stop()

        if chosen:
            bullet_list = "\n".join(f"  • {n}" for n in chosen)
            msg = f"✅ **Подписка сохранена!**\n\nБудешь получать утренние отчёты по:\n{bullet_list}"
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
    def __init__(self, target_date: date) -> None:
        self.target_date = target_date
        super().__init__(
            placeholder="Выберите сотрудников…",
            min_values=0,
            max_values=len(MEMBER_NAMES),
            options=[
                discord.SelectOption(label=f"{en_name} ({name})", value=name)
                for name, en_name, _, _ in TEAM
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()


class DayOffView(discord.ui.View):
    def __init__(self, target_date: date) -> None:
        super().__init__(timeout=600)
        self.target_date = target_date
        self.select      = DayOffSelect(target_date)
        self.add_item(self.select)

    @discord.ui.button(label="Сохранить", style=discord.ButtonStyle.success, emoji="💾")
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        chosen = self.select.values
        save_day_offs(chosen, self.target_date)
        msg = (
            f"✅ Выходной на {self.target_date.strftime('%d.%m.%Y')} сохранён: {', '.join(chosen)}"
            if chosen else
            f"✅ Выходных на {self.target_date.strftime('%d.%m.%Y')} не сохранено."
        )
        self.stop()
        await interaction.response.edit_message(content=msg, view=None)

    @discord.ui.button(label="Сегодня все работают", style=discord.ButtonStyle.secondary, emoji="🚫")
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

async def _deliver_report(
    bot:            commands.Bot,
    user_id:        int,
    report_date:    date,
    hours:          dict[str, float],
    week_hours:     dict[str, float],
    day_offs:       set[str],
    filter_members: Optional[list[str]],
) -> None:
    """Fetch user and send them the combined daily + weekly DM."""
    try:
        user        = await bot.fetch_user(user_id)
        wb          = week_start(report_date)
        daily_text  = format_daily_report(report_date, hours, day_offs, filter_members)
        weekly_text = format_weekly_report(wb, week_hours, filter_members)
        await user.send(daily_text + "\n\n" + weekly_text)
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
    yesterday = report_date or (today - timedelta(days=1))

    # Fetch data once; all subscribers share the same raw numbers
    try:
        hours = await fetch_hours(yesterday)
    except Exception as exc:
        log.exception("fetch_hours failed: %s", exc)
        hours = {name: 0.0 for name in MEMBER_NAMES}

    day_offs = get_day_offs(yesterday)
    wb       = week_start(yesterday)

    try:
        week_hours = await fetch_week_hours(wb)
    except Exception as exc:
        log.exception("fetch_week_hours failed: %s", exc)
        week_hours = {name: 0.0 for name in MEMBER_NAMES}

    # --- reports to all subscribers ---
    subscribers = get_all_subscribers()
    if not subscribers:
        log.warning("No subscribers found — nobody will receive a morning report.")

    for user_id, members in subscribers.items():
        await _deliver_report(bot, user_id, yesterday, hours, week_hours, day_offs, members)

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
        send_morning_routine,
        trigger="cron",
        hour=9, minute=0,
        id="morning_report",
        replace_existing=True,
        kwargs={"bot": bot},
    )
    scheduler.start()
    log.info("Scheduler started — morning report at 09:00 Moscow time")


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
    yesterday = today - timedelta(days=1)

    try:
        hours = await fetch_hours(yesterday)
    except Exception as exc:
        log.exception("fetch_hours: %s", exc)
        hours = {name: 0.0 for name in MEMBER_NAMES}

    day_offs = get_day_offs(yesterday)
    wb       = week_start(yesterday)

    try:
        week_hours = await fetch_week_hours(wb)
    except Exception as exc:
        log.exception("fetch_week_hours: %s", exc)
        week_hours = {name: 0.0 for name in MEMBER_NAMES}

    await _deliver_report(bot, user_id, yesterday, hours, week_hours, day_offs, members)
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

        # Correct format: user_ids (string) + start_at + end_at
        attempts = [
            {"user_ids": str(test_id), "start_at": date_str, "end_at": date_str},
            {"user_ids": str(test_id), "start_at": date_str, "end_at": date_str, "entity_type": "engineer"},
        ]

        for params in attempts:
            try:
                resp = await client.get(base, params=params, headers=headers, timeout=10)
                results.append(
                    f"**Params:** `{params}`\n"
                    f"**Status:** {resp.status_code}\n"
                    f"**Body:** ```{resp.text[:800]}```"
                )
                if resp.status_code == 200:
                    break
            except Exception as exc:
                results.append(f"**Params:** `{params}`\n**Error:** {exc}")

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
