# Renormalize tracker (revotrac-bot)

A Discord bot that DMs each subscriber a short morning report: which of their people are
short on hours in Renormalize, and who hasn't posted a daily report in Discord. In the
evening it can also remind people who still haven't posted.

Split across modules (`config.py`, `db.py`, `renormalize.py`, `reports/`,
`routines.py`, `commands.py`) and wired together by `bot.py`. Stack: discord.py +
APScheduler + SQLite.

---

## How it works

### Core concepts

| Concept | What it is |
|---|---|
| **Tracked person** | Someone whose hours/reports can be followed. Comes from one of three places in `config.py`/the database: `TEAM` (a handful of hardcoded people with real hour targets), `CANDIDATE_ROSTER` (a curated list of ~57 engineers, default 8h/day target, already trackable with `/track` — no `/adddevelopertolist` needed), or `custom_members` in the database (anyone added at runtime with `/adddevelopertolist` — Lead only — Renormalize ID + hours tracked, or `/addperson`, open to everyone, no Renormalize, report check only). |
| **Subscriber** | A Discord user who receives a morning report. Adds people one at a time with `/track` (searchable), removes with `/untrack`, `/tracklist` shows the current list. Any number of subscribers, each with their own list. |
| **Discord link** | "Tracked person → their Discord account" (`/linkdiscord`). Without it the bot can't tell which messages in a channel count as that person's report. |
| **Reports channel** | Where daily reports get posted. Each subscriber can set their own (`/setchannel`); falls back to `REPORTS_CHANNEL_ID` if unset. Channels can be on different servers — the bot just needs to be a member of each one. |
| **Day off** | Vacation, sick leave or other absence — read live from Renormalize (`/v1/vacations`), not entered manually. Skipped for both the hours and the report check. Only works for tracked people with a Renormalize ID; report-only people (`/addperson`) can't be checked this way. |
| **Allowed user** | Who's permitted to talk to the bot at all — a separate concept from "tracked person" above. See **Access control** below. |

### Morning report

Sent on workdays at the time each subscriber picked with `/settime` (default 09:00 UTC+2).

1. **Which day.** The last workday: Tue–Fri → yesterday, Monday → Friday. Nothing is sent on weekends.
2. **Hours.** Pulled from Renormalize (`/v1/time/progression`) and compared to the person's daily target:
   - 🔴 **under-track** — more than 2h short of target;
   - 🔵 **overtime** — more than 3h over target;
   - anything in between (short by ≤2h, or over by ≤3h) counts as fine and isn't shown.
3. **Daily report check.** The bot reads the subscriber's reports channel for that day (00:00–23:59 UTC+2):
   - any message from the person themselves counts;
   - if another bot posted the report (e.g. a "Daily Reports" bot), the author is read from an embed field named `Developer`/`Author`/`User` (or their Russian equivalents, for reports already posted that way) — if a `Date` field names a different day, it doesn't count;
   - both plain text channels and forums work (forums: all posts, including archived ones).
4. **What shows up.** Under-track (🔴) people are listed first; a missing report (🟡) is flagged the same way even for someone otherwise on target. Overtime (🔵) people are listed separately, under a **OverTimes:** heading at the bottom — kept apart so the main list stays about who needs attention. Each flagged person also gets their month-to-date delta (ahead or behind, since the 1st of the month at their daily target × workdays elapsed) — someone can be fine for the month but short today, or the other way round. Everyone with no issue (on target, report posted) is summarized as "The other N — no issues". People on vacation/sick leave/absence in Renormalize that day are skipped. A separate line lists anyone with no Discord link — their report can't be checked.
5. **Weekly and monthly.** Friday's report also includes weekly hours progress for everyone (same as `/weekly`, on demand any day). `/monthly` is separate — month-to-date, **only people who are behind** (everyone else is omitted, not just summarized).
6. **Which hours count, exactly.** `/report` (and the morning routine) always covers the last *full* workday — never today, since today isn't over yet and would just read as a false shortfall. `/weekly` and `/monthly`, run on demand, are live and include today's hours so far. The month-to-date figure embedded in `/report`'s line follows `/report`'s own cutoff (through yesterday), so it can read slightly differently from standing `/monthly` on the same day — intentional, not a bug.

Example:

```
### Friday, October 2
🔴 David · 4.5 of 8h today · 4.2h behind this month · no report
🟡 Jane Doe · no report
-# The other 3 — no issues

**OverTimes:**
🔵 Sergii · 11.6 of 8h today · over target today · 6.1h ahead this month
```

### Midday "hasn't started" alert

On workdays at `MIDDAY_CHECK_TIME` (default 13:00 UTC+2), each subscriber gets a DM about
anyone in their subscription with **0 hours logged so far today** and no day off/sick
leave/absence on record. Doesn't necessarily mean something's wrong — they might just not
have started yet, or Renormalize hasn't synced their leave — but it surfaces it mid-day
instead of only finding out in tomorrow's report.

### Evening reminders

A subscriber turns them on with `/reminders on`. On workdays at `REMINDER_TIME` (default
19:00 UTC+2), the bot checks that subscriber's reports channel for **today**. Everyone in
their subscription without a report gets a DM. People on vacation/sick leave/absence that
day are skipped. Someone in several subscriptions still gets just one message.

