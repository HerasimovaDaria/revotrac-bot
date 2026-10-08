"""Bot commands (hybrid !cmd / slash /cmd) and their autocomplete providers."""

from datetime import datetime
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from client import bot
from config import (
    ADDMEMBER_CANDIDATE_IDS,
    CANDIDATE_ROSTER,
    LEAD_USER_ID,
    REMINDER_HOUR,
    REMINDER_MINUTE,
    RENORMALIZE_API_KEY,
    TEAM,
    UTC2,
    log,
)
from db import (
    _all_members,
    _all_renormalize_ids,
    _deduped_custom_members,
    add_allowed_user,
    add_custom_member,
    add_report_only_member,
    get_allowed_users,
    get_custom_members,
    get_discord_links,
    get_preference,
    get_reminders,
    get_reports_channel,
    get_subscription,
    remove_allowed_user,
    remove_custom_member,
    save_discord_link,
    save_preference,
    save_reminders,
    save_reports_channel,
    save_subscription,
)
from renormalize import fetch_all_renormalize_users, fetch_month_hours, fetch_week_hours
from reports.formatting import format_monthly_report, format_weekly_report
from routines import _build_report_text, _collect_report_data
from utils import _find_member, _resolve_discord_user, previous_workday, week_start

# ---------------------------------------------------------------------------
# Reply helpers
# ---------------------------------------------------------------------------
# Hybrid commands work both as "!cmd" and as "/cmd" (slash, with autocomplete in Discord).
# Slash calls have no real message to react to, so replies go through these helpers.

async def _reply(ctx: commands.Context, content: str, **kwargs) -> None:
    """Slash → answer in place (only you see it on servers). Prefix → DM (+📨 on servers)."""
    if ctx.interaction:
        await ctx.send(content, ephemeral=ctx.guild is not None, **kwargs)
    else:
        await ctx.author.send(content, **kwargs)
        if ctx.guild:
            await ctx.message.add_reaction("📨")


async def _working(ctx: commands.Context) -> None:
    """Show that a slow command is in progress."""
    if ctx.interaction:
        await ctx.defer(ephemeral=ctx.guild is not None)
    else:
        await ctx.message.add_reaction("⏳")


async def _deny(ctx: commands.Context) -> None:
    """Lead-only command called by someone else."""
    if ctx.interaction:
        await ctx.send("🚫 Lead only.", ephemeral=True)
    else:
        await ctx.message.add_reaction("🚫")


@bot.hybrid_command(name="report", description="Get a report for the last workday right now")
async def cmd_report(ctx: commands.Context) -> None:
    """!report — trigger your personalized morning report right now."""
    user_id = ctx.author.id
    members = get_subscription(user_id)

    if not members:
        await _reply(
            ctx,
            "⚠️ You don't have a subscription.\n"
            "Use `/track <name>` to add people."
        )
        return

    await _working(ctx)

    today     = datetime.now(UTC2).date()
    yesterday = previous_workday(today)

    hours, day_offs, week_hours, month_hours, authors = await _collect_report_data(bot, yesterday)

    text = await _build_report_text(bot, user_id, yesterday, hours, week_hours, month_hours,
                                    day_offs, members, authors)
    await _reply(ctx, text)


@bot.hybrid_command(name="start", description="What this bot does and how to set it up")
async def cmd_start(ctx: commands.Context) -> None:
    """!start — onboarding: show what this bot does and how to set it up."""
    text = (
        "**Renormalize Tracker** — DMs you a morning report on your team's hours and "
        "daily reports. All times are **UTC+2**.\n\n"
        "**Do this to get it working:**\n"
        "```\n"
        "/track <name>        — add one person you want reports on (repeat for each)\n"
        "/settime 09:00       — when you want your report (UTC+2)\n"
        "```\n"
        "That's it — tomorrow morning you'll get a DM with only the people who need "
        "attention. Vacations and sick leave are detected automatically.\n\n"
        "**Main commands:**\n"
        "`/track <name>` — add one person, searchable\n"
        "`/untrack <name>` — remove one person\n"
        "`/tracklist` — see who you're tracking\n"
        "`/settime HH:MM` — set or check your report time\n"
        "`/report` — get your report right now, don't wait for tomorrow\n\n"
        "**Also useful:** `/weekly` (week progress) · `/monthly` (who's behind this month) · "
        "`/members` (who's tracked)\n\n"
        "-# No access? Ask the Lead for `/alloweduser add`."
    )
    await _reply(ctx, text)


