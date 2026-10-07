"""SQLite data layer: day-offs, subscriptions, custom members, discord links, preferences."""

import os
import sqlite3
from datetime import date
from typing import Optional

from config import DB_PATH, RENORMALIZE_IDS, REPORTS_CHANNEL_ID, TEAM


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
        for column in ("reports_channel_id INTEGER", "reminders INTEGER NOT NULL DEFAULT 0"):
            try:
                conn.execute(f"ALTER TABLE preferences ADD COLUMN {column}")
            except sqlite3.OperationalError:
                pass                                # column already exists
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS discord_links (
                member          TEXT    PRIMARY KEY,
                discord_user_id INTEGER NOT NULL
            )
            """
        )
        # Check *before* creating the table — the seeding below must run exactly once,
        # the first time this table is created, not every time it happens to be empty
        # (otherwise a deliberate !alloweduser remove-everyone would quietly get undone
        # by the next bot restart).
        allowed_users_is_new = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'allowed_users'"
        ).fetchone() is None

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS allowed_users (
                discord_user_id INTEGER PRIMARY KEY
            )
            """
        )
        # Grandfather in everyone who already uses the bot the first time this table is
        # created, so turning on access control doesn't lock out the whole current team —
        # only people added after this point need an explicit !alloweduser add.
        if allowed_users_is_new:
            existing_ids: set[int] = set()
            for row in conn.execute("SELECT DISTINCT discord_user_id FROM subscriptions"):
                existing_ids.add(row[0])
            for row in conn.execute("SELECT DISTINCT discord_user_id FROM preferences"):
                existing_ids.add(row[0])
            if existing_ids:
                conn.executemany(
                    "INSERT OR IGNORE INTO allowed_users (discord_user_id) VALUES (?)",
                    [(uid,) for uid in existing_ids],
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
        result[name] = renorm_id if renorm_id > 0 else None   # < 0 → report-only person
    return result


def add_report_only_member(name: str, discord_user_id: int) -> None:
    """Add a person without Renormalize: no hours, only the daily-report check.

    Stored in custom_members with renormalize_id = -discord_user_id and 0h targets.
    """
    add_custom_member(-discord_user_id, name, daily=0.0, weekly=0.0)
    save_discord_link(name, discord_user_id)


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


def save_reports_channel(user_id: int, channel_id: int) -> None:
    """Save the user's own daily-reports channel."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO preferences (discord_user_id, reports_channel_id) VALUES (?, ?)
            ON CONFLICT(discord_user_id) DO UPDATE SET
                reports_channel_id = excluded.reports_channel_id
            """,
            (user_id, channel_id),
        )
        conn.commit()


def get_reports_channel(user_id: int) -> int:
    """Return the user's daily-reports channel ID, falling back to REPORTS_CHANNEL_ID (0 = none)."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT reports_channel_id FROM preferences WHERE discord_user_id = ?",
            (user_id,),
        ).fetchone()
    return (row[0] if row and row[0] else None) or REPORTS_CHANNEL_ID


def save_reminders(user_id: int, enabled: bool) -> None:
    """Turn evening reminders for the user's subscribed people on/off."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO preferences (discord_user_id, reminders) VALUES (?, ?)
            ON CONFLICT(discord_user_id) DO UPDATE SET reminders = excluded.reminders
            """,
            (user_id, int(enabled)),
        )
        conn.commit()


def get_reminders(user_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT reminders FROM preferences WHERE discord_user_id = ?", (user_id,)
        ).fetchone()
    return bool(row and row[0])


def get_reminder_subscribers() -> list[int]:
    """Subscribers who turned reminders on."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT discord_user_id FROM preferences WHERE reminders = 1"
        ).fetchall()
    return [r[0] for r in rows]


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


# --- access control -----------------------------------------------------------

def add_allowed_user(user_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR IGNORE INTO allowed_users (discord_user_id) VALUES (?)", (user_id,))
        conn.commit()


def remove_allowed_user(user_id: int) -> bool:
    """Returns True if the user was removed (False if they weren't on the list)."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute("DELETE FROM allowed_users WHERE discord_user_id = ?", (user_id,))
        conn.commit()
        return cursor.rowcount > 0


def get_allowed_users() -> list[int]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT discord_user_id FROM allowed_users ORDER BY rowid").fetchall()
    return [r[0] for r in rows]


def is_allowed_user(user_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM allowed_users WHERE discord_user_id = ?", (user_id,)
        ).fetchone()
    return row is not None
