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
)
from renormalize import fetch_all_renormalize_users, fetch_week_hours
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
        "**2. Если нужного человека нет — добавь его**\n"
        "```\n!addmember Имя Фамилия\n```\n"
        "*(или `/addmember` — Discord сам подскажет имя из Renormalize)*\n\n"
        "**3. Подпишись на нужных людей и выбери время отчёта**\n"
        "```\n!subscribe\n!settime 09:00\n```\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "**📋 Все команды:**\n\n"
        "`!members` — список всех доступных сотрудников\n"
        "`!addmember <имя>` — добавить человека (есть автодополнение в `/addmember`)\n"
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


async def _renormalize_user_autocomplete(
    interaction: discord.Interaction, current: str,
) -> list[app_commands.Choice[str]]:
    """Live-search Renormalize accounts (active, not already tracked) by name or email."""
    try:
        users = await fetch_all_renormalize_users()
    except Exception as exc:
        log.warning("renormalize user autocomplete failed: %s", exc)
        return []
    existing_ids = {rid for rid in _all_renormalize_ids().values() if rid}
    cur = current.casefold().strip()
    matches = [
        u for u in users
        if u.get("status") == "active" and u.get("id") not in existing_ids
        and (not cur or cur in u.get("name", "").casefold() or cur in u.get("email", "").casefold())
    ]
    return [
        app_commands.Choice(name=f"{u['name']} ({u.get('email', '')})"[:100], value=str(u["id"]))
        for u in matches[:25]
    ]


