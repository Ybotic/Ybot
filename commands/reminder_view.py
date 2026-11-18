"""Persistent owner-only controls attached to reminder notifications."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import discord

from database.db import Database
from services.reminder_format import tomorrow_snooze_due

logger = logging.getLogger(__name__)


class ReminderSnoozeSelect(discord.ui.Select):
    def __init__(self, parent: ReminderActionView) -> None:
        super().__init__(
            custom_id="ybot:reminder:snooze",
            placeholder="Snooze…",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="10 minutes", value="10"),
                discord.SelectOption(label="30 minutes", value="30"),
                discord.SelectOption(label="1 hour", value="60"),
                discord.SelectOption(label="Tomorrow", value="tomorrow"),
            ],
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.parent_view.snooze(interaction, self.values[0])


class ReminderActionView(discord.ui.View):
    """Message-scoped persistent view whose actions can be restored on startup."""

    def __init__(
        self,
        database: Database,
        owner_id: int,
        guild_id: int,
        timezone_name: str,
        reminder_id: int,
    ) -> None:
        super().__init__(timeout=None)
        self.database = database
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.timezone_name = timezone_name
        self.reminder_id = reminder_id
        self.add_item(ReminderSnoozeSelect(self))

    @discord.ui.button(
        label="Complete",
        style=discord.ButtonStyle.success,
        custom_id="ybot:reminder:complete",
    )
    async def complete_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        del button
        if not await self._authorize_and_check(interaction):
            return
        if not await self._change_status(interaction, "complete"):
            return
        await self._finish_action(interaction, "Reminder marked complete.")

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.danger,
        custom_id="ybot:reminder:cancel",
    )
    async def cancel_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        del button
        if not await self._authorize_and_check(interaction):
            return
        if not await self._change_status(interaction, "cancel"):
            return
        await self._finish_action(interaction, "Reminder cancelled.")

    async def snooze(self, interaction: discord.Interaction, value: str) -> None:
        if not await self._authorize_and_check(interaction):
            return
        now = datetime.now(timezone.utc)
        if value == "tomorrow":
            due_at = tomorrow_snooze_due(self.timezone_name, now=now)
        elif value in {"10", "30", "60"}:
            due_at = now + timedelta(minutes=int(value))
        else:
            await interaction.response.send_message(
                "That snooze option is invalid.", ephemeral=True
            )
            return
        try:
            changed = await self.database.snooze_reminder(self.reminder_id, due_at)
        except Exception:
            logger.exception("Could not snooze reminder ID %s", self.reminder_id)
            await interaction.response.send_message(
                "YBOT couldn't snooze this reminder. Please try again later.",
                ephemeral=True,
            )
            return
        if not changed:
            await interaction.response.send_message(
                "This reminder is no longer active.", ephemeral=True
            )
            return
        await self._finish_action(interaction, "Reminder snoozed.")

    async def _change_status(
        self, interaction: discord.Interaction, action: str
    ) -> bool:
        try:
            if action == "complete":
                changed = await self.database.complete_reminder(self.reminder_id)
            else:
                changed = await self.database.cancel_reminder(self.reminder_id)
        except Exception:
            logger.exception("Could not update reminder ID %s", self.reminder_id)
            await interaction.response.send_message(
                "YBOT couldn't update this reminder. Please try again later.",
                ephemeral=True,
            )
            return False
        if not changed:
            await interaction.response.send_message(
                "This reminder is no longer active.", ephemeral=True
            )
            return False
        return True

    async def _authorize_and_check(self, interaction: discord.Interaction) -> bool:
        if (
            interaction.user.id != self.owner_id
            or interaction.guild_id != self.guild_id
        ):
            await interaction.response.send_message(
                "Only the configured owner can use reminder controls here.",
                ephemeral=True,
            )
            return False

        try:
            reminder = await self.database.get_reminder(self.reminder_id)
        except Exception:
            logger.exception("Could not verify reminder control access")
            await interaction.response.send_message(
                "YBOT couldn't verify this reminder. Please try again later.",
                ephemeral=True,
            )
            return False
        message_id = getattr(interaction.message, "id", None)
        if (
            reminder is None
            or reminder["status"] != "notified"
            or reminder["discord_message_id"] != message_id
        ):
            await interaction.response.send_message(
                "This reminder is no longer active.", ephemeral=True
            )
            return False
        return True

    async def _finish_action(
        self, interaction: discord.Interaction, response_text: str
    ) -> None:
        await interaction.response.send_message(response_text, ephemeral=True)
        try:
            await interaction.message.edit(view=None)
        except discord.HTTPException:
            logger.exception("Could not remove completed reminder controls")
