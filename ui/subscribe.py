"""Discord UI — subscription selector (!subscribe)."""

import discord

from db import _all_renormalize_ids, get_preference, get_subscription, save_subscription
from ui.common import _chunk_placeholder, _member_chunks


def _subscribe_prompt(current: list[str]) -> str:
    if current:
        return f"📋 **Your subscription:** {', '.join(current)}\n\nPick who you want to track:"
    return "📋 You don't have a subscription yet.\nPick the people whose hours you want to see:"


class SubscribeSelect(discord.ui.Select):
    def __init__(self, current: list[str],
                 members: list[tuple[str, str, float, float]], placeholder: str) -> None:
        all_ids = _all_renormalize_ids()
        options = [
            discord.SelectOption(
                label=(f"{en_name}  (id {all_ids[name]})" if all_ids.get(name)
                       else f"{en_name}  (reports only)"),
                value=name,
                default=name in current,
            )
            for name, en_name, _, _ in members
        ]
        super().__init__(
            placeholder=placeholder,
            min_values=0,
            max_values=len(options),
            options=options,
        )
        self.touched = False

    @property
    def chosen(self) -> list[str]:
        """Selected values; untouched select keeps its defaults (values would be empty)."""
        if self.touched:
            return list(self.values)
        return [o.value for o in self.options if o.default]

    async def callback(self, interaction: discord.Interaction) -> None:
        self.touched = True
        await interaction.response.defer()


class EditSubscriptionView(discord.ui.View):
    """Shown after saving — gives the user a chance to edit again."""

    def __init__(self, user_id: int) -> None:
        super().__init__(timeout=300)
        self.user_id = user_id

    @discord.ui.button(label="Edit subscription", style=discord.ButtonStyle.secondary, emoji="✏️")
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        current = get_subscription(self.user_id)
        view    = SubscribeView(self.user_id)
        self.stop()
        await interaction.response.edit_message(
            content=_subscribe_prompt(current),
            view=view,
        )


class SubscribeView(discord.ui.View):
    def __init__(self, user_id: int) -> None:
        super().__init__(timeout=300)
        self.user_id = user_id
        current      = get_subscription(user_id)
        chunks       = _member_chunks("!subscribe")
        self.selects: list[SubscribeSelect] = []
        for i, chunk in enumerate(chunks):
            placeholder = _chunk_placeholder(
                "Choose people to track…", i, chunk, len(chunks))
            select = SubscribeSelect(current, chunk, placeholder)
            select.row = i
            self.selects.append(select)
            self.add_item(select)

    @discord.ui.button(label="Save", style=discord.ButtonStyle.success, emoji="💾", row=4)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        chosen = [n for s in self.selects for n in s.chosen]
        save_subscription(self.user_id, chosen)
        self.stop()

        if chosen:
            bullet_list = "\n".join(f"  • {n}" for n in chosen)
            h, m = get_preference(self.user_id)
            msg = (
                f"✅ **Subscription saved!**\n\n"
                f"You'll get reports for:\n{bullet_list}\n\n"
                f"⏰ Report time: **{h:02d}:{m:02d} UTC+3**.\n"
                f"Change the time: `!settime HH:MM`  (e.g. `!settime 08:30`)"
            )
        else:
            msg = (
                "⚠️ Empty subscription — you won't get morning reports.\n"
                "Click \"Edit subscription\" to add people."
            )

        await interaction.response.edit_message(
            content=msg,
            view=EditSubscriptionView(self.user_id),
        )
