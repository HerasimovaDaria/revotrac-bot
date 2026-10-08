"""Hours data source: the Renormalize API, with a mock fallback."""

import random
import time
from datetime import date, datetime, timedelta

from config import RENORMALIZE_API_KEY, UTC2, log
from db import _all_members, _all_renormalize_ids
from utils import workdays_between

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


def _mock_hours() -> dict[str, float]:
    """Fallback: random hours near each person's daily target."""
    result: dict[str, float] = {}
    for name, _en, daily, _ in _all_members():
        if not daily:                       # report-only person
            result[name] = 0.0
            continue
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
            if not daily:                   # report-only person
                results[name] = 0.0
                continue
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
    today      = datetime.now(UTC2).date()
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


async def fetch_month_hours(target_date: date) -> dict[str, tuple[float, float]]:
    """
    Return {name: (worked, target)} from the 1st of *target_date*'s month through
    *target_date* (inclusive). worked is the actual logged hours — no synthetic credit.
    target is daily_rate * workdays elapsed, minus daily_rate for each workday covered by a
    vacation/sick-leave/absence record — the hard cap already accounts for days off, instead
    of inflating worked hours to paper over them. 1 API call per member for hours, plus 1
    (cached) for their leave record.
    """
    month_start = target_date.replace(day=1)
    workdays    = workdays_between(month_start, target_date)
    all_m       = _all_members()
    all_ids     = _all_renormalize_ids()
    totals: dict[str, tuple[float, float]] = {m[0]: (0.0, m[2] * workdays) for m in all_m}

    if not RENORMALIZE_API_KEY:
        for name, _en, daily, _ in all_m:
            if not daily:
                continue
            target = daily * workdays
            worked = round(random.gauss(target * 0.85, target * 0.1 or 1), 2)
            totals[name] = (worked, target)
        return totals

    import httpx
    async with httpx.AsyncClient() as client:
        headers = {"Authorization": f"Bearer {RENORMALIZE_API_KEY}"}
        for name, _en, daily, _ in all_m:
            renorm_id = all_ids.get(name)
            if renorm_id is None:
                continue
            target = daily * workdays
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
                total_sec = sum(e.get("total_time", 0) for e in entries)
                worked    = total_sec / 3600

                if daily and renorm_id > 0:
                    leave_days = await _leave_workdays_in_range(renorm_id, month_start, target_date)
                    target    -= daily * leave_days

                totals[name] = (round(worked, 2), round(target, 2))
            except Exception as exc:
                log.exception("fetch_month_hours failed for %s: %s", name, exc)

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


async def fetch_day_offs(target_date: date) -> set[str]:
    """Return display names of everyone on vacation/sick leave/absence in Renormalize on
    *target_date*. Report-only people (no Renormalize ID) are never included — there's no
    leave data to check for them.
    """
    off: set[str] = set()
    all_ids = _all_renormalize_ids()
    for name, _en, _daily, _weekly in _all_members():
        renorm_id = all_ids.get(name)
        if not renorm_id or renorm_id < 0:
            continue
        for start, end in await fetch_leave_records(renorm_id):
            if start <= target_date <= end:
                off.add(name)
                break
    return off