@bot.hybrid_command(name="addmember", description="Добавить сотрудника — начни печатать имя, подскажет Renormalize")
@app_commands.describe(person="Начни вводить имя — выбери из подсказок (или вставь Renormalize ID)")
@app_commands.rename(person="сотрудник")
@app_commands.autocomplete(person=_renormalize_user_autocomplete)
async def cmd_addmember(ctx: commands.Context, *, person: str) -> None:
    """!addmember <имя или Renormalize ID> — add a person found live in Renormalize."""
    person = person.strip()
    if not person:
        await ctx.send("❌ Укажи имя или Renormalize ID. Пример: `!addmember Ivan Petrov`.")
        return

    try:
        users = await fetch_all_renormalize_users()
    except Exception as exc:
        log.exception("addmember: fetch_all_renormalize_users failed: %s", exc)
        await ctx.send("❌ Не смог получить список из Renormalize. Попробуй ещё раз чуть позже.")
        return

    existing_ids = {rid for rid in _all_renormalize_ids().values() if rid}

    if person.isdigit():
        renorm_id = int(person)
        match     = next((u for u in users if u.get("id") == renorm_id), None)
        if match is None:
            await ctx.send(f"❌ В Renormalize нет пользователя с ID `{renorm_id}`.")
            return
    else:
        q          = person.casefold()
        candidates = [
            u for u in users
            if u.get("status") == "active" and u.get("id") not in existing_ids
            and q in u.get("name", "").casefold()
        ]
        if not candidates:
            await ctx.send(
                f"❌ Не нашёл «{person}» в Renormalize. Проверь написание — "
                f"или используй `/addmember`, там живые подсказки."
            )
            return
        if len(candidates) > 1:
            lines = "\n".join(f"• {u['name']} — id `{u['id']}`" for u in candidates[:10])
            await ctx.send(
                f"⚠️ Нашёл несколько совпадений для «{person}»:\n{lines}\n\n"
                f"Уточни имя или используй `/addmember` с подсказками."
            )
            return
        match     = candidates[0]
        renorm_id = match["id"]

    if renorm_id in existing_ids:
        await ctx.send(f"⚠️ **{match['name']}** уже в списке.")
        return

    add_custom_member(renorm_id, match["name"])
    await ctx.send(
        f"✅ Добавлен: **{match['name']}** (id `{renorm_id}`)\n"
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


@bot.hybrid_command(name="findmembers", description="Список всех сотрудников в Renormalize с их ID (только PM)")
async def cmd_find_members(ctx: commands.Context) -> None:
    """!findmembers — list all Renormalize workspace members with their IDs (PM only)."""
    if ctx.author.id != PM_USER_ID:
        await _deny(ctx)
        return
    if not RENORMALIZE_API_KEY:
        await _reply(ctx, "❌ RENORMALIZE_API_KEY не задан в .env")
        return

    await _working(ctx)

    try:
        members = await fetch_all_renormalize_users(force=True)
    except Exception as exc:
        log.exception("findmembers API error: %s", exc)
        user = await bot.fetch_user(PM_USER_ID)
        await user.send(f"❌ Ошибка запроса к Renormalize:\n```{exc}```")
        await _reply(ctx, "❌ Ошибка запроса к Renormalize, подробности — в личке.")
        return

    if not members:
        await _reply(ctx, "⚠️ Renormalize вернул пустой список.")
        return

    lines = ["👥 **Сотрудники в Renormalize (ID — Имя — статус):**\n"]
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
    user = await bot.fetch_user(PM_USER_ID)
    # Split if over Discord's 2000-char limit
    for chunk in [text[i:i+1900] for i in range(0, len(text), 1900)]:
        await user.send(chunk)
    await _reply(ctx, f"✅ Список отправлен тебе в личку ({len(members)} чел.).")


@bot.hybrid_command(name="alloweduser",
                    description="Кто может пользоваться ботом — добавить / убрать / показать список (только PM)")
@app_commands.describe(action="add — разрешить, remove — запретить, list — показать список",
                       user="Начни вводить ник — или вставь Discord ID (не нужен для list)")
@app_commands.rename(action="действие", user="discord")
@app_commands.choices(action=[
    app_commands.Choice(name="add — разрешить", value="add"),
    app_commands.Choice(name="remove — запретить", value="remove"),
    app_commands.Choice(name="list — показать список", value="list"),
])
@app_commands.autocomplete(user=_discord_user_autocomplete)
async def cmd_alloweduser(ctx: commands.Context, action: str, user: Optional[str] = None) -> None:
    """!alloweduser <add|remove|list> [discord] — manage who can use the bot (PM only)."""
    if ctx.author.id != PM_USER_ID:
        await _deny(ctx)
        return

    action = action.strip().lower()

    if action == "list":
        ids = get_allowed_users()
        header = "✅ **Доступ есть у тебя (PM) и у:**\n" if ids else "Доступ есть только у тебя (PM) — список пуст."
        lines  = "\n".join(f"• <@{uid}> (`{uid}`)" for uid in ids)
        await _reply(ctx, header + lines, allowed_mentions=discord.AllowedMentions.none())
        return

    if action not in ("add", "remove"):
        await _reply(ctx, "❌ Действие: `add`, `remove` или `list`.")
        return

    if not user:
        await _reply(ctx, "❌ Укажи, кого добавить/убрать — ник, упоминание или Discord ID.")
        return

    target = await _resolve_discord_user(user, ctx.message.mentions)
    if target is None:
        await _reply(ctx, f"❌ Не нашёл пользователя Discord «{user}». Выбери из подсказок или вставь ID.")
        return

    if action == "add":
        add_allowed_user(target.id)
        await _reply(ctx, f"✅ {target.mention} теперь может пользоваться ботом.",
                     allowed_mentions=discord.AllowedMentions.none())
    else:
        if target.id == PM_USER_ID:
            await _reply(ctx, "⚠️ Себя (PM) убрать нельзя — у тебя доступ всегда есть.")
            return
        removed = remove_allowed_user(target.id)
        msg = (f"✅ {target.mention} больше не может пользоваться ботом." if removed
               else f"⚠️ {target.mention} и так не было в списке разрешённых.")
        await _reply(ctx, msg, allowed_mentions=discord.AllowedMentions.none())
