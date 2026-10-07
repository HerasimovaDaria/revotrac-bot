# Renormalize tracker (revotrac-bot)

A Discord bot that DMs each subscriber a short morning report: which of their people are
short on hours in Renormalize, and who hasn't posted a daily report in Discord. In the
evening it can also remind people who still haven't posted.

Split across modules (`config.py`, `db.py`, `renormalize.py`, `reports/`, `ui/`,
`routines.py`, `commands.py`) and wired together by `bot.py`. Stack: discord.py +
APScheduler + SQLite.

---

## How it works

### Core concepts

| Concept | What it is |
|---|---|
| **Tracked person** | Someone whose hours/reports can be followed. Either hardcoded in `TEAM` (`config.py`), or added at runtime with `/addmember` (Renormalize ID, hours tracked) or `/addperson` (no Renormalize, report check only). |
| **Subscriber** | A Discord user who receives a morning report. Picks who to follow with `/subscribe`. Any number of subscribers, each with their own list. |
| **Discord link** | "Tracked person → their Discord account" (`/linkdiscord`). Without it the bot can't tell which messages in a channel count as that person's report. |
| **Reports channel** | Where daily reports get posted. Each subscriber can set their own (`/setchannel`); falls back to `REPORTS_CHANNEL_ID` if unset. Channels can be on different servers — the bot just needs to be a member of each one. |
| **Day off** | Vacation, sick leave or other absence — read live from Renormalize (`/v1/vacations`), not entered manually. Skipped for both the hours and the report check. Only works for tracked people with a Renormalize ID; report-only people (`/addperson`) can't be checked this way. |
| **Allowed user** | Who's permitted to talk to the bot at all — a separate concept from "tracked person" above. See **Access control** below. |

### Morning report

Sent on workdays at the time each subscriber picked with `/settime` (default 09:00 UTC+3).

1. **Which day.** The last workday: Tue–Fri → yesterday, Monday → Friday. Nothing is sent on weekends.
2. **Hours.** Pulled from Renormalize (`/v1/time/progression`) and compared to the person's daily target:
   - target met — fine;
   - 🟡 short by less than an hour;
   - 🔴 short by more than an hour.
3. **Daily report check.** The bot reads the subscriber's reports channel for that day (00:00–23:59 UTC+3):
   - any message from the person themselves counts;
   - if another bot posted the report (e.g. a "Daily Reports" bot), the author is read from an embed field named `Developer`/`Author`/`User` (or their Russian equivalents, for reports already posted that way) — if a `Date` field names a different day, it doesn't count;
   - both plain text channels and forums work (forums: all posts, including archived ones).
4. **What shows up.** Only people with an issue: an hours shortfall and/or a missing report. Each flagged person also gets their month-to-date shortfall (hours still owed since the 1st of the month, at their daily target × workdays elapsed) — someone can be fine for the month but short today, or the other way round. Everyone with no issue is summarized as "The other N — no issues". People on vacation/sick leave/absence in Renormalize that day are skipped. A separate line lists anyone with no Discord link — their report can't be checked.
5. **Weekly and monthly.** Friday's report also includes weekly hours progress for everyone (same as `/weekly`, on demand any day). `/monthly` is separate — month-to-date, **only people who are behind** (everyone else is omitted, not just summarized).

Example:

```
### Friday, October 2
🔴 David · 6.8 of 8h today · 4.2h behind this month · no report
🟡 George · 7.5 of 8h today · on track this month
🟡 Jane Doe · no report
-# The other 4 — no issues
```

### Evening reminders

A subscriber turns them on with `/reminders on`. On workdays at `REMINDER_TIME` (default
19:00 UTC+3), the bot checks that subscriber's reports channel for **today**. Everyone in
their subscription without a report gets a DM. People on vacation/sick leave/absence that
day are skipped. Someone in several subscriptions still gets just one message.

### Access control

Two layers:

- **Lead (`LEAD_USER_ID`)** — one fixed Discord ID, set once in the environment. Always has
  access to everything, and is the only one who can run Lead-only commands:
  - `/removemember` — remove a manually-added person
  - `/findmembers` — list everyone in the Renormalize workspace with their ID
  - `/alloweduser` — manage who else can use the bot (see below)

  Without `LEAD_USER_ID` set, the bot refuses to start.

- **Allowed users** — everyone else (the PMs who actually use the bot day to day) needs
  explicit access, managed entirely at runtime (no redeploy needed):
  ```
  /alloweduser add      — grant access (pick a Discord nickname or paste an ID)
  /alloweduser remove   — revoke access
  /alloweduser list     — show who currently has access
  ```
  Anyone not on this list (and not the Lead) gets an explicit "🚫 You don't have access to
  this bot" instead of being silently ignored.

  The first time this feature runs, everyone who already had a subscription or a saved
  preference gets added automatically, so turning this on doesn't lock out the existing
  team. From then on it's manual.

### Who `/addmember` can suggest

`/addmember`'s name search (both the `/`-autocomplete and `!addmember`'s text matching) only
searches a curated candidate pool — `ADDMEMBER_CANDIDATE_IDS` in `config.py` — not the whole
Renormalize workspace. This keeps sales/HR/other departments' names and emails from being
surfaced to everyone with bot access. If you already know someone's Renormalize ID, you can
still add them directly (`/addmember <id>`) even if they're outside this pool — the
restriction only applies to search-by-name. To change who's suggestable, edit the set in
`config.py` and redeploy.