@bot.hybrid_command(name="weekly", description="Hours progress for the current week")
async def cmd_weekly(ctx: commands.Context) -> None:
    """!weekly — show current-week progress for your subscribed members."""
    user_id = ctx.author.id
    members = get_subscription(user_id)

    if not members:
        await _reply(
            ctx,
            "⚠️ You don't have a subscription.\n"
            "Use `/track <name>` to add people."
        )
        return

    await _working(ctx)

    today      = datetime.now(UTC2).date()
    wb         = week_start(today)
    week_hours = await fetch_week_hours(wb)
    text       = format_weekly_report(wb, week_hours, members, today) or "None of your people have a weekly hour target."

    await _reply(ctx, text)


@bot.hybrid_command(name="monthly", description="Who's behind this month — only people with a shortfall")
async def cmd_monthly(ctx: commands.Context) -> None:
    """!monthly — show month-to-date hours shortfall for your subscribed members (behind only)."""
    user_id = ctx.author.id
    members = get_subscription(user_id)

    if not members:
        await _reply(
            ctx,
            "⚠️ You don't have a subscription.\n"
            "Use `/track <name>` to add people."
        )
        return

    await _working(ctx)

    today = datetime.now(UTC2).date()
    try:
        month_hours = await fetch_month_hours(today)
    except Exception as exc:
        log.exception("fetch_month_hours failed: %s", exc)
        await _reply(ctx, "❌ Couldn't fetch hours from Renormalize. Try again in a bit.")
        return

    text = format_monthly_report(today, month_hours, members)
    await _reply(ctx, text)


@bot.hybrid_command(name="members", description="List of tracked people and their Discord links")
async def cmd_members(ctx: commands.Context) -> None:
    """!members — list all people available for tracking."""
    lines = ["**👥 All tracked people:**\n"]
    all_ids = _all_renormalize_ids()
    links   = get_discord_links()

    def link_str(name: str) -> str:
        uid = links.get(name)
        return f" · <@{uid}>" if uid else " · ⛓️‍💥 no Discord"

    for name, en_name, daily, _ in TEAM:
        rid = all_ids.get(name, "?")
        lines.append(f"`{rid}` — {en_name}  ({daily:.0f}h/day){link_str(name)}")

    if CANDIDATE_ROSTER:
        lines.append("\n**Engineering roster:**")
        for rid, name in sorted(CANDIDATE_ROSTER, key=lambda m: m[1]):
            lines.append(f"`{rid}` — {name}  (8h/day){link_str(name)}")

    custom = _deduped_custom_members()
    if custom:
        lines.append("\n**Added manually:**")
        for rid, dname, daily, _ in custom:
            if rid < 0:
                lines.append(f"{dname}  (reports only){link_str(dname)}")
            else:
                lines.append(f"`{rid}` — {dname}  ({daily:.0f}h/day){link_str(dname)}")

    lines.append("\n➕ Add: `!adddevelopertolist <renormalize_id> <name>`")
    lines.append("📝 No hours, reports only: `!addperson <name> <ID>`")
    lines.append("🔗 Link Discord: `!linkdiscord <name> @user`")

    # Split if over Discord's 2000-char limit (easily happens with the full roster)
    text = "\n".join(lines)
    for chunk in [text[i:i + 1900] for i in range(0, len(text), 1900)]:
        await ctx.send(chunk, allowed_mentions=discord.AllowedMentions.none())


