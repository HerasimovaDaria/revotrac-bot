"""Hours data source: the Renormalize API, with a mock fallback."""

import random
import time
from datetime import date, datetime, timedelta

from config import MOSCOW, RENORMALIZE_API_KEY, log
from db import _all_members, _all_renormalize_ids

# ---------------------------------------------------------------------------
# Workspace user directory — GET /v1/users (NOT /members, which is 501 Not Implemented).
# Cached briefly since it backs live Discord autocomplete (!addmember) and is paginated.
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
