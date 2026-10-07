"""
Discord bot: daily team hours reports with day-off tracking and per-user subscriptions.

Morning routine (per-user configured time, default 09:00 UTC+3, Mon–Fri only):
  1. Personalized report for the PREVIOUS WORKDAY (on Monday — for Friday) — sent to every
     subscriber. Lists only their members with an hours shortfall (🟡/🔴) or no daily report
     in the subscriber's own channel (!setchannel; default REPORTS_CHANNEL_ID), any message
     00:00–23:59 UTC+3. People on a day off are skipped.
     Weekly progress is appended only to the report for Friday.
  2. Day-off selector for TODAY — sent to PM only (at PM's configured time).

Evening reminder (REMINDER_TIME, default 19:00 UTC+3, Mon–Fri): for subscribers who ran
!reminders on, everyone in their subscription without a report today gets a DM.

Commands (also available as slash commands with autocomplete: /subscribe, /report, …):
  !subscribe              — choose which team members appear in your daily reports
  !settime [HH:MM]        — set your daily report time (UTC+3). No arg = show current.
  !report                 — trigger your personalized morning report right now
  !dayoff [DD.MM]         — open day-off selector for a specific date (PM only)
  !weekly                 — show current-week progress for your subscribed members
  !members                — list all people available for tracking (with Discord links)
  !linkdiscord <name> <@user|nick|id> — link a member to Discord (for daily-report checks)
  !addmember <id> <name>  — add a person by Renormalize ID (visible to everyone)
  !addperson <name> <@user|nick|id> — add a person without Renormalize (daily-report check only)
  !removemember <id|name> — remove a custom member (PM only)
  !setchannel [id]        — use this channel (or channel id) as YOUR daily-reports channel
  !reminders [on|off]     — evening DM to people in your subscription who haven't posted a report
  !findmembers            — list all Renormalize workspace members (PM only)

The implementation is split across modules: config.py (settings/roster), db.py (SQLite),
renormalize.py (hours API), reports/ (formatting + author detection), ui/ (Discord selectors),
routines.py (scheduled delivery), commands.py (all bot commands). This file just wires them up.
"""

from client import bot, scheduler
from config import PM_USER_ID, TOKEN, log
from db import init_db
from routines import check_report_time

import commands  # noqa: F401  (registers all !/slash commands via decorators)


@bot.event
async def on_ready() -> None:
    log.info("Logged in as %s (id=%s)", bot.user, bot.user.id)  # type: ignore[union-attr]

    init_db()

    if scheduler.running:            # on_ready fires again after reconnects
        return

    try:
        synced = await bot.tree.sync()
        log.info("Synced %d slash commands", len(synced))
    except Exception as exc:
        log.exception("Slash command sync failed: %s", exc)

    scheduler.add_job(
        check_report_time,
        trigger="cron",
        minute="*",          # fires every minute; sends only to users whose time matches
        id="check_report",
        replace_existing=True,
        kwargs={"bot": bot},
    )
    scheduler.start()
    log.info("Scheduler started — checking report times every minute (UTC+3)")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN is not set. Copy .env.example → .env and fill it in.")
    if not PM_USER_ID:
        raise SystemExit("PM_USER_ID is not set.")
    bot.run(TOKEN, log_handler=None)
