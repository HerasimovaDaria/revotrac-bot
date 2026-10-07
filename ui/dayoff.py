"""Discord UI — day-off selector (PM only)."""

from datetime import date

import discord

from db import save_day_offs
from ui.common import _chunk_placeholder, _member_chunks


class DayOffSelect(discord.ui.Select):
    def __init__(self, target_date: date,
                 members: list[tuple[str, str, float, float]], placeholder: str) -> None:
        self.target_date = target_date
        super().__init__(
            placeholder=placeholder,
            min_values=0,
            max_values=len(members),
            options=[
                discord.SelectOption(label=f"{en_name} ({name})", value=name)
                for name, en_name, _, _ in members
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()


class DayOffView(discord.ui.View):
    def __init__(self, target_date: date) -> None:
        super().__init__(timeout=600)
        self.target_date = target_date
        chunks           = _member_chunks("day-off selector")
        self.selects: list[DayOffSelect] = []
        for i, chunk in enumerate(chunks):
            select = DayOffSelect(
                target_date, chunk, _chunk_placeholder("Выберите сотрудников…", i, chunk, len(chunks)))
            select.row = i
            self.selects.append(select)
            self.add_item(select)

    @discord.ui.button(label="Сохранить", style=discord.ButtonStyle.success, emoji="💾", row=4)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        chosen = [n for s in self.selects for n in s.values]
        save_day_offs(chosen, self.target_date)
        msg = (
            f"✅ Выходной на {self.target_date.strftime('%d.%m.%Y')} сохранён: {', '.join(chosen)}"
            if chosen else
            f"✅ Выходных на {self.target_date.strftime('%d.%m.%Y')} не сохранено."
        )
        self.stop()
        await interaction.response.edit_message(content=msg, view=None)

    @discord.ui.button(label="Сегодня все работают", style=discord.ButtonStyle.secondary, emoji="🚫", row=4)
    async def no_day_off(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        save_day_offs([], self.target_date)
        self.stop()
        await interaction.response.edit_message(
            content=f"👍 Выходных на {self.target_date.strftime('%d.%m.%Y')} нет.",
            view=None,
        )