---

## Commands

Every command works both as `/command` (autocomplete, hints in Discord) and `!command`. On a
server, a `/` reply is only visible to you; a `!` reply comes as a DM.

| Command | What it does |
|---|---|
| `/start` | Quick guide |
| `/subscribe` | Choose who appears in your report (checkbox list — chunked past 25 people) |
| `/track <person>` | Add one person to your subscription — searchable (`!track` also takes a comma-separated list for bulk adds) |
| `/untrack <person>` | Remove one person from your subscription — searchable (same bulk-list support via `!untrack`) |
| `/settime 09:00` | Your morning report time (UTC+3); no argument shows the current one |
| `/report` | Get a report for the last workday right now |
| `/weekly` | Hours progress for the current week |
| `/monthly` | Who's behind this month — only people with a shortfall |
| `/members` | Everyone tracked, plus their Discord links |
| `/addmember <person>` | Add someone — start typing a name for live suggestions, or paste a Renormalize ID directly |
| `/addperson <name> <discord>` | Add someone without Renormalize — only their daily report is checked |
| `/linkdiscord <person> <discord>` | Link a tracked person to a Discord account (autocompletes both fields) |
| `/setchannel [channel_id]` | Set your reports channel — run it in the target channel, or pass an ID |
| `/reminders on\|off` | Evening reminders for your subscription |
| `/removemember <id or name>` | Remove a manually-added person (**Lead only**) |
| `/findmembers` | List everyone in Renormalize with their ID and status (**Lead only**) |
| `/alloweduser add\|remove\|list [discord]` | Manage who can use the bot (**Lead only**) |

---

## Typical scenarios

**The Lead wants to know which PMs haven't posted their own report**
1. Add the PMs: `/addperson <name> <discord>`.
2. Lead: `/subscribe` (pick the PMs) → `/setchannel` in the PMs' channel → `/reminders on` if they also want evening nudges.

**A PM tracks their developers' hours and reports**
1. If a developer isn't already tracked (check `/members`), use `/track <name>` — or `/addmember` if they're not in the roster at all yet.
2. Link them to Discord: `/linkdiscord`.
3. PM: `/setchannel` in the devs' channel → `/settime`.

**Granting a new PM access to the bot**
1. Lead: `/alloweduser add`, pick the person from the nickname suggestions (or paste their Discord ID).
2. They need to share at least one Discord server with the bot — Discord doesn't allow DMs between users/bots without one. If they're not already on a shared server, either add them to the team's server, or set up a small private server with just them and the bot invited to it.

---

## Setup and deployment

### Discord Developer Portal
1. Create an application and bot, copy the token.
2. **Bot → Privileged Gateway Intents:** enable **Message Content Intent**. Without it the bot can't see `!` commands or other bots' embed content.
3. **OAuth2 → URL Generator:** check scopes `bot` and `applications.commands`; permissions — View Channels, Send Messages, Read Message History, Add Reactions. Use the generated link to add the bot to each server you need.
4. In every reports channel, the bot needs **View Channel** and **Read Message History**.

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `DISCORD_BOT_TOKEN` | yes | Bot token |
| `LEAD_USER_ID` | yes | The Lead's Discord ID (see **Access control** above) |
| `RENORMALIZE_TOKEN` | recommended | Renormalize API JWT. Without it, hours are randomized (mock mode) |
| `REPORTS_CHANNEL_ID` | no | Default reports channel for subscribers who haven't run `/setchannel` |
| `DB_PATH` | no | SQLite file path, defaults to `hours.db` |
| `REMINDER_TIME` | no | Evening reminder time `HH:MM` (UTC+3), defaults to `19:00` |

See `.env.example` for a template.

### Running locally

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in the values
python3 bot.py
```

⚠️ If `DISCORD_BOT_TOKEN` is the same token already deployed elsewhere (e.g. on Railway),
**don't run this locally while that deployment is live** — two processes on one token means
duplicated replies and duplicated scheduled DMs to real people.

### Railway
1. Connect the repo: Railway runs `worker: python3 bot.py` from `Procfile` on every push to `main`.
2. Set the environment variables (table above).
3. **Attach a Volume** (e.g. mounted at `/data`) and set `DB_PATH=/data/hours.db`. Without
   it, subscriptions, links, channels and the allowed-users list all get wiped on every
   deploy.
4. Merging more than one PR in quick succession can race Railway's own build/deploy pipeline
   — the older commit can end up "winning" and staying active even though a newer, working
   build finished first. Wait for "Deployment successful" before merging the next PR.

---

## Data (SQLite)

| Table | What it stores |
|---|---|
| `subscriptions` | subscriber → people in their report |
| `preferences` | report time, own reports channel, reminders on/off |
| `custom_members` | people added via `/addmember` / `/addperson` (the latter get a negative ID and a 0h target) |
| `discord_links` | tracked person → Discord ID |
| `allowed_users` | Discord IDs allowed to use the bot (besides the Lead) — managed with `/alloweduser` |

Day offs aren't stored here at all — they're read live from Renormalize on every report
(see **Day off** above).

People defined in code (`TEAM` and `RENORMALIZE_IDS` in `config.py`) aren't stored in the
database — the `TEAM` names are also used as primary keys in several of the tables above, so
renaming them in code would orphan every existing row referencing the old name. To change the
core roster or someone's hour target, edit `config.py` and redeploy.