@bot.command(name="linkdiscord")
async def cmd_linkdiscord(ctx: commands.Context, *, args: str = "") -> None:
    """!linkdiscord <name> <@user | nick | id> — link a member to their Discord account."""
    parts = args.split()
    if len(parts) < 2:
        await ctx.send(
            "❌ Format: `!linkdiscord <name> <@user | nick | Discord ID>`\n"
            "Example: `!linkdiscord Samvel @samvel` or `!linkdiscord Aleksey Siedin 954344819092783184`"
        )
        return

    name, spec = " ".join(parts[:-1]), parts[-1]
    member = _find_member(name)
    if member is None:
        await ctx.send(f"❌ No match for «{name}» (or more than one). List: `!members`")
        return

    user = await _resolve_discord_user(spec, ctx.message.mentions)
    if user is None:
        await ctx.send(
            f"❌ Couldn't find Discord user «{spec}».\n"
            "Give a nickname (as in their profile, no spaces) or a Discord ID "
            "(right-click them → \"Copy ID\", needs Developer Mode)."
        )
        return

    save_discord_link(member, user.id)
    await ctx.send(
        f"✅ **{member}** linked to {user.mention}.",
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def _renormalize_user_autocomplete(
    interaction: discord.Interaction, current: str,
) -> list[app_commands.Choice[str]]:
    """Live-search Renormalize accounts by name or email — curated candidates only
    (ADDMEMBER_CANDIDATE_IDS), active, not already tracked. A known ID outside this
    list can still be added directly via /adddevelopertolist's numeric-ID path —
    this only limits what gets *suggested* by name, so the bot doesn't surface the
    whole company directory (sales, HR, other departments, …)."""
    try:
        users = await fetch_all_renormalize_users()
    except Exception as exc:
        log.warning("renormalize user autocomplete failed: %s", exc)
        return []
    existing_ids = {rid for rid in _all_renormalize_ids().values() if rid}
    cur = current.casefold().strip()
    matches = [
        u for u in users
        if u.get("id") in ADDMEMBER_CANDIDATE_IDS
        and u.get("status") == "active" and u.get("id") not in existing_ids
        and (not cur or cur in u.get("name", "").casefold() or cur in u.get("email", "").casefold())
    ]
    return [
        app_commands.Choice(name=f"{u['name']} ({u.get('email', '')})"[:100], value=str(u["id"]))
        for u in matches[:25]
    ]


@bot.hybrid_command(name="adddevelopertolist", description="Add a person — start typing a name, Renormalize suggests it (Lead only)")
@app_commands.describe(person="Start typing a name — pick a suggestion (or paste a Renormalize ID)")
@app_commands.rename(person="person")
@app_commands.autocomplete(person=_renormalize_user_autocomplete)
async def cmd_add_developer_to_list(ctx: commands.Context, *, person: str) -> None:
    """!adddevelopertolist <name or Renormalize ID> — add a person found live in Renormalize (Lead only)."""
    if ctx.author.id != LEAD_USER_ID:
        msg = f"🚫 Please contact <@{LEAD_USER_ID}> to add a developer."
        if ctx.interaction:
            await ctx.send(msg, ephemeral=ctx.guild is not None)
        else:
            await ctx.send(msg)
        return

    person = person.strip()
    if not person:
        await ctx.send("❌ Give a name or Renormalize ID. Example: `!adddevelopertolist Ivan Petrov`.")
        return

    try:
        users = await fetch_all_renormalize_users()
    except Exception as exc:
        log.exception("adddevelopertolist: fetch_all_renormalize_users failed: %s", exc)
        await ctx.send("❌ Couldn't fetch the list from Renormalize. Try again in a bit.")
        return

    existing_ids = {rid for rid in _all_renormalize_ids().values() if rid}

    if person.isdigit():
        renorm_id = int(person)
        match     = next((u for u in users if u.get("id") == renorm_id), None)
        if match is None:
            await ctx.send(f"❌ No Renormalize user with ID `{renorm_id}`.")
            return
    else:
        q          = person.casefold()
        candidates = [
            u for u in users
            if u.get("id") in ADDMEMBER_CANDIDATE_IDS
            and u.get("status") == "active" and u.get("id") not in existing_ids
            and q in u.get("name", "").casefold()
        ]
        if not candidates:
            await ctx.send(
                f"❌ No match for «{person}» in Renormalize. Check the spelling — "
                f"or use `/adddevelopertolist`, it has live suggestions."
            )
            return
        if len(candidates) > 1:
            lines = "\n".join(f"• {u['name']} — id `{u['id']}`" for u in candidates[:10])
            await ctx.send(
                f"⚠️ Found several matches for «{person}»:\n{lines}\n\n"
                f"Be more specific, or use `/adddevelopertolist` with suggestions."
            )
            return
        match     = candidates[0]
        renorm_id = match["id"]

    if renorm_id in existing_ids:
        await ctx.send(f"⚠️ **{match['name']}** is already in the list.")
        return

    add_custom_member(renorm_id, match["name"])
    await ctx.send(
        f"✅ Added: **{match['name']}** (id `{renorm_id}`)\n"
        f"Track them: `!track {match['name']}`"
    )


async def _custom_member_autocomplete(
    interaction: discord.Interaction, current: str,
) -> list[app_commands.Choice[str]]:
    """Only people actually in custom_members — the ones /removedeveloperfromlist can act on
    (TEAM and CANDIDATE_ROSTER are baked into the code, not removable this way)."""
    cur = current.casefold()
    return [
        app_commands.Choice(name=f"{dname} (id {rid})"[:100], value=dname)
        for rid, dname, _, _ in get_custom_members()
        if cur in dname.casefold()
    ][:25]


@bot.hybrid_command(name="removedeveloperfromlist", description="Remove a manually-added person (Lead only)")
@app_commands.describe(arg="Renormalize ID or name")
@app_commands.rename(arg="who")
@app_commands.autocomplete(arg=_custom_member_autocomplete)
async def cmd_remove_developer_from_list(ctx: commands.Context, *, arg: str) -> None:
    """!removedeveloperfromlist <renormalize_id | name> — remove a custom member (Lead only)."""
    if ctx.author.id != LEAD_USER_ID:
        await _deny(ctx)
        return

    arg = arg.strip()
    if arg.isdigit():
        renorm_id = int(arg)
    else:
        name      = _find_member(arg)
        found     = [rid for rid, dname, _, _ in get_custom_members() if dname == name]
        renorm_id = found[0] if found else 0

    deleted = bool(renorm_id) and remove_custom_member(renorm_id)
    if deleted:
        await ctx.send(f"✅ «{arg}» removed from the list.")
    else:
        await ctx.send(f"⚠️ «{arg}» not found among manually-added people.")


@bot.command(name="addperson")
async def cmd_addperson(ctx: commands.Context, *, args: str = "") -> None:
    """!addperson <name> <@user | nick | id> — add a person without Renormalize (reports only)."""
    parts = args.split()
    if len(parts) < 2:
        await ctx.send(
            "❌ Format: `!addperson <name> <@user | nick | Discord ID>`\n"
            "Example: `!addperson Jane Doe 954344819092783184`"
        )
        return

    name, spec = " ".join(parts[:-1]), parts[-1]
    if any(name.casefold() in (n.casefold(), en.casefold()) for n, en, _, _ in _all_members()):
        await ctx.send(f"⚠️ «{name}» is already in the list. Link Discord: `!linkdiscord {name} <ID>`")
        return

    user = await _resolve_discord_user(spec, ctx.message.mentions)
    if user is None:
        await ctx.send(f"❌ Couldn't find Discord user «{spec}». Better to give a Discord ID.")
        return

    add_report_only_member(name, user.id)
    await ctx.send(
        f"✅ Added **{name}** ({user.mention}) — no hours, only the daily-report check.\n"
        f"Track them: `!track {name}`",
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.hybrid_command(name="setchannel", description="Make this channel (or a channel by ID) your daily-reports channel")
@app_commands.describe(channel_id="Channel ID; leave empty for the current channel")
@app_commands.rename(channel_id="channel_id")
async def cmd_setchannel(ctx: commands.Context, channel_id: Optional[str] = None) -> None:
    """!setchannel [channel_id] — set YOUR daily-reports channel (run it in that channel or pass an ID)."""
    if channel_id is not None and not channel_id.strip().isdigit():
        await _reply(ctx, "❌ A channel ID is a number. Right-click the channel → \"Copy Channel ID\".")
        return

    if channel_id is None and ctx.guild is None:
        current = get_reports_channel(ctx.author.id)
        await _reply(ctx,
            (f"📝 Your reports channel: <#{current}> (`{current}`).\n" if current
             else "📝 No reports channel set — reports aren't checked.\n")
            + "Change it: run `!setchannel` right in the target channel (or forum post), "
              "or `!setchannel <channel_id>`."
        )
        return

    try:
        if channel_id is None:
            channel = ctx.channel
        else:
            cid     = int(channel_id)
            channel = bot.get_channel(cid) or await bot.fetch_channel(cid)
    except discord.HTTPException:
        await _reply(ctx, f"❌ Channel `{channel_id}` not found or the bot can't access it.")
        return

    # A command typed inside a thread / forum post → use the parent channel
    if isinstance(channel, discord.Thread) and channel.parent is not None:
        channel = channel.parent

    guild = getattr(channel, "guild", None)
    if guild is None:
        await _reply(ctx, "❌ That's not a server channel. Run `!setchannel` in your reports channel.")
        return
    if not channel.permissions_for(guild.me).read_message_history:
        await _reply(
            ctx,
            f"⚠️ The bot doesn't have \"Read Message History\" in <#{channel.id}> — "
            f"grant it in the channel settings and try again."
        )
        return

    save_reports_channel(ctx.author.id, channel.id)
    await _reply(
        ctx,
        f"✅ Done: your reports will check channel **#{channel.name}** "
        f"on server **{guild.name}**."
    )


@bot.hybrid_command(name="reminders", description="Evening DM to people who haven't posted a report (on/off)")
@app_commands.describe(mode="on — enable, off — disable; leave empty to show status")
@app_commands.rename(mode="mode")
@app_commands.choices(mode=[app_commands.Choice(name="on", value="on"),
                            app_commands.Choice(name="off", value="off")])
async def cmd_reminders(ctx: commands.Context, mode: Optional[str] = None) -> None:
    """!reminders [on|off] — evening DM to people in your subscription who haven't posted a report."""
    when = f"{REMINDER_HOUR:02d}:{REMINDER_MINUTE:02d} UTC+2"
    if mode is None:
        state = "on" if get_reminders(ctx.author.id) else "off"
        await ctx.send(
            f"🔔 Reminders are {state}. When on, at {when} on workdays the bot DMs "
            f"everyone in your subscription who hasn't posted a report in your channel yet.\n"
            f"Turn on: `/reminders on`, turn off: `/reminders off`",
            ephemeral=True,
        )
        return

    mode = mode.strip().lower()
    if mode not in ("on", "off"):
        await ctx.send("❌ Use `on` or `off`.", ephemeral=True)
        return
    enabled = mode == "on"
    save_reminders(ctx.author.id, enabled)
    if enabled:
        channel_id = get_reports_channel(ctx.author.id)
        note = "" if channel_id else "\n⚠️ No reports channel set — run `/setchannel` there."
        await ctx.send(
            f"🔔 Enabled: at {when} on workdays, people in your subscription without a report get a reminder.{note}",
            ephemeral=True,
        )
    else:
        await ctx.send("🔕 Reminders disabled.", ephemeral=True)


@bot.hybrid_command(name="settime", description="Your daily report time (UTC+2), e.g. 09:00")
@app_commands.describe(time_str="HH:MM; leave empty to show the current time")
@app_commands.rename(time_str="time")
async def cmd_settime(ctx: commands.Context, time_str: Optional[str] = None) -> None:
    """!settime [HH:MM] — set your daily report time (UTC+2). No arg = show current."""
    if time_str is None:
        h, m = get_preference(ctx.author.id)
        await ctx.send(
            f"🕐 Your current report time: **{h:02d}:{m:02d} UTC+2**.\n"
            f"Change it: `!settime 08:30`"
        )
        return

    try:
        parts  = time_str.strip().split(":")
        hour   = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("out of range")
    except (ValueError, IndexError):
        await ctx.send("❌ Invalid format. Example: `!settime 09:00` or `!settime 8:30`")
        return

    save_preference(ctx.author.id, hour, minute)
    await ctx.send(
        f"✅ Daily report time set: **{hour:02d}:{minute:02d} UTC+2**.\n"
        f"Haven't picked people yet? Use `!track <name>` to add someone."
    )


async def _member_autocomplete(
    interaction: discord.Interaction, current: str,
) -> list[app_commands.Choice[str]]:
    cur = current.casefold()
    return [
        app_commands.Choice(name=n[:100], value=n)
        for n, en, _, _ in _all_members()
        if cur in n.casefold() or cur in en.casefold()
    ][:25]


@bot.hybrid_command(name="tracklist", description="Show who you're currently tracking")
async def cmd_tracklist(ctx: commands.Context) -> None:
    """!tracklist — show the people in your own subscription."""
    current = get_subscription(ctx.author.id)
    if not current:
        await ctx.send("You're not tracking anyone yet. `/track <name>` to add someone.")
        return
    names = "\n".join(f"• {n}" for n in current)
    await ctx.send(f"**You're tracking {len(current)}:**\n{names}")


@bot.hybrid_command(name="track", description="Add one person to your subscription — searchable")
@app_commands.describe(member="Start typing a name")
@app_commands.rename(member="person")
@app_commands.autocomplete(member=_member_autocomplete)
async def cmd_track(ctx: commands.Context, *, member: str) -> None:
    """!track <name> — add one person to your subscription (searchable). See /tracklist for
    who you're currently tracking."""
    name = _find_member(member)
    if name is None:
        await ctx.send(f"❌ No match for «{member}» (or more than one). Check the spelling, or `/members` to browse everyone.")
        return

    current = get_subscription(ctx.author.id)
    if name in current:
        await ctx.send(f"⚠️ **{name}** is already in your subscription.")
        return

    save_subscription(ctx.author.id, current + [name])
    await ctx.send(f"✅ Added **{name}** to your subscription.")


@bot.hybrid_command(name="untrack", description="Remove one person from your subscription — searchable")
@app_commands.describe(member="Start typing a name")
@app_commands.rename(member="person")
@app_commands.autocomplete(member=_member_autocomplete)
async def cmd_untrack(ctx: commands.Context, *, member: str) -> None:
    """!untrack <name> — remove one person from your subscription (searchable)."""
    name = _find_member(member)
    if name is None:
        await ctx.send(f"❌ No match for «{member}» (or more than one).")
        return

    current = get_subscription(ctx.author.id)
    if name not in current:
        await ctx.send(f"⚠️ **{name}** isn't in your subscription.")
        return

    save_subscription(ctx.author.id, [n for n in current if n != name])
    await ctx.send(f"✅ Removed **{name}** from your subscription.")


async def _discord_user_autocomplete(
    interaction: discord.Interaction, current: str,
) -> list[app_commands.Choice[str]]:
    """Search people on all the bot's servers — works in DMs too, unlike Discord's user picker."""
    query = current.lstrip("@").strip()
    if not query or query.isdigit():
        return []
    seen: dict[int, str] = {}
    for guild in bot.guilds:
        try:
            for m in await guild.query_members(query=query, limit=10):
                seen.setdefault(m.id, f"{m.display_name} (@{m.name}) · {guild.name}"[:100])
        except Exception as exc:
            log.warning("autocomplete query_members failed in %s: %s", guild, exc)
    return [app_commands.Choice(name=label, value=str(uid)) for uid, label in seen.items()][:25]


@bot.tree.command(name="linkdiscord", description="Link a tracked person to their Discord account")
@app_commands.describe(member="Person", user="Start typing a nickname — or paste a Discord ID")
@app_commands.rename(member="person", user="discord")
@app_commands.autocomplete(member=_member_autocomplete, user=_discord_user_autocomplete)
async def slash_linkdiscord(interaction: discord.Interaction, member: str, user: str) -> None:
    ephemeral = interaction.guild is not None
    name      = _find_member(member)
    if name is None:
        await interaction.response.send_message(
            f"❌ No match for «{member}». Pick one from the suggestions.", ephemeral=True)
        return
    target = await _resolve_discord_user(user)
    if target is None:
        await interaction.response.send_message(
            f"❌ Couldn't find Discord user «{user}». Pick a suggestion or paste an ID.",
            ephemeral=True)
        return
    save_discord_link(name, target.id)
    await interaction.response.send_message(
        f"✅ **{name}** linked to {target.mention}.",
        ephemeral=ephemeral, allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(name="addperson", description="Add a person without Renormalize (daily-report check only)")
@app_commands.describe(name="Name for reports", user="Start typing a nickname — or paste a Discord ID")
@app_commands.rename(name="name", user="discord")
@app_commands.autocomplete(user=_discord_user_autocomplete)
async def slash_addperson(interaction: discord.Interaction, name: str, user: str) -> None:
    ephemeral = interaction.guild is not None
    name      = name.strip()
    if any(name.casefold() in (n.casefold(), en.casefold()) for n, en, _, _ in _all_members()):
        await interaction.response.send_message(
            f"⚠️ «{name}» is already in the list. Link Discord: `/linkdiscord`", ephemeral=True)
        return
    target = await _resolve_discord_user(user)
    if target is None:
        await interaction.response.send_message(
            f"❌ Couldn't find Discord user «{user}». Pick a suggestion or paste an ID.",
            ephemeral=True)
        return
    add_report_only_member(name, target.id)
    await interaction.response.send_message(
        f"✅ Added **{name}** ({target.mention}) — no hours, only the daily-report check.\n"
        f"Track them: `/track {name}`",
        ephemeral=ephemeral, allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.hybrid_command(name="renormalizeusers", description="List everyone in Renormalize with their ID (Lead only)")
async def cmd_renormalize_users(ctx: commands.Context) -> None:
    """!renormalizeusers — list all Renormalize workspace members with their IDs (Lead only)."""
    if ctx.author.id != LEAD_USER_ID:
        await _deny(ctx)
        return
    if not RENORMALIZE_API_KEY:
        await _reply(ctx, "❌ RENORMALIZE_API_KEY is not set in .env")
        return

    await _working(ctx)

    try:
        members = await fetch_all_renormalize_users(force=True)
    except Exception as exc:
        log.exception("renormalizeusers API error: %s", exc)
        user = await bot.fetch_user(LEAD_USER_ID)
        await user.send(f"❌ Renormalize request failed:\n```{exc}```")
        await _reply(ctx, "❌ Renormalize request failed, details sent to your DMs.")
        return

    if not members:
        await _reply(ctx, "⚠️ Renormalize returned an empty list.")
        return

    lines = ["👥 **People in Renormalize (ID — Name — status):**\n"]
    for m in sorted(members, key=lambda x: (x.get("status", "?"), x.get("name", ""))):
        uid    = m.get("id") or m.get("user_id") or "?"
        name   = (
            m.get("full_name") or m.get("name")
            or f"{m.get('first_name','')} {m.get('last_name','')}".strip()
            or m.get("username") or "?"
        )
        status = m.get("status", "?")
        lines.append(f"`{uid}` — **{name}** ({status})")

    text = "\n".join(lines)
    user = await bot.fetch_user(LEAD_USER_ID)
    # Split if over Discord's 2000-char limit
    for chunk in [text[i:i+1900] for i in range(0, len(text), 1900)]:
        await user.send(chunk)
    await _reply(ctx, f"✅ Sent you the list in DMs ({len(members)} people).")


@bot.hybrid_command(name="alloweduser",
                    description="Who can use the bot — add / remove / list (Lead only)")
@app_commands.describe(action="add — grant access, remove — revoke access, list — show the list",
                       user="Start typing a nickname — or paste a Discord ID (not needed for list)")
@app_commands.rename(action="action", user="discord")
@app_commands.choices(action=[
    app_commands.Choice(name="add — grant access", value="add"),
    app_commands.Choice(name="remove — revoke access", value="remove"),
    app_commands.Choice(name="list — show the list", value="list"),
])
@app_commands.autocomplete(user=_discord_user_autocomplete)
async def cmd_alloweduser(ctx: commands.Context, action: str, user: Optional[str] = None) -> None:
    """!alloweduser <add|remove|list> [discord] — manage who can use the bot (Lead only)."""
    if ctx.author.id != LEAD_USER_ID:
        await _deny(ctx)
        return

    action = action.strip().lower()

    if action == "list":
        ids = get_allowed_users()
        header = "✅ **Access: you (Lead) and:**\n" if ids else "Only you (Lead) have access — the list is empty."
        lines  = "\n".join(f"• <@{uid}> (`{uid}`)" for uid in ids)
        await _reply(ctx, header + lines, allowed_mentions=discord.AllowedMentions.none())
        return

    if action not in ("add", "remove"):
        await _reply(ctx, "❌ Action: `add`, `remove` or `list`.")
        return

    if not user:
        await _reply(ctx, "❌ Say who to add/remove — a nickname, mention, or Discord ID.")
        return

    target = await _resolve_discord_user(user, ctx.message.mentions)
    if target is None:
        await _reply(ctx, f"❌ Couldn't find Discord user «{user}». Pick a suggestion or paste an ID.")
        return

    if action == "add":
        add_allowed_user(target.id)
        await _reply(ctx, f"✅ {target.mention} can now use the bot.",
                     allowed_mentions=discord.AllowedMentions.none())
    else:
        if target.id == LEAD_USER_ID:
            await _reply(ctx, "⚠️ Can't remove yourself (Lead) — you always have access.")
            return
        removed = remove_allowed_user(target.id)
        msg = (f"✅ {target.mention} can no longer use the bot." if removed
               else f"⚠️ {target.mention} wasn't on the allowed list anyway.")
        await _reply(ctx, msg, allowed_mentions=discord.AllowedMentions.none())
