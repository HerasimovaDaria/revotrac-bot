"""Bot commands (hybrid !cmd / slash /cmd) and their autocomplete providers."""

from datetime import datetime, timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from client import bot
from config import MOSCOW, PM_USER_ID, REMINDER_HOUR, REMINDER_MINUTE, RENORMALIZE_API_KEY, TEAM, log
from db import (
    _all_members,
    _all_renormalize_ids,
    add_custom_member,
    add_report_only_member,
    get_custom_members,
    get_discord_links,
    get_preference,
    get_reminders,
    get_reports_channel,
    get_subscription,
    remove_custom_member,
    save_discord_link,
    save_preference,
    save_reminders,
    save_reports_channel,
)
from renormalize import fetch_week_hours
from reports.formatting import format_weekly_report
from routines import _build_report_text, _collect_report_data
from ui.dayoff import DayOffView
from ui.subscribe import SubscribeView, _subscribe_prompt
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
    """PM-only command called by someone else."""
    if ctx.interaction:
        await ctx.send("🚫 Только для PM.", ephemeral=True)
    else:
        await ctx.message.add_reaction("🚫")


@bot.hybrid_command(name="subscribe", description="Выбрать сотрудников для утреннего отчёта")
async def cmd_subscribe(ctx: commands.Context) -> None:
    """!subscribe — choose which team members appear in your daily reports."""
    user_id = ctx.author.id
    current = get_subscription(user_id)
    view    = SubscribeView(user_id)

    await _reply(ctx, _subscribe_prompt(current), view=view)


@bot.hybrid_command(name="report", description="Получить отчёт за прошлый рабочий день прямо сейчас")
async def cmd_report(ctx: commands.Context) -> None:
    """!report — trigger your personalized morning report right now."""
    user_id = ctx.author.id
    members = get_subscription(user_id)

    if not members:
        await _reply(
            ctx,
            "⚠️ У тебя нет подписки.\n"
            "Используй `/subscribe`, чтобы выбрать, чьи часы ты хочешь видеть в отчёте."
        )
        return

    await _working(ctx)

    today     = datetime.now(MOSCOW).date()
    yesterday = previous_workday(today)

    hours, day_offs, week_hours, authors = await _collect_report_data(bot, yesterday)

    text = await _build_report_text(bot, user_id, yesterday, hours, week_hours, day_offs, members, authors)
    await _reply(ctx, text)


@bot.hybrid_command(name="dayoff", description="Отметить, кто не работает в выбранный день (только PM)")
@app_commands.describe(date_str="Дата ДД.ММ, по умолчанию сегодня")
@app_commands.rename(date_str="дата")
async def cmd_dayoff(ctx: commands.Context, date_str: Optional[str] = None) -> None:
    """!dayoff [DD.MM] — open day-off selector for a given date (PM only)."""
    if ctx.author.id != PM_USER_ID:
        await _deny(ctx)
        return

    if date_str:
        try:
            today  = datetime.now(MOSCOW).date()
            parsed = datetime.strptime(date_str, "%d.%m").replace(year=today.year).date()
        except ValueError:
            await ctx.send("❌ Неверный формат. Пример: `!dayoff 25.05`")
            return
    else:
        parsed = datetime.now(MOSCOW).date()

    view = DayOffView(parsed)
    await _reply(ctx, f"📅 **Кто {parsed.strftime('%d.%m.%Y')} не работает?**", view=view)


