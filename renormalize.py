"""Hours data source: the Renormalize API, with a mock fallback."""

import asyncio
import random
import time
from datetime import date, datetime, timedelta
from typing import Optional

from config import RENORMALIZE_API_KEY, UTC2, log
from db import _all_members, _all_renormalize_ids, get_screenshot_activity, save_screenshot_activity
from utils import workdays_between

# Combined keyboard+mouse actions/minute below which a Renormalize screenshot counts as
# "low activity" — tracked, but with ~no real input during that interval. Per the team's
# own definition (not something the API classifies for you).
LOW_ACTIVITY_THRESHOLD = 30

# Cap on simultaneous Renormalize API requests — fetch_* functions fire one request per
# member via asyncio.gather instead of awaiting them one at a time, which is what made a
# full-roster /report take 30-40s. Capped (not unlimited) to avoid hammering the API.
_API_CONCURRENCY = 10


def _scoped_members(members: Optional[list[str]]) -> list[tuple[str, str, float, float]]:
    """_all_members(), filtered down to *members* when given — the whole point of passing a
    filter being to only hit the Renormalize API for people actually needed (e.g. one
    subscriber's tracked list), instead of the entire ~65-person roster every time."""
    all_m = _all_members()
    if members is None:
        return all_m
    wanted = set(members)
    return [m for m in all_m if m[0] in wanted]

# ---------------------------------------------------------------------------
# Workspace user directory — GET /v1/users (NOT /members, which is 501 Not Implemented).
# Cached briefly since it backs live Discord autocomplete (!adddevelopertolist) and is paginated.
# ---------------------------------------------------------------------------

_USERS_CACHE_TTL = 300   # seconds
_users_cache: dict = {"data": [], "ts": 0.0}