### Access control

Two layers:

- **Lead (`LEAD_USER_ID`)** — one fixed Discord ID, set once in the environment. Always has
  access to everything, and is the only one who can run Lead-only commands:
  - `/adddevelopertolist` — add a person found live in Renormalize
  - `/removedeveloperfromlist` — remove a manually-added person
  - `/renormalizeusers` — list everyone in the Renormalize workspace with their ID
  - `/alloweduser` — manage who else can use the bot (see below)

  Anyone else who tries `/adddevelopertolist` gets pointed to the Lead instead of a bare
  "no access" — they can still ask to have someone added, just not do it themselves.

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

### Who `/adddevelopertolist` can suggest

`/adddevelopertolist`'s name search (both the `/`-autocomplete and `!adddevelopertolist`'s text matching) only
searches a curated candidate pool — `ADDMEMBER_CANDIDATE_IDS` in `config.py` — not the whole
Renormalize workspace. This keeps sales/HR/other departments' names and emails from being
surfaced to everyone with bot access. If you already know someone's Renormalize ID, you can
still add them directly (`/adddevelopertolist <id>`) even if they're outside this pool — the
restriction only applies to search-by-name. To change who's suggestable, edit the set in
`config.py` and redeploy.

Note this is a different list from `CANDIDATE_ROSTER` (the "Tracked person" row above):
`ADDMEMBER_CANDIDATE_IDS` controls who `/adddevelopertolist` can *find and add*; `CANDIDATE_ROSTER`
is the people already added — baked into the roster, trackable with `/track` right away.

---

## Commands

Every command works both as `/command` (autocomplete, hints in Discord) and `!command`. On a
server, a `/` reply is only visible to you; a `!` reply comes as a DM.

| Command | What it does |
|---|---|
| `/start` | Quick guide — main commands up front: `/track`, `/untrack`, `/tracklist`, `/settime`, `/report` |
| `/track <person>` | Add one person to your subscription — searchable, start typing a name |
| `/untrack <person>` | Remove one person from your subscription — searchable |
| `/tracklist` | Show who's in your subscription |
| `/settime 09:00` | Your morning report time (UTC+2); no argument shows the current one |
| `/report` | Get a report for the last workday right now |
| `/weekly` | Hours progress for the current week |
| `/monthly` | Who's behind this month — only people with a shortfall |
| `/members` | Everyone tracked, plus their Discord links |
| `/adddevelopertolist <person>` | Add someone — start typing a name for live suggestions, or paste a Renormalize ID directly (**Lead only**) |
| `/addperson <name> <discord>` | Add someone without Renormalize — only their daily report is checked |
| `/linkdiscord <person> <discord>` | Link a tracked person to a Discord account (autocompletes both fields) |
| `/setchannel [channel_id]` | Set your reports channel — run it in the target channel, or pass an ID |
| `/reminders on\|off` | Evening reminders for your subscription |
| `/removedeveloperfromlist <id or name>` | Remove a manually-added person (**Lead only**) |
| `/renormalizeusers` | List everyone in Renormalize with their ID and status (**Lead only**) |
| `/alloweduser add\|remove\|list [discord]` | Manage who can use the bot (**Lead only**) |

---

## Typical scenarios

**The Lead wants to know which PMs haven't posted their own report**
1. Add the PMs: `/addperson <name> <discord>`.
2. Lead: `/track <name>` for each PM → `/setchannel` in the PMs' channel → `/reminders on` if they also want evening nudges.

**A PM tracks their developers' hours and reports**
1. `/track <name>` — most developers are already in `CANDIDATE_ROSTER`, so this alone adds them. If a name doesn't come up (check `/members`), ask the Lead to run `/adddevelopertolist` (Lead only), then `/track` them.
2. Link them to Discord: `/linkdiscord`.
3. PM: `/setchannel` in the devs' channel → `/settime`.
4. `/tracklist` any time, to see who's currently tracked.

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
| `REMINDER_TIME` | no | Evening reminder time `HH:MM` (UTC+2), defaults to `19:00` |
| `MIDDAY_CHECK_TIME` | no | "Hasn't started work" alert time `HH:MM` (UTC+2), defaults to `13:00` |

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
| `custom_members` | people added via `/adddevelopertolist` / `/addperson` (the latter get a negative ID and a 0h target) |
| `discord_links` | tracked person → Discord ID |
| `allowed_users` | Discord IDs allowed to use the bot (besides the Lead) — managed with `/alloweduser` |

Day offs aren't stored here at all — they're read live from Renormalize on every report
(see **Day off** above).

People defined in code (`TEAM`, `RENORMALIZE_IDS` and `CANDIDATE_ROSTER` in `config.py`)
aren't stored in the database — their names are also used as primary keys in several of the
tables above, so renaming them in code would orphan every existing row referencing the old
name. To change the core roster or someone's hour target, edit `config.py` and redeploy.

If someone was added with `/adddevelopertolist` *before* they existed in `CANDIDATE_ROSTER`, they'd
end up in both the hardcoded roster and `custom_members` — `_deduped_custom_members()` in
`db.py` filters `custom_members` against everyone already baked into code, so they're only
ever counted once in reports and `/members`.