@bot.hybrid_command(name="start", description="Что умеет бот и как его настроить")
async def cmd_start(ctx: commands.Context) -> None:
    """!start — onboarding: show what this bot does and how to set it up."""
    text = (
        "👋 **Привет! Я слежу за часами команды в Renormalize.**\n"
        "Каждое утро буду присылать тебе в личку отчёт по нужным людям.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "**🚀 Быстрый старт — 3 шага:**\n\n"
        "**1. Посмотри, кто уже есть в списке**\n"
        "```\n!members\n```\n"
        "**2. Если нужного человека нет — добавь его по Renormalize ID**\n"
        "```\n!addmember 12345 Имя Фамилия\n```\n"
        "*(ID найдёшь в URL профиля сотрудника в Renormalize: `?entity_id=XXXXX`)*\n\n"
        "**3. Подпишись на нужных людей и выбери время отчёта**\n"
        "```\n!subscribe\n!settime 09:00\n```\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "**📋 Все команды:**\n\n"
        "`!members` — список всех доступных сотрудников\n"
        "`!addmember <id> <имя>` — добавить человека по Renormalize ID\n"
        "`!subscribe` — выбрать, чьи часы видеть в отчёте\n"
        "`!linkdiscord <имя> <ID>` — привязать Discord для проверки daily-отчётов\n"
        "`!addperson <имя> <ID>` — добавить человека без Renormalize (только отчёты)\n"
        "`!setchannel` — написать в канале с отчётами, чтобы проверять именно его\n"
        "`!settime HH:MM` — время ежедневного отчёта (UTC+3, по умолчанию 09:00)\n"
        "`!reminders on` — вечером напоминать людям из подписки, если они не написали отчёт\n"
        "`!report` — получить отчёт прямо сейчас\n"
        "`!weekly` — прогресс за текущую неделю\n"
        "`!start` — показать эту инструкцию снова\n\n"
        "💡 Все команды можно вызывать через `/` — Discord покажет подсказки.\n"
    )
    await _reply(ctx, text)


@bot.hybrid_command(name="weekly", description="Прогресс по часам за текущую неделю")
async def cmd_weekly(ctx: commands.Context) -> None:
    """!weekly — show current-week progress for your subscribed members."""
    user_id = ctx.author.id
    members = get_subscription(user_id)

    if not members:
        await _reply(
            ctx,
            "⚠️ У тебя нет подписки.\n"
            "Используй `/subscribe`, чтобы выбрать, чьи часы ты хочешь видеть."
        )
        return

    await _working(ctx)

    today      = datetime.now(MOSCOW).date()
    wb         = week_start(today)
    week_hours = await fetch_week_hours(wb)
    text       = format_weekly_report(wb, week_hours, members) or "У твоих сотрудников нет недельной нормы часов."

    await _reply(ctx, text)


@bot.hybrid_command(name="members", description="Список сотрудников и привязок Discord")
async def cmd_members(ctx: commands.Context) -> None:
    """!members — list all people available for tracking."""
    lines = ["**👥 Все доступные сотрудники:**\n"]
    all_ids = _all_renormalize_ids()
    links   = get_discord_links()

    def link_str(name: str) -> str:
        uid = links.get(name)
        return f" · <@{uid}>" if uid else " · ⛓️‍💥 нет Discord"

    for name, en_name, daily, _ in TEAM:
        rid = all_ids.get(name, "?")
        lines.append(f"`{rid}` — {en_name}  ({daily:.0f}h/day){link_str(name)}")

    custom = get_custom_members()
    if custom:
        lines.append("\n**Добавлены вручную:**")
        for rid, dname, daily, _ in custom:
            if rid < 0:
                lines.append(f"{dname}  (только отчёты){link_str(dname)}")
            else:
                lines.append(f"`{rid}` — {dname}  ({daily:.0f}h/day){link_str(dname)}")

    lines.append("\n➕ Добавить: `!addmember <renormalize_id> <имя>`")
    lines.append("📝 Без часов, только отчёты: `!addperson <имя> <ID>`")
    lines.append("🔗 Привязать Discord: `!linkdiscord <имя> @user`")
    # Show mentions without pinging people
    await ctx.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())


