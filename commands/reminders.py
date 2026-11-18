"""Prefix and guild-scoped slash command handlers for reminders."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import discord
from discord import app_commands

from config import Config
from database.db import Database
from services.reminder_format import (
    format_local_due,
    format_remaining,
    local_day_bounds,
    tomorrow_snooze_due,
)
from services.reminder_parser import (
    ReminderParseError,
    parse_prefix_reminder,
    parse_time_expression,
    validate_description,
)
from services.class_service import resolve_class_label

logger = logging.getLogger(__name__)
_REMINDER_ID_RE = re.compile(r"^reminder\s+(\d{1,19})$", re.IGNORECASE)


async def handle_reminder_prefix(
    command_name: str,
    arguments: str,
    config: Config,
    database: Database,
    *,
    channel: Any | None = None,
) -> str | None:
    """Run a supported prefix reminder operation; None means not a reminder command."""
    args = arguments.strip()
    if command_name == "remind":
        if not args.lower().startswith("me "):
            return "Use `!ybot remind me <time> <description>`."
        try:
            request, explicit_class = _split_class_suffix(args[3:])
            parsed = parse_prefix_reminder(
                request, config.timezone_name
            )
            clean_class = (
                validate_description(explicit_class, maximum_length=100)
                if explicit_class is not None
                else None
            )
            class_name = await resolve_class_label(
                database, clean_class, channel
            )
        except ReminderParseError as exc:
            return str(exc)
        except ValueError as exc:
            return str(exc)
        reminder_id = await database.create_reminder(
            parsed.description, parsed.due_at, class_name
        )
        return (
            f"Reminder #{reminder_id} created: "
            f"**{discord.utils.escape_mentions(parsed.description)}**\n"
            f"Due: {format_local_due(parsed.due_at, config.timezone_name)}"
        )

    if command_name == "reminders":
        if args and args.lower() != "today":
            return "Use `!ybot reminders` or `!ybot reminders today`."
        return await _format_pending_reminders(database, config, today_only=bool(args))

    if command_name in {"cancel", "delete", "complete"}:
        match = _REMINDER_ID_RE.fullmatch(args)
        if match is None:
            return f"Use `!ybot {command_name} reminder <id>`."
        reminder_id = int(match.group(1))
        if command_name == "delete":
            changed = await database.delete_reminder(reminder_id)
            return (
                f"Reminder #{reminder_id} deleted."
                if changed
                else f"Reminder #{reminder_id} was not found."
            )
        if command_name == "cancel":
            changed = await database.cancel_reminder(reminder_id)
            return _status_result(reminder_id, changed, "cancelled")
        changed = await database.complete_reminder(reminder_id)
        return _status_result(reminder_id, changed, "completed")

    if command_name == "snooze":
        match = re.fullmatch(
            r"reminder\s+(\d{1,19})\s+(10m|30m|1h|tomorrow)", args, re.I
        )
        if match is None:
            return "Use `!ybot snooze reminder <id> <10m|30m|1h|tomorrow>`."
        reminder_id = int(match.group(1))
        option = match.group(2).lower()
        now = datetime.now(timezone.utc)
        due_at = (
            now + timedelta(minutes={"10m": 10, "30m": 30, "1h": 60}[option])
            if option != "tomorrow"
            else tomorrow_snooze_due(config.timezone_name, now=now)
        )
        changed = await database.snooze_reminder(reminder_id, due_at)
        return _status_result(reminder_id, changed, "snoozed")

    return None


def _split_class_suffix(value: str) -> tuple[str, str | None]:
    match = re.search(r"\s+\|\s*class\s*:\s*", value, flags=re.IGNORECASE)
    if match is None:
        return value, None
    request = value[: match.start()].strip()
    class_name = value[match.end() :].strip()
    if not class_name:
        raise ReminderParseError("Class name cannot be empty after `class:`.")
    return request, class_name


async def _format_pending_reminders(
    database: Database, config: Config, *, today_only: bool = False
) -> str:
    if today_only:
        start, end = local_day_bounds(config.timezone_name)
        reminders = await database.list_pending_reminders(due_from=start, due_before=end)
        title = "Pending reminders due today"
    else:
        reminders = await database.list_pending_reminders()
        title = "Pending reminders"
    if not reminders:
        return f"{title}: none."

    lines = [f"**{title}:**"]
    displayed = 0
    for reminder in reminders:
        description = discord.utils.escape_mentions(reminder["description"])
        if len(description) > 120:
            description = description[:117] + "…"
        line = (
            f"`#{reminder['id']}` **{description}**\n"
            f"Due {format_local_due(reminder['due_at'], config.timezone_name)} "
            f"({format_remaining(reminder['due_at'], config.timezone_name)})"
        )
        if reminder.get("class_name"):
            line += f" · {discord.utils.escape_mentions(reminder['class_name'])}"
        if sum(len(existing) for existing in lines) + len(line) + 2 > 1750:
            break
        lines.append(line)
        displayed += 1
    if len(reminders) > displayed:
        lines.append(
            f"…and {len(reminders) - displayed} more. Use reminder IDs to manage them."
        )
    return "\n".join(lines)


def _status_result(reminder_id: int, changed: bool, action: str) -> str:
    if changed:
        return f"Reminder #{reminder_id} {action}."
    return f"Reminder #{reminder_id} was not found or is no longer active."


def register_reminder_slash_commands(
    tree: app_commands.CommandTree,
    config: Config,
    database: Database,
) -> None:
    """Register the Phase 2 slash commands only in the configured guild."""
    target_guild = discord.Object(id=config.guild_id)

    async def authorized(interaction: discord.Interaction) -> bool:
        if (
            interaction.guild_id != config.guild_id
            or interaction.user.id != config.owner_id
        ):
            await interaction.response.send_message(
                "This reminder command is available only to the configured owner "
                "in the configured server.",
                ephemeral=True,
            )
            return False
        return True

    @app_commands.command(
        name="remind", description="Create a reminder using a natural-language time."
    )
    @app_commands.describe(
        time="For example: in 30 minutes, Friday at 5 PM, or November 20 at 8 PM",
        description="What should YBOT remind you about?",
        class_name="Optional class or course name",
    )
    @app_commands.rename(class_name="class")
    async def remind_command(
        interaction: discord.Interaction,
        time: str,
        description: str,
        class_name: str | None = None,
    ) -> None:
        if not await authorized(interaction):
            return
        try:
            due_at = parse_time_expression(time, config.timezone_name)
            clean_description = validate_description(description)
            clean_class = (
                validate_description(class_name, maximum_length=100)
                if class_name
                else None
            )
            clean_class = await resolve_class_label(
                database, clean_class, interaction.channel
            )
            reminder_id = await database.create_reminder(
                clean_description, due_at, clean_class
            )
            class_suffix = (
                f" · {discord.utils.escape_mentions(clean_class)}" if clean_class else ""
            )
            await interaction.response.send_message(
                f"Reminder `#{reminder_id}` created: "
                f"**{discord.utils.escape_mentions(clean_description)}**{class_suffix}\n"
                f"Due {format_local_due(due_at, config.timezone_name)}.",
                ephemeral=True,
            )
        except ReminderParseError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
        except Exception:
            logger.exception("Slash reminder creation failed")
            await interaction.response.send_message(
                "YBOT couldn't create that reminder. Please try again.", ephemeral=True
            )

    @app_commands.command(name="reminders", description="Show your pending reminders.")
    async def reminders_command(interaction: discord.Interaction) -> None:
        if not await authorized(interaction):
            return
        try:
            response = await _format_pending_reminders(database, config)
            await interaction.response.send_message(response, ephemeral=True)
        except Exception:
            logger.exception("Slash reminder listing failed")
            await interaction.response.send_message(
                "YBOT couldn't load reminders. Please try again.", ephemeral=True
            )

    @app_commands.command(
        name="reminder-cancel", description="Select and cancel a pending reminder."
    )
    @app_commands.describe(reminder_id="Choose a pending reminder")
    async def cancel_command(
        interaction: discord.Interaction, reminder_id: str
    ) -> None:
        if not await authorized(interaction):
            return
        try:
            changed = await database.cancel_reminder(int(reminder_id))
            await interaction.response.send_message(
                _status_result(int(reminder_id), changed, "cancelled"), ephemeral=True
            )
        except (ValueError, TypeError):
            await interaction.response.send_message(
                "Select a valid reminder.", ephemeral=True
            )
        except Exception:
            logger.exception("Slash reminder cancellation failed")
            await interaction.response.send_message(
                "YBOT couldn't cancel that reminder. Please try again.", ephemeral=True
            )

    @cancel_command.autocomplete("reminder_id")
    async def cancel_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if (
            interaction.guild_id != config.guild_id
            or interaction.user.id != config.owner_id
        ):
            return []
        try:
            reminders = await database.list_pending_reminders()
        except Exception:
            logger.exception("Reminder cancellation autocomplete failed")
            return []
        choices = []
        for reminder in reminders:
            label = (
                f"#{reminder['id']} — {reminder['description']} — "
                f"{format_local_due(reminder['due_at'], config.timezone_name)}"
            )[:100]
            if current.lower() in label.lower() or current in str(reminder["id"]):
                choices.append(app_commands.Choice(name=label, value=str(reminder["id"])))
            if len(choices) == 25:
                break
        return choices

    for command in (remind_command, reminders_command, cancel_command):
        tree.add_command(command, guild=target_guild)
