"""Environment config, logging setup and the team roster."""

import logging
import os
from datetime import timedelta, timezone
from typing import Optional

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

UTC3    = timezone(timedelta(hours=3))   # fixed offset — every user-facing time is "UTC+3"
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

# The same curated pool, minus the 8 TEAM members above (already tracked with their own
# real targets) — baked straight into the trackable roster so everyone's already pickable
# in /subscribe from the start, no command/action needed. Flat 8h/40h target: we don't
# have individual targets for them like TEAM does. (renormalize_id, display_name)
CANDIDATE_ROSTER: list[tuple[int, str]] = [
    (76435, "Albina Haivan"),
    (76238, "Albina Zemskova"),
    (76217, "Alex Kolchyn"),
    (76671, "Alexander Stotsky"),
    (76669, "Alexey Lavrenchuk"),
    (76800, "Altynai Kadyrova"),
    (76606, "Andrew Shmorhun"),
    (76716, "Andrii Matsiuk"),
    (76819, "Anna Dychkivska"),
    (76682, "Anna Yermolenko"),
    (76675, "Artem Sydun"),
    (76218, "Baraka mukelenga"),
    (76653, "Bohdan Sameliuk"),
    (76658, "Danil Yenbaiev"),
    (76775, "Daria Herasimova"),
    (76813, "Denys Danko"),
    (76824, "Dmytro Ranskyi"),
    (76667, "Dmytro Shcherbonos"),
    (76811, "Eugenia Maslova"),
    (76663, "Ignatii Kim"),
    (76619, "Ihor Mokhnatskyy"),
    (76668, "Ilya Virich"),
    (76508, "Karyna Vakar"),
    (76659, "Kitchak Pavlo"),
    (76548, "Maksym Honchar"),
    (76223, "Marcelo Inocente"),
    (76754, "Maria Melnychuk"),
    (76444, "Maria Royko"),
    (76239, "Mariam Sargsyan"),
    (76509, "Michael Adinebo"),
    (76721, "Michael Bezruchko"),
    (76827, "Mohamed Kamel"),
    (76630, "Mykola Konovalov"),
    (76689, "Mykola Lysenko"),
    (76471, "Mykola Maslov"),
    (76547, "Nurbek Azamat uulu"),
    (76220, "Oleg Merkuriev"),
    (76817, "Oleksii Fedorkan"),
    (76618, "Oleksii Kharenko"),
    (76823, "Olesia Drahanchuk"),
    (76661, "Oloo Moses"),
    (76776, "Pavlo Rudyuk"),
    (76646, "Sergiy Zinovyev"),
    (76814, "Serhii Romaniuk"),
    (76795, "Serhii Shymotiuk"),
    (76808, "Svitlana Romanova"),
    (76654, "Tolulope Olaniyan"),
    (76603, "Vadim Saratov"),
    (76727, "Vasyl Liutan"),
    (76665, "Vladimir Shapovalov"),
    (76672, "Vladimir Voronkov"),
    (76673, "Vladyslav  Moskalenko"),
    (76777, "Vladyslav Bondarenko"),
    (76818, "Volodymyr Vysotskyi"),
    (76760, "Yulii Maievskyi"),
    (76656, "Yurii Synkulych"),
    (76647, "Yuriy Cherkasov"),
]
