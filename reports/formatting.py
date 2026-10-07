"""Rendering of daily/weekly reports as Discord message text."""

from datetime import date
from typing import Optional

from db import _all_members, get_discord_links


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
        if not daily:                     # report-only person: hours don't matter
            if no_report:
                lines.append(f"🟡 **{name}** · нет отчёта")
            continue
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
        if not weekly:                    # report-only person
            continue
        done      = week_hours.get(name, 0.0)
        remaining = max(weekly - done, 0.0)
        pct       = int(min(done / weekly, 1.0) * 100) if weekly else 0
        tail      = f"осталось {_h(remaining)} ч" if remaining > 0 else "норма выполнена"
        lines.append(f"**{name}** · {_h(done)} из {weekly:g} ч · {pct}%\n-# {tail}")

    if not lines:
        return ""
    return header + "\n".join(lines)
