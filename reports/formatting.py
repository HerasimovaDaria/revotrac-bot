"""Rendering of daily/weekly reports as Discord message text."""

from datetime import date, timedelta
from typing import Optional

from db import _all_members, get_discord_links
from utils import workdays_between


def status_emoji(worked: float, target: float, day_off: bool) -> str:
    if day_off:
        return "✅"
    diff = worked - target
    if diff > 3:       return "🔵"   # overtime — more than 3h over target
    if diff < -2:      return "🔴"   # under-track — more than 2h behind
    return "✅"


MONTHS   = ["January", "February", "March", "April", "May", "June", "July",
            "August", "September", "October", "November", "December"]
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _h(hours: float) -> str:
    """6.8 → '6.8'."""
    return f"{hours:.1f}"


def _day_title(d: date) -> str:
    """date(2026, 10, 2) → 'Friday, October 2'."""
    return f"{WEEKDAYS[d.weekday()]}, {MONTHS[d.month - 1]} {d.day}"


def format_daily_report(
    report_date:    date,
    hours:          dict[str, float],
    day_offs:       set[str],
    filter_members: Optional[list[str]] = None,   # None → all members
    report_authors: Optional[set[int]] = None,    # None → report check disabled
    month_hours:    Optional[dict[str, tuple[float, float]]] = None,   # name -> (worked, target); None → skip
) -> str:
    """Show members more than 2h behind today, more than 3h over target today (overtime,
    listed separately under "OverTimes:"), or missing a daily report — everyone else is
    folded into the "no issues" summary line."""
    header = f"### {_day_title(report_date)}\n"

    active = [(n, d, w) for n, _en, d, w in _all_members()
              if filter_members is None or n in filter_members]

    links = get_discord_links()

    behind:    list[str] = []
    overtime:  list[str] = []
    unlinked:  list[str] = []
    for name, daily, _ in active:
        if name in day_offs:
            continue                      # day off is not a problem
        worked    = hours.get(name, 0.0)
        emoji     = status_emoji(worked, daily, False)
        uid       = links.get(name)
        no_report = report_authors is not None and uid is not None and uid not in report_authors
        if report_authors is not None and uid is None:
            unlinked.append(name)
        if not daily:                     # report-only person: hours don't matter
            if no_report:
                behind.append(f"🟡 **{name}** · no report")
            continue
        if emoji == "✅" and not no_report:
            continue
        marker = "🟡" if emoji == "✅" else emoji   # ✅-but-no-report still needs a flag
        line   = f"{marker} **{name}** · {_h(worked)} of {daily:g}h today"
        if emoji == "🔵":
            line += " · over target today"
        if month_hours is not None:
            done, target = month_hours.get(name, (0.0, 0.0))
            delta = done - target
            if abs(delta) > 1:
                line += f" · {delta:+.1f}h, {_h(done)}/{_h(target)}h this month"
            else:
                line += " · on track this month"
        if no_report:
            line += " · no report"
        (overtime if emoji == "🔵" else behind).append(line)

    ok = len(active) - len(behind) - len(overtime)
    lines = list(behind)
    if not lines and not overtime:
        lines.append("Nothing to flag" if ok == 1 else f"Nothing to flag — all {ok} are on track")
    elif ok:
        lines.append(f"-# The other {ok} — no issues")
    if overtime:
        lines.append("\n**OverTimes:**")
        lines.extend(overtime)
    if unlinked:
        lines.append(
            f"-# Report not checked, no Discord link: {', '.join(unlinked)}. "
            f"Link one: `!linkdiscord <name> <ID>`"
        )

    return header + "\n".join(lines)


def format_weekly_report(
    week_begin:     date,
    week_hours:     dict[str, float],
    filter_members: Optional[list[str]] = None,   # None → all members
    today:          Optional[date] = None,        # None → week_begin (i.e. no days elapsed yet)
) -> str:
    """Progress toward the weekly target for everyone — not filtered to problems like the
    daily/monthly reports. The status circle compares done against a *prorated* target
    (daily rate × workdays elapsed in the week so far), not the full weekly target — early
    in the week nobody's done the full target yet, so that comparison would be meaningless."""
    header = f"### Week of {MONTHS[week_begin.month - 1]} {week_begin.day}\n"
    today  = today or week_begin
    week_workdays_so_far = workdays_between(week_begin, min(today, week_begin + timedelta(days=6)))

    active = [(n, d, w) for n, _en, d, w in _all_members()
              if filter_members is None or n in filter_members]

    lines: list[str] = []
    for name, daily, weekly in active:
        if not weekly:                    # report-only person
            continue
        done       = week_hours.get(name, 0.0)
        remaining  = max(weekly - done, 0.0)
        pct        = int(min(done / weekly, 1.0) * 100) if weekly else 0
        tail       = f"{_h(remaining)}h left" if remaining > 0 else "target reached"
        prorated   = daily * week_workdays_so_far
        week_delta = done - prorated
        marker     = "🔴" if week_delta < -1 else "🔵" if week_delta > 1 else "✅"
        lines.append(f"{marker} **{name}** · {_h(done)} of {weekly:g}h · {pct}%\n-# {tail}")

    if not lines:
        return ""
    return header + "\n".join(lines)


def format_monthly_report(
    report_date:    date,
    month_hours:    dict[str, tuple[float, float]],   # name -> (worked, target)
    filter_members: Optional[list[str]] = None,   # None → all members
) -> str:
    """Month-to-date shortfall, people who are behind only — everyone else is omitted."""
    workdays = workdays_between(report_date.replace(day=1), report_date)
    header   = f"### {MONTHS[report_date.month - 1]} — {workdays} workdays so far\n"

    active = [(n, d, w) for n, _en, d, w in _all_members()
              if filter_members is None or n in filter_members]

    lines: list[str] = []
    for name, daily, _ in active:
        if not daily:                     # report-only person
            continue
        done, target = month_hours.get(name, (0.0, 0.0))
        delta = done - target
        if delta >= -1:
            continue
        pct = int(min(done / target, 1.0) * 100) if target else 0
        lines.append(f"🔴 **{name}** · {_h(done)} of {_h(target)}h · {pct}%\n-# {_h(-delta)}h behind")

    if not lines:
        return "✅ Everyone's on track this month."
    return header + "\n".join(lines)
