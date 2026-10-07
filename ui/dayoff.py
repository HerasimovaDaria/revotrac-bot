"""Discord UI — day-off selector (PM only).

The menu must keep working hours later and after bot restarts, so its items are
DynamicItems: the date (and select chunk) live in custom_id, the choice is saved to the DB
as soon as it's made, and the handlers are registered on startup (bot.add_dynamic_items).
"""

from datetime import date

import discord

from db import get_day_offs, save_day_offs
from ui.common import _chunk_placeholder, _member_chunks


def _dayoff_summary(day: date) -> str:
    names = sorted(get_day_offs(day))
    return (f"✅ Выходной {day.strftime('%d.%m.%Y')}: {', '.join(names)}" if names
            else f"👍 Выходных на {day.strftime('%d.%m.%Y')} нет.")


class DayOffSelect(discord.ui.DynamicItem[discord.ui.Select],
                   template=r"dayoff:sel:(?P<day>\d{4}-\d{2}-\d{2}):(?P<chunk>\d+)"):
    def __init__(self, target_date: date, chunk: int,
                 members: list[tuple[str, str, float, float]], placeholder: str) -> None:
        self.target_date = target_date
        current          = get_day_offs(target_date)
        super().__init__(
            discord.ui.Select(
                custom_id=f"dayoff:sel:{target_date.isoformat()}:{chunk}",
                placeholder=placeholder,
                min_values=0,
                max_values=len(members),
                options=[
                    discord.SelectOption(label=f"{en_name} ({name})"[:100], value=name,
                                         default=name in current)
                    for name, en_name, _, _ in members
                ],
            ),
            row=chunk,
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction,
                             item: discord.ui.Select, match) -> "DayOffSelect":
        # Rebuild from the clicked message's own options — the member list may have changed since
        members = [(o.value, o.value, 0.0, 0.0) for o in item.options]
        return cls(date.fromisoformat(match["day"]), int(match["chunk"]), members, item.placeholder or "")

    async def callback(self, interaction: discord.Interaction) -> None:
        chosen   = set((interaction.data or {}).get("values", []))
        in_chunk = {o.value for o in self.item.options}
        keep     = get_day_offs(self.target_date) - in_chunk
        save_day_offs(sorted(keep | chosen), self.target_date)
        await interaction.response.defer()


class DayOffSaveButton(discord.ui.DynamicItem[discord.ui.Button],
                       template=r"dayoff:save:(?P<day>\d{4}-\d{2}-\d{2})"):
    def __init__(self, target_date: date) -> None:
        self.target_date = target_date
        super().__init__(
            discord.ui.Button(label="Сохранить", style=discord.ButtonStyle.success, emoji="💾",
                              custom_id=f"dayoff:save:{target_date.isoformat()}"),
            row=4,
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match) -> "DayOffSaveButton":
        return cls(date.fromisoformat(match["day"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content=_dayoff_summary(self.target_date), view=None)


class DayOffNoneButton(discord.ui.DynamicItem[discord.ui.Button],
                       template=r"dayoff:none:(?P<day>\d{4}-\d{2}-\d{2})"):
    def __init__(self, target_date: date) -> None:
        self.target_date = target_date
        super().__init__(
            discord.ui.Button(label="Сегодня все работают", style=discord.ButtonStyle.secondary,
                              emoji="🚫", custom_id=f"dayoff:none:{target_date.isoformat()}"),
            row=4,
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match) -> "DayOffNoneButton":
        return cls(date.fromisoformat(match["day"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        save_day_offs([], self.target_date)
        await interaction.response.edit_message(content=_dayoff_summary(self.target_date), view=None)


class DayOffView(discord.ui.View):
    def __init__(self, target_date: date) -> None:
        super().__init__(timeout=None)
        chunks = _member_chunks("day-off selector")
        for i, chunk in enumerate(chunks):
            self.add_item(DayOffSelect(
                target_date, i, chunk, _chunk_placeholder("Выберите сотрудников…", i, chunk, len(chunks))))
        self.add_item(DayOffSaveButton(target_date))
        self.add_item(DayOffNoneButton(target_date))
