"""
Discord bot: daily team hours reports with day-off tracking and per-user subscriptions.

Morning routine (per-user configured time, default 09:00 UTC+2, Mon–Fri only):
  Personalized report for the PREVIOUS WORKDAY (on Monday — for Friday) — sent to every
  subscriber. Lists members more than 2h short of target (🔴), more than 3h over target
  (🔵 — overtime, listed separately under "OverTimes:"), or missing a daily report in the
  subscriber's own channel (!setchannel; default REPORTS_CHANNEL_ID), any message
  00:00–23:59 UTC+2. People on vacation/sick leave/absence in Renormalize that day are
  skipped (read live — no manual day-off entry).
  Weekly progress is appended only to the report for Friday.

Midday alert (MIDDAY_CHECK_TIME, default 13:00 UTC+2, Mon–Fri): for every subscriber, a DM
about anyone in their subscription with 0 hours logged so far today and no day off on
record — a heads-up, not necessarily a problem.

Evening reminder (REMINDER_TIME, default 19:00 UTC+2, Mon–Fri): for subscribers who ran
!reminders on, everyone in their subscription without a report today gets a DM.

Commands (also available as slash commands with autocomplete: /track, /report, …):
  !track <name>           — add one person to your subscription (searchable)
  !tracklist              — show who's in your subscription
  !untrack <name>         — remove one person from your subscription (searchable)
  !settime [HH:MM]        — set your daily report time (UTC+2). No arg = show current.
  !report                 — trigger your personalized morning report right now
  !weekly                 — show current-week progress for your subscribed members
  !monthly                — show who's behind this month (shortfall only)
  !members                — list all people available for tracking (with Discord links)
  !linkdiscord <name> <@user|nick|id> — link a member to Discord (for daily-report checks)
  !adddevelopertolist <name|id> — add a person found live in Renormalize (Lead only)
  !addperson <name> <@user|nick|id> — add a person without Renormalize (daily-report check only)
  !removedeveloperfromlist <id|name> — remove a custom member (Lead only)
  !setchannel [id]        — use this channel (or channel id) as YOUR daily-reports channel
  !reminders [on|off]     — evening DM to people in your subscription who haven't posted a report
  !renormalizeusers       — list all Renormalize workspace members (Lead only)
  !alloweduser add|remove|list — manage who can use the bot (Lead only)

The implementation is split across modules: config.py (settings/roster), db.py (SQLite),
renormalize.py (hours + day-off API), reports/ (formatting + author detection),
routines.py (scheduled delivery), commands.py (all bot commands).
This file just wires them up.
"""

from client import bot, scheduler
from config import LEAD_USER_ID, TOKEN, log
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
    log.info("Scheduler started — checking report times every minute (UTC+2)")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN is not set. Copy .env.example → .env and fill it in.")
    if not LEAD_USER_ID:
        raise SystemExit("LEAD_USER_ID is not set.")
    bot.run(TOKEN, log_handler=None)