async def fetch_all_renormalize_users(force: bool = False) -> list[dict]:
    """Return every Renormalize workspace account: [{"id", "name", "email", "status"}, ...].

    Cached for _USERS_CACHE_TTL seconds — pass force=True to bypass the cache.
    """
    now = time.monotonic()
    if not force and _users_cache["data"] and now - _users_cache["ts"] < _USERS_CACHE_TTL:
        return _users_cache["data"]
    if not RENORMALIZE_API_KEY:
        return []

    import httpx

    users: list[dict] = []
    async with httpx.AsyncClient() as client:
        headers = {"Authorization": f"Bearer {RENORMALIZE_API_KEY}"}
        page = 1
        while True:
            resp = await client.get(
                "https://api.renormalize.com/v1/users",
                params={"page": page, "count": 200},
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            data  = resp.json()
            batch = data if isinstance(data, list) else data.get("data", [])
            if not batch:
                break
            users.extend(batch)
            if len(batch) < 200:
                break
            page += 1

    _users_cache["data"], _users_cache["ts"] = users, now
    return users


def _split_yellow(entries: list[dict]) -> tuple[float, float]:
    """Return (worked, yellow) hours from a list of time/progression entries. yellow is the
    subset Renormalize itself flagged "redacted" — idle time (reason="idle") or time added/
    edited by hand (any other reason text) — as opposed to normal automatic tracking. Always
    <= worked, since it's a subset, not an addition."""
    worked = sum(e.get("total_time", 0) for e in entries) / 3600
    yellow = sum(e.get("total_time", 0) for e in entries if e.get("redacted")) / 3600
    return worked, yellow


def _mock_hours(members: Optional[list[str]] = None) -> dict[str, tuple[float, float]]:
    """Fallback: random hours near each person's daily target, no yellow time (mock mode
    has no concept of idle/manual entries)."""
    result: dict[str, tuple[float, float]] = {}
    for name, _en, daily, _ in _scoped_members(members):
        if not daily:                       # report-only person
            result[name] = (0.0, 0.0)
            continue
        hours = random.gauss(daily, 1.5)
        result[name] = (round(max(0.0, min(daily + 2, hours)), 2), 0.0)
    return result


async def fetch_hours(target_date: date, members: Optional[list[str]] = None) -> dict[str, tuple[float, float]]:
    """
    Return {name: (worked, yellow)} hours worked by each team member on *target_date*.
    yellow is the idle/manually-added subset of worked — see _split_yellow().

    Pass *members* to only fetch for those people (e.g. one subscriber's tracked list)
    instead of the whole roster — much faster for on-demand commands. None (default) means
    everyone, for batch jobs serving several subscribers at once.

    Real data comes from the Renormalize API (api.renormalize.com), fetched concurrently
    (one request per member, up to _API_CONCURRENCY at a time).
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
        return _mock_hours(members)

    date_str  = target_date.isoformat()
    # end_at is EXCLUSIVE in the API — use target_date + 1 to include target_date entries
    end_at    = (target_date + timedelta(days=1)).isoformat()

    import httpx

    all_ids = _all_renormalize_ids()
    sem     = asyncio.Semaphore(_API_CONCURRENCY)

    async def _one(client: "httpx.AsyncClient", headers: dict, name: str, daily: float):
        renorm_id = all_ids.get(name)
        if not daily:                   # report-only person
            return name, (0.0, 0.0)
        if renorm_id is None:
            hours = random.gauss(daily, 1.5)
            return name, (round(max(0.0, min(daily + 2, hours)), 2), 0.0)

        async with sem:
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
                # Filter by date field (confirmed reliable by testapi)
                entries = [e for e in resp.json().get(str(renorm_id), []) if e.get("date") == date_str]
                worked, yellow = _split_yellow(entries)
                log.info("%s on %s: %.2fh (%d entries)", name, date_str, worked, len(entries))
                return name, (round(worked, 2), round(yellow, 2))

            except httpx.HTTPStatusError as exc:
                log.error("Renormalize %s for %s: %s", exc.response.status_code, name, exc.response.text[:100])
                return name, (0.0, 0.0)
            except Exception as exc:
                log.exception("fetch_hours failed for %s: %s", name, exc)
                return name, (0.0, 0.0)

    async with httpx.AsyncClient() as client:
        headers = {"Authorization": f"Bearer {RENORMALIZE_API_KEY}"}
        pairs = await asyncio.gather(*(
            _one(client, headers, name, daily)
            for name, _en, daily, _ in _scoped_members(members)
        ))

    return dict(pairs)


async def fetch_week_hours(week_begin: date, members: Optional[list[str]] = None) -> dict[str, tuple[float, float]]:
    """
    Return {name: (worked, yellow)} total hours worked per team member for the week starting
    *week_begin*. Makes 1 API call per member (not 7), fired concurrently (up to
    _API_CONCURRENCY at a time). Pass *members* to only fetch for those people.
    """
    today  = datetime.now(UTC2).date()
    end    = min(week_begin + timedelta(days=6), today)
    all_m  = _scoped_members(members)
    all_ids = _all_renormalize_ids()
    totals: dict[str, tuple[float, float]] = {m[0]: (0.0, 0.0) for m in all_m}

    if not RENORMALIZE_API_KEY:
        for name, _en, daily, weekly in all_m:
            totals[name] = (round(random.gauss(weekly * 0.85, weekly * 0.1), 2), 0.0)
        return totals

    import httpx
    sem = asyncio.Semaphore(_API_CONCURRENCY)

    async def _one(client: "httpx.AsyncClient", headers: dict, name: str, renorm_id: Optional[int]):
        if renorm_id is None:
            return None
        async with sem:
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
                worked, yellow = _split_yellow(entries)
                return name, (round(worked, 2), round(yellow, 2))
            except Exception as exc:
                log.exception("fetch_week_hours failed for %s: %s", name, exc)
                return None

    async with httpx.AsyncClient() as client:
        headers = {"Authorization": f"Bearer {RENORMALIZE_API_KEY}"}
        results = await asyncio.gather(*(
            _one(client, headers, name, all_ids.get(name)) for name, _en, _, _ in all_m
        ))
    totals.update(r for r in results if r is not None)

    return totals


async def fetch_month_hours(
    target_date: date, members: Optional[list[str]] = None,
) -> dict[str, tuple[float, float, float]]:
    """
    Return {name: (worked, target, yellow)} from the 1st of *target_date*'s month through
    *target_date* (inclusive). worked is the actual logged hours — no synthetic credit.
    target is daily_rate * workdays elapsed, minus daily_rate for each workday covered by a
    vacation/sick-leave/absence record — the hard cap already accounts for days off, instead
    of inflating worked hours to paper over them. yellow is the idle/manually-added subset of
    worked — see _split_yellow(). 1 API call per member for hours, plus 1 (cached) for leave,
    fired concurrently (up to _API_CONCURRENCY at a time). Pass *members* to only fetch for
    those people.
    """
    month_start = target_date.replace(day=1)
    workdays    = workdays_between(month_start, target_date)
    all_m       = _scoped_members(members)
    all_ids     = _all_renormalize_ids()
    totals: dict[str, tuple[float, float, float]] = {m[0]: (0.0, m[2] * workdays, 0.0) for m in all_m}

    if not RENORMALIZE_API_KEY:
        for name, _en, daily, _ in all_m:
            if not daily:
                continue
            target = daily * workdays
            worked = round(random.gauss(target * 0.85, target * 0.1 or 1), 2)
            totals[name] = (worked, target, 0.0)
        return totals

    import httpx
    sem = asyncio.Semaphore(_API_CONCURRENCY)

    async def _one(client: "httpx.AsyncClient", headers: dict, name: str, daily: float,
                    renorm_id: Optional[int]):
        if renorm_id is None:
            return None
        target = daily * workdays
        async with sem:
            try:
                resp = await client.get(
                    "https://api.renormalize.com/v1/time/progression",
                    params={
                        "user_ids": str(renorm_id),
                        "start_at": month_start.isoformat(),
                        "end_at":   (target_date + timedelta(days=1)).isoformat(),  # exclusive → +1
                    },
                    headers=headers,
                    timeout=15,
                )
                resp.raise_for_status()
                entries = resp.json().get(str(renorm_id), [])
                worked, yellow = _split_yellow(entries)

                if daily and renorm_id > 0:
                    leave_days = await _leave_workdays_in_range(renorm_id, month_start, target_date)
                    target    -= daily * leave_days

                return name, (round(worked, 2), round(target, 2), round(yellow, 2))
            except Exception as exc:
                log.exception("fetch_month_hours failed for %s: %s", name, exc)
                return None

    async with httpx.AsyncClient() as client:
        headers = {"Authorization": f"Bearer {RENORMALIZE_API_KEY}"}
        results = await asyncio.gather(*(
            _one(client, headers, name, daily, all_ids.get(name))
            for name, _en, daily, _ in all_m
        ))
    totals.update(r for r in results if r is not None)

    return totals


# ---------------------------------------------------------------------------
# Day off / sick leave / absence — GET /v1/vacations?user_id=<id> (the name is
# misleading: it returns every leave type — "vacation", "sick_leave", "absence" — not
# just vacations). No manual day-off entry needed: we read it straight from Renormalize.
# Only covers people with a Renormalize ID — report-only people (added via !addperson)
# have no Renormalize account, so they can't be checked this way.
# ---------------------------------------------------------------------------

_LEAVE_CACHE_TTL = 1800   # seconds
_leave_cache: dict[int, dict] = {}   # renorm_id -> {"data": [(start, end), ...], "ts": float}


async def fetch_leave_records(renorm_id: int, force: bool = False) -> list[tuple[date, date]]:
    """Return [(start_date, end_date), ...] for every leave record of this Renormalize user,
    past and future. Cached per ID for _LEAVE_CACHE_TTL seconds.
    """
    now    = time.monotonic()
    cached = _leave_cache.get(renorm_id)
    if not force and cached and now - cached["ts"] < _LEAVE_CACHE_TTL:
        return cached["data"]
    if not RENORMALIZE_API_KEY:
        return []

    import httpx

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://api.renormalize.com/v1/vacations",
                params={"user_id": str(renorm_id)},
                headers={"Authorization": f"Bearer {RENORMALIZE_API_KEY}"},
                timeout=15,
            )
            resp.raise_for_status()
            raw = resp.json()
    except Exception as exc:
        log.warning("fetch_leave_records failed for %s: %s", renorm_id, exc)
        return cached["data"] if cached else []

    records: list[tuple[date, date]] = []
    for rec in raw:
        try:
            start = date.fromisoformat(rec["start"][:10])
            end   = date.fromisoformat(rec["end"][:10])
            records.append((start, end))
        except (KeyError, ValueError, TypeError):
            continue

    _leave_cache[renorm_id] = {"data": records, "ts": now}
    return records


async def _leave_workdays_in_range(renorm_id: int, start: date, end: date) -> int:
    """Count distinct Mon–Fri days in [start, end] covered by any leave record (any type)."""
    records = await fetch_leave_records(renorm_id)
    covered: set[date] = set()
    for rec_start, rec_end in records:
        lo, hi = max(rec_start, start), min(rec_end, end)
        d = lo
        while d <= hi:
            if d.weekday() < 5:
                covered.add(d)
            d += timedelta(days=1)
    return len(covered)


async def fetch_day_offs(target_date: date, members: Optional[list[str]] = None) -> set[str]:
    """Return display names of everyone on vacation/sick leave/absence in Renormalize on
    *target_date*. Report-only people (no Renormalize ID) are never included — there's no
    leave data to check for them. Pass *members* to only check those people. Fetches each
    person's (cached) leave record concurrently rather than one at a time.
    """
    all_ids = _all_renormalize_ids()
    candidates = [
        (name, all_ids.get(name)) for name, _en, _daily, _weekly in _scoped_members(members)
        if all_ids.get(name) and all_ids.get(name) > 0
    ]

    async def _check(name: str, renorm_id: int) -> Optional[str]:
        for start, end in await fetch_leave_records(renorm_id):
            if start <= target_date <= end:
                return name
        return None

    results = await asyncio.gather(*(_check(name, rid) for name, rid in candidates))
    return {name for name in results if name}


# ---------------------------------------------------------------------------
# Low activity — GET /v1/screenshots?from=&to=&user_id= (separate endpoint from
# /v1/time/progression; found via the Renormalize UI's own network tab, same as the other
# endpoints). Each screenshot carries keyboard_usage_per_minute/mouse_usage_per_minute for
# that interval — "low activity" (tracked, but ~no real input) isn't a field Renormalize
# hands back directly, it's our own classification on top: combined usage below
# LOW_ACTIVITY_THRESHOLD. A past day's screenshots never change retroactively, so once
# computed they're cached in the database (screenshot_activity table) — only today, and
# any day never seen before, actually hits the API.
# ---------------------------------------------------------------------------

async def _fetch_screenshots(
    client: "object", headers: dict, renorm_id: int, frm: date, to: date,
) -> list[dict]:
    """All screenshots for renorm_id in [frm, to) — paginated, 100 per page (API max)."""
    shots: list[dict] = []
    page = 1
    while True:
        try:
            resp = await client.get(
                "https://api.renormalize.com/v1/screenshots",
                params={
                    "from":    frm.isoformat(),
                    "to":      to.isoformat(),
                    "user_id": str(renorm_id),
                    "page":    page,
                    "count":   100,
                },
                headers=headers,
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.exception("fetch_low_activity: screenshots failed for %s: %s", renorm_id, exc)
            break
        shots.extend(data.get("screenshots", []))
        pagination = data.get("pagination", {})
        if page >= pagination.get("total_pages", 1):
            break
        page += 1
    return shots


async def fetch_low_activity(
    start_date: date, end_date: date, members: Optional[list[str]] = None,
) -> dict[str, tuple[float, float]]:
    """
    Return {name: (tracked_hours, low_activity_hours)} from Renormalize screenshots between
    *start_date* and *end_date* (inclusive). tracked_hours is the sum of screenshot
    durations — not the same figure as fetch_hours()'s "worked" (manually-added/edited
    time has no screenshots at all, so this is always <= that). low_activity_hours is the
    subset where keyboard+mouse usage was below LOW_ACTIVITY_THRESHOLD.

    Pass *members* to only fetch for those people. Concurrency capped at _API_CONCURRENCY;
    past days are cached per person in the database and never re-fetched.
    """
    all_m = _scoped_members(members)
    if not RENORMALIZE_API_KEY:
        return {m[0]: (0.0, 0.0) for m in all_m}

    all_ids = _all_renormalize_ids()
    today   = datetime.now(UTC2).date()

    import httpx
    sem = asyncio.Semaphore(_API_CONCURRENCY)

    async def _day(client: httpx.AsyncClient, headers: dict, renorm_id: int, day: date) -> tuple[float, float]:
        if day < today:
            cached = get_screenshot_activity(renorm_id, day)
            if cached is not None:
                return cached
        async with sem:
            shots = await _fetch_screenshots(client, headers, renorm_id, day, day + timedelta(days=1))
        total = sum(s.get("duration", 0) for s in shots)
        low   = sum(
            s.get("duration", 0) for s in shots
            if s.get("keyboard_usage_per_minute", 0) + s.get("mouse_usage_per_minute", 0) < LOW_ACTIVITY_THRESHOLD
        )
        if day < today:
            save_screenshot_activity(renorm_id, day, total, low)
        return total, low

    async def _person(client: httpx.AsyncClient, headers: dict, name: str, renorm_id: Optional[int]):
        if not renorm_id or renorm_id < 0:
            return name, (0.0, 0.0)
        total_sec = low_sec = 0.0
        d = start_date
        while d <= end_date:
            t, l = await _day(client, headers, renorm_id, d)
            total_sec += t
            low_sec   += l
            d += timedelta(days=1)
        return name, (round(total_sec / 3600, 2), round(low_sec / 3600, 2))

    async with httpx.AsyncClient() as client:
        headers = {"Authorization": f"Bearer {RENORMALIZE_API_KEY}"}
        pairs = await asyncio.gather(*(
            _person(client, headers, name, all_ids.get(name)) for name, _en, _, _ in all_m
        ))

    return dict(pairs)