@bot.command(name="linkdiscord")
async def cmd_linkdiscord(ctx: commands.Context, *, args: str = "") -> None:
    """!linkdiscord <name> <@user | nick | id> — link a member to their Discord account."""
    parts = args.split()
    if len(parts) < 2:
        await ctx.send(
            "❌ Формат: `!linkdiscord <имя> <@user | ник | Discord ID>`\n"
            "Например: `!linkdiscord Самвел @samvel` или `!linkdiscord Aleksey Siedin 954344819092783184`"
        )
        return

    name, spec = " ".join(parts[:-1]), parts[-1]
    member = _find_member(name)
    if member is None:
        await ctx.send(f"❌ Сотрудник «{name}» не найден (или подходит несколько). Список: `!members`")
        return

    user = await _resolve_discord_user(spec, ctx.message.mentions)
    if user is None:
        await ctx.send(
            f"❌ Не нашёл пользователя Discord «{spec}».\n"
            "Укажи ник (как в профиле, без пробелов) или Discord ID "
            "(ПКМ по человеку → «Копировать ID», нужен режим разработчика)."
        )
        return

    save_discord_link(member, user.id)
    await ctx.send(
        f"✅ **{member}** привязан к {user.mention}.",
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.hybrid_command(name="addmember", description="Добавить сотрудника по Renormalize ID")
@app_commands.describe(renorm_id="ID сотрудника в Renormalize", name="Имя для отчётов")
@app_commands.rename(renorm_id="renormalize_id", name="имя")
async def cmd_addmember(ctx: commands.Context, renorm_id: int, *, name: str) -> None:
    """!addmember <renormalize_id> <name> — add a person by their Renormalize ID."""
    name = name.strip()
    if not name:
        await ctx.send("❌ Укажи имя. Пример: `!addmember 12345 Ivan Petrov`")
        return

    add_custom_member(renorm_id, name)
    await ctx.send(
        f"✅ Добавлен: **{name}** (id `{renorm_id}`)\n"
        f"Теперь его можно выбрать через `!subscribe`."
    )


@bot.hybrid_command(name="removemember", description="Удалить добавленного вручную сотрудника (только PM)")
@app_commands.describe(arg="Renormalize ID или имя")
@app_commands.rename(arg="кого")
async def cmd_removemember(ctx: commands.Context, *, arg: str) -> None:
    """!removemember <renormalize_id | name> — remove a custom member (PM only)."""
    if ctx.author.id != PM_USER_ID:
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
        await ctx.send(f"✅ «{arg}» удалён из списка.")
    else:
        await ctx.send(f"⚠️ «{arg}» не найден среди добавленных вручную.")


@bot.command(name="addperson")
async def cmd_addperson(ctx: commands.Context, *, args: str = "") -> None:
    """!addperson <name> <@user | nick | id> — add a person without Renormalize (reports only)."""
    parts = args.split()
    if len(parts) < 2:
        await ctx.send(
            "❌ Формат: `!addperson <имя> <@user | ник | Discord ID>`\n"
            "Например: `!addperson Daria Herasimova 954344819092783184`"
        )
        return

    name, spec = " ".join(parts[:-1]), parts[-1]
    if any(name.casefold() in (n.casefold(), en.casefold()) for n, en, _, _ in _all_members()):
        await ctx.send(f"⚠️ «{name}» уже есть в списке. Привязать Discord: `!linkdiscord {name} <ID>`")
        return

    user = await _resolve_discord_user(spec, ctx.message.mentions)
    if user is None:
        await ctx.send(f"❌ Не нашёл пользователя Discord «{spec}». Лучше укажи Discord ID.")
        return

    add_report_only_member(name, user.id)
    await ctx.send(
        f"✅ Добавлен(а) **{name}** ({user.mention}) — без часов, проверяется только daily-отчёт.\n"
        f"Теперь можно выбрать в `!subscribe`.",
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.hybrid_command(name="setchannel", description="Сделать этот канал (или канал по ID) каналом с daily-отчётами")
@app_commands.describe(channel_id="ID канала; пусто — текущий канал")
@app_commands.rename(channel_id="id_канала")
async def cmd_setchannel(ctx: commands.Context, channel_id: Optional[str] = None) -> None:
    """!setchannel [channel_id] — set YOUR daily-reports channel (run it in that channel or pass an ID)."""
    if channel_id is not None and not channel_id.strip().isdigit():
        await _reply(ctx, "❌ ID канала — это число. ПКМ по каналу → «Копировать ID канала».")
        return

    if channel_id is None and ctx.guild is None:
        current = get_reports_channel(ctx.author.id)
        await _reply(ctx,
            (f"📝 Твой канал с отчётами: <#{current}> (`{current}`).\n" if current
             else "📝 Канал с отчётами не задан — отчёты не проверяются.\n")
            + "Изменить: напиши `!setchannel` прямо в нужном канале (или в посте форума), "
              "либо `!setchannel <ID канала>`."
        )
        return

    try:
        if channel_id is None:
            channel = ctx.channel
        else:
            cid     = int(channel_id)
            channel = bot.get_channel(cid) or await bot.fetch_channel(cid)
    except discord.HTTPException:
        await _reply(ctx, f"❌ Канал `{channel_id}` не найден или у бота нет к нему доступа.")
        return

    # A command typed inside a thread / forum post → use the parent channel
    if isinstance(channel, discord.Thread) and channel.parent is not None:
        channel = channel.parent

    guild = getattr(channel, "guild", None)
    if guild is None:
        await _reply(ctx, "❌ Это не канал сервера. Напиши `!setchannel` в канале с отчётами.")
        return
    if not channel.permissions_for(guild.me).read_message_history:
        await _reply(
            ctx,
            f"⚠️ У бота нет права «Читать историю сообщений» в <#{channel.id}> — "
            f"выдай его в настройках канала и повтори."
        )
        return

    save_reports_channel(ctx.author.id, channel.id)
    await _reply(
        ctx,
        f"✅ Готово: в твоих отчётах проверяется канал **#{channel.name}** "
        f"на сервере **{guild.name}**."
    )


@bot.hybrid_command(name="reminders", description="Вечерние напоминания тем, кто не написал отчёт (вкл/выкл)")
@app_commands.describe(mode="on — включить, off — выключить; пусто — показать статус")
@app_commands.rename(mode="режим")
@app_commands.choices(mode=[app_commands.Choice(name="включить", value="on"),
                            app_commands.Choice(name="выключить", value="off")])
async def cmd_reminders(ctx: commands.Context, mode: Optional[str] = None) -> None:
    """!reminders [on|off] — evening DM to people in your subscription who haven't posted a report."""
    when = f"{REMINDER_HOUR:02d}:{REMINDER_MINUTE:02d} UTC+3"
    if mode is None:
        state = "включены" if get_reminders(ctx.author.id) else "выключены"
        await ctx.send(
            f"🔔 Напоминания {state}. Если включены, в {when} по будням бот пишет в личку "
            f"каждому из твоей подписки, кто ещё не написал отчёт в твоём канале.\n"
            f"Включить: `/reminders on`, выключить: `/reminders off`",
            ephemeral=True,
        )
        return

    mode = mode.strip().lower()
    if mode not in ("on", "off", "вкл", "выкл"):
        await ctx.send("❌ Используй `on` или `off`.", ephemeral=True)
        return
    enabled = mode in ("on", "вкл")
    save_reminders(ctx.author.id, enabled)
    if enabled:
        channel_id = get_reports_channel(ctx.author.id)
        note = "" if channel_id else "\n⚠️ Канал с отчётами не задан — напиши `/setchannel` в нём."
        await ctx.send(
            f"🔔 Включено: в {when} по будням людям из твоей подписки без отчёта придёт напоминание.{note}",
            ephemeral=True,
        )
    else:
        await ctx.send("🔕 Напоминания выключены.", ephemeral=True)


@bot.hybrid_command(name="settime", description="Время утреннего отчёта (UTC+3), например 09:00")
@app_commands.describe(time_str="ЧЧ:ММ; пусто — показать текущее время")
@app_commands.rename(time_str="время")
async def cmd_settime(ctx: commands.Context, time_str: Optional[str] = None) -> None:
    """!settime [HH:MM] — set your daily report time (UTC+3). No arg = show current."""
    if time_str is None:
        h, m = get_preference(ctx.author.id)
        await ctx.send(
            f"🕐 Твоё текущее время отчёта: **{h:02d}:{m:02d} UTC+3**.\n"
            f"Изменить: `!settime 08:30`"
        )
        return

    try:
        parts  = time_str.strip().split(":")
        hour   = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("out of range")
    except (ValueError, IndexError):
        await ctx.send("❌ Неверный формат. Пример: `!settime 09:00` или `!settime 8:30`")
        return

    save_preference(ctx.author.id, hour, minute)
    await ctx.send(
        f"✅ Время ежедневного отчёта установлено: **{hour:02d}:{minute:02d} UTC+3**.\n"
        f"Если ещё не выбрал сотрудников — используй `!subscribe`."
    )


async def _member_autocomplete(
    interaction: discord.Interaction, current: str,
) -> list[app_commands.Choice[str]]:
    cur = current.casefold()
    return [
        app_commands.Choice(name=f"{en} ({n})"[:100] if en != n else n[:100], value=n)
        for n, en, _, _ in _all_members()
        if cur in n.casefold() or cur in en.casefold()
    ][:25]


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


@bot.tree.command(name="linkdiscord", description="Привязать сотрудника к его аккаунту Discord")
@app_commands.describe(member="Сотрудник", user="Начни вводить ник — или вставь Discord ID")
@app_commands.rename(member="сотрудник", user="discord")
@app_commands.autocomplete(member=_member_autocomplete, user=_discord_user_autocomplete)
async def slash_linkdiscord(interaction: discord.Interaction, member: str, user: str) -> None:
    ephemeral = interaction.guild is not None
    name      = _find_member(member)
    if name is None:
        await interaction.response.send_message(
            f"❌ Сотрудник «{member}» не найден. Выбери из подсказок.", ephemeral=True)
        return
    target = await _resolve_discord_user(user)
    if target is None:
        await interaction.response.send_message(
            f"❌ Не нашёл пользователя Discord «{user}». Выбери из подсказок или вставь ID.",
            ephemeral=True)
        return
    save_discord_link(name, target.id)
    await interaction.response.send_message(
        f"✅ **{name}** привязан к {target.mention}.",
        ephemeral=ephemeral, allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(name="addperson", description="Добавить человека без Renormalize (только проверка отчётов)")
@app_commands.describe(name="Имя для отчётов", user="Начни вводить ник — или вставь Discord ID")
@app_commands.rename(name="имя", user="discord")
@app_commands.autocomplete(user=_discord_user_autocomplete)
async def slash_addperson(interaction: discord.Interaction, name: str, user: str) -> None:
    ephemeral = interaction.guild is not None
    name      = name.strip()
    if any(name.casefold() in (n.casefold(), en.casefold()) for n, en, _, _ in _all_members()):
        await interaction.response.send_message(
            f"⚠️ «{name}» уже есть в списке. Привязать Discord: `/linkdiscord`", ephemeral=True)
        return
    target = await _resolve_discord_user(user)
    if target is None:
        await interaction.response.send_message(
            f"❌ Не нашёл пользователя Discord «{user}». Выбери из подсказок или вставь ID.",
            ephemeral=True)
        return
    add_report_only_member(name, target.id)
    await interaction.response.send_message(
        f"✅ Добавлен(а) **{name}** ({target.mention}) — без часов, проверяется только daily-отчёт.\n"
        f"Теперь можно выбрать в `/subscribe`.",
        ephemeral=ephemeral, allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.command(name="testapi")
async def cmd_test_api(ctx: commands.Context) -> None:
    """!testapi — test Renormalize API with Samvel's ID, show raw response (PM only)."""
    if ctx.author.id != PM_USER_ID:
        await ctx.message.add_reaction("🚫")
        return

    await ctx.message.add_reaction("⏳")
    import httpx

    today    = datetime.now(MOSCOW).date()
    test_id  = 76632  # Samvel
    date_str = (today - timedelta(days=1)).isoformat()

    results: list[str] = []
    async with httpx.AsyncClient() as client:
        headers = {"Authorization": f"Bearer {RENORMALIZE_API_KEY}"}
        base    = "https://api.renormalize.com/v1/time/progression"

        # Test Alexey Dumailenko (76542) — UI shows 9h38m on Jun 2, bot shows 6.8h
        alexey_id = 76542

        # Approach A: wide range Jun 1→Jun 3, filter by time_start date
        try:
            resp = await client.get(
                base,
                params={"user_ids": str(alexey_id), "start_at": "2026-06-01", "end_at": "2026-06-03"},
                headers=headers, timeout=10,
            )
            all_entries = resp.json().get(str(alexey_id), [])
            by_date: dict[str, int] = {}
            by_ts_date: dict[str, int] = {}
            for e in all_entries:
                d = e.get("date", "?")
                ts_d = str(e.get("time_start", ""))[:10]
                by_date[d] = by_date.get(d, 0) + e.get("total_time", 0)
                by_ts_date[ts_d] = by_ts_date.get(ts_d, 0) + e.get("total_time", 0)
            results.append(
                f"**Alexey (76542) Jun1→Jun3**\n"
                f"Entries: {len(all_entries)}\n"
                f"By `date` field: {by_date}\n"
                f"By `time_start` date: {by_ts_date}\n"
                f"→ Jun2 by date: {by_date.get('2026-06-02',0)/3600:.2f}h\n"
                f"→ Jun2 by time_start: {by_ts_date.get('2026-06-02',0)/3600:.2f}h"
            )
        except Exception as exc:
            results.append(f"**Alexey test** Error: {exc}")

    user = await bot.fetch_user(PM_USER_ID)
    await user.send("🔬 **API test (Samvel, yesterday):**\n\n" + "\n\n".join(results))
    await ctx.message.add_reaction("✅")


@bot.command(name="findmembers")
async def cmd_find_members(ctx: commands.Context) -> None:
    """!findmembers — list all Renormalize workspace members with their IDs (PM only)."""
    if ctx.author.id != PM_USER_ID:
        await ctx.message.add_reaction("🚫")
        return
    if not RENORMALIZE_API_KEY:
        await ctx.send("❌ RENORMALIZE_API_KEY не задан в .env")
        return

    await ctx.message.add_reaction("⏳")

    import httpx

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://api.renormalize.com/members",
                params={
                    "roles":  "engineer,manager,sales,qa,field_worker",
                    "status": "active,pending",
                    "page":   1,
                    "count":  200,
                },
                headers={"Authorization": f"Bearer {RENORMALIZE_API_KEY}"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        log.exception("findmembers API error: %s", exc)
        user = await bot.fetch_user(PM_USER_ID)
        await user.send(f"❌ Ошибка запроса к Renormalize:\n```{exc}```")
        await ctx.message.add_reaction("🔴")
        return

    # Handle various response shapes
    members = (
        data if isinstance(data, list)
        else data.get("data", data.get("members", data.get("users", [])))
    )

    if not members:
        user = await bot.fetch_user(PM_USER_ID)
        await user.send(
            "⚠️ Пустой список или неизвестная структура ответа.\n"
            f"Сырой ответ (первые 500 символов):\n```{str(data)[:500]}```"
        )
        await ctx.message.add_reaction("⚠️")
        return

    lines = ["👥 **Сотрудники в Renormalize (ID — Имя):**\n"]
    for m in members:
        uid  = m.get("id") or m.get("user_id") or "?"
        name = (
            m.get("full_name") or m.get("name")
            or f"{m.get('first_name','')} {m.get('last_name','')}".strip()
            or m.get("username") or "?"
        )
        role = m.get("role") or (m.get("roles") or [""])[0]
        lines.append(f"`{uid}` — **{name}** ({role})")

    text = "\n".join(lines)
    user = await bot.fetch_user(PM_USER_ID)
    # Split if over Discord's 2000-char limit
    for chunk in [text[i:i+1900] for i in range(0, len(text), 1900)]:
        await user.send(chunk)

    await ctx.message.add_reaction("✅")
