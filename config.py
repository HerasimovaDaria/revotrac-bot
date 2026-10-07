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
