"""Environment config, logging setup and the team roster."""

import logging
import os
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

TOKEN             = os.getenv("DISCORD_BOT_TOKEN", "")
PM_USER_ID        = int(os.getenv("PM_USER_ID", "0"))
# Support both variable names (RENORMALIZE_TOKEN is the real JWT, RENORMALIZE_API_KEY is legacy)
RENORMALIZE_API_KEY = os.getenv("RENORMALIZE_TOKEN") or os.getenv("RENORMALIZE_API_KEY", "")
# Default channel with daily reports (text or forum) for subscribers without !setchannel.
# 0 → no default (report check only for those who ran !setchannel).
REPORTS_CHANNEL_ID = int(os.getenv("REPORTS_CHANNEL_ID") or 0)
# Evening "you haven't posted your daily report" DM, HH:MM UTC+3
REMINDER_HOUR, REMINDER_MINUTE = (int(x) for x in (os.getenv("REMINDER_TIME") or "19:00").split(":"))

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

# Renormalize user IDs — find them with !findmembers,
# or manually: open the employee's report in Renormalize, the ID is in the URL: ?id=XXXXX
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

# Renormalize IDs that /addmember is allowed to *suggest* by name (engineering-adjacent
# roles only — not sales, HR, or other departments; curated by the PM). Adding someone by
# a known Renormalize ID directly still works regardless of this list — this only limits
# what shows up when searching/autocompleting by name, so the bot doesn't surface the
# whole company directory. Edit this set (and redeploy) to change who's suggestable.
ADDMEMBER_CANDIDATE_IDS: frozenset[int] = frozenset({
    76217, 76218, 76220, 76223, 76238, 76239, 76435, 76444,
    76471, 76508, 76509, 76536, 76537, 76542, 76544, 76547,
    76548, 76603, 76606, 76607, 76618, 76619, 76630, 76632,
    76646, 76647, 76653, 76654, 76656, 76657, 76658, 76659,
    76661, 76663, 76665, 76667, 76668, 76669, 76671, 76672,
    76673, 76675, 76682, 76689, 76716, 76718, 76721, 76727,
    76754, 76760, 76775, 76776, 76777, 76795, 76800, 76808,
    76811, 76813, 76814, 76817, 76818, 76819, 76823, 76824,
    76827,
})
