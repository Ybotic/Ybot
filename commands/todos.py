"""Prefix and guild-scoped slash commands for the to-do manager."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import discord
from discord import app_commands

from commands.todo_panel import TodoEditLauncherView, TodoPanelManager
from config import Config
from services.reminder_format import format_local_due, format_remaining
from services.todo_service import TodoService, TodoValidationError

logger = logging.getLogger(__name__)
_TASK_ID_RE = re.compile(r"^[1-9]\d{0,18}$")


@dataclass(slots=True)
class TodoPrefixResponse:
    content: str
    view: discord.ui.View | None = None


async def handle_todo_prefix(
    arguments: str,
    message: Any,
    config: Config,
    service: TodoService,
    panel: TodoPanelManager,
) -> TodoPrefixResponse:
    """Handle `!ybot todo ...` after the shared owner/guild checks."""
    raw = arguments.strip()
    if not raw:
        counts = await service.counts()
        link = await panel.panel_link()
        panel_link = f" Open the [task panel]({link})." if link else " The task panel is starting."
        return TodoPrefixResponse(
            f"To-do panel · {counts['open']} outstanding · "
            f"{counts['completed']} completed.{panel_link}"
        )

    first, _, remainder = raw.partition(" ")
    command = first.casefold()
    if command == "list":
        return TodoPrefixResponse(await _format_task_list(service, config, "open"))
    if command == "completed":
        return TodoPrefixResponse(await _format_task_list(service, config, "completed"))
    if command == "add":
        return await _add_from_prefix(remainder, message, service, panel)
    if command == "done":
        return await _complete_from_prefix(remainder, service, panel)
    if command == "remove":
        return await _archive_from_prefix(remainder, service, panel)
    if command == "edit":
        return await _edit_from_prefix(remainder, config, service, panel)
    if command == "clear":
        return await _clear_from_prefix(remainder, service, panel)

    # `!ybot todo buy groceries` is the quick-add form.
    return await _add_from_prefix(raw, message, service, panel)


async def _add_from_prefix(
    arguments: str,
    message: Any,
    service: TodoService,
    panel: TodoPanelManager,
) -> TodoPrefixResponse:
    title, fields, error = _parse_add_fields(arguments)
    if error:
        return TodoPrefixResponse(error)
    if not title:
        return TodoPrefixResponse(
            "Provide a task title. Example: `!ybot todo add buy groceries`."
        )
    try:
        todo_id = await service.add_task(
            title,
            description=fields.get("description"),
            class_name=fields.get("class"),
            due_text=fields.get("due"),
            priority=fields.get("priority", "normal"),
            channel=message.channel,
        )
    except TodoValidationError as exc:
        return TodoPrefixResponse(str(exc))
    refreshed = await panel.refresh()
    response = f"Task #{todo_id} added: **{discord.utils.escape_mentions(title)}**."
    if not refreshed:
        response += " The task is saved; use Refresh Panel to retry the panel update."
    return TodoPrefixResponse(response)


def _parse_add_fields(arguments: str) -> tuple[str, dict[str, str], str | None]:
    parts = [part.strip() for part in arguments.split("|")]
    title = parts[0] if parts else ""
    fields: dict[str, str] = {}
    allowed = {"description", "class", "due", "priority"}
    for part in parts[1:]:
        key, separator, value = part.partition(":")
        normalized_key = key.strip().casefold()
        if not separator or normalized_key not in allowed:
            return title, fields, (
                "Optional task fields use `| description: ... | class: ... | "
                "due: Friday at 5 PM | priority: high`."
            )
        fields[normalized_key] = value.strip()
    return title, fields, None


async def _complete_from_prefix(
    raw_id: str, service: TodoService, panel: TodoPanelManager
) -> TodoPrefixResponse:
    todo_id = _parse_task_id(raw_id)
    if todo_id is None:
        return TodoPrefixResponse("Use `!ybot todo done <id>`.")
    try:
        changed = await service.complete_task(todo_id)
    except Exception:
        logger.exception("Could not complete task ID %s", todo_id)
        return TodoPrefixResponse("YBOT couldn't complete that task. Please try again.")
    if not changed:
        return TodoPrefixResponse(f"Open task #{todo_id} was not found.")
    refreshed = await panel.refresh()
    response = f"Task #{todo_id} completed."
    if not refreshed:
        response += " The task is saved; use Refresh Panel to retry the panel update."
    return TodoPrefixResponse(response)


async def _archive_from_prefix(
    raw_id: str, service: TodoService, panel: TodoPanelManager
) -> TodoPrefixResponse:
    todo_id = _parse_task_id(raw_id)
    if todo_id is None:
        return TodoPrefixResponse("Use `!ybot todo remove <id>`.")
    try:
        changed = await service.archive_task(todo_id)
    except Exception:
        logger.exception("Could not archive task ID %s", todo_id)
        return TodoPrefixResponse("YBOT couldn't archive that task. Please try again.")
    if not changed:
        return TodoPrefixResponse(f"Task #{todo_id} was not found or is already archived.")
    refreshed = await panel.refresh()
    response = f"Task #{todo_id} archived (it remains in the database)."
    if not refreshed:
        response += " Use Refresh Panel to retry the panel update."
    return TodoPrefixResponse(response)


async def _edit_from_prefix(
    raw_id: str,
    config: Config,
    service: TodoService,
    panel: TodoPanelManager,
) -> TodoPrefixResponse:
    todo_id = _parse_task_id(raw_id)
    if todo_id is None:
        return TodoPrefixResponse("Use `!ybot todo edit <id>`.")
    task = await service.get_task(todo_id)
    if task is None or task["status"] == "archived":
        return TodoPrefixResponse(f"Task #{todo_id} was not found or is archived.")
    return TodoPrefixResponse(
        f"Edit task #{todo_id} using the button below:",
        TodoEditLauncherView(panel, todo_id, config.owner_id, config.guild_id),
    )


async def _clear_from_prefix(
    confirmation: str, service: TodoService, panel: TodoPanelManager
) -> TodoPrefixResponse:
    if confirmation.strip().casefold() != "confirm":
        open_count = await service.count_tasks("open")
        return TodoPrefixResponse(
            f"This will archive all {open_count} open tasks. "
            "Repeat `!ybot todo clear confirm` to continue. Tasks will be preserved."
        )
    try:
        archived = await service.archive_all_open()
    except Exception:
        logger.exception("Could not archive all open tasks")
        return TodoPrefixResponse("YBOT couldn't clear open tasks. Please try again.")
    refreshed = await panel.refresh()
    response = f"Archived {archived} open task(s). They remain in the database."
    if not refreshed:
        response += " Use Refresh Panel to retry the panel update."
    return TodoPrefixResponse(response)


async def _format_task_list(
    service: TodoService, config: Config, status: str
) -> str:
    tasks = await service.list_tasks(status=status, limit=25)
    if not tasks:
        return f"No {status} tasks."
    lines = [f"**{status.title()} tasks:**"]
    for task in tasks:
        title = discord.utils.escape_mentions(task["title"])
        line = f"`#{task['id']}` **{title}** · {task['priority'].upper()}"
        if task.get("class_name"):
            line += f" · {discord.utils.escape_mentions(task['class_name'])}"
        if task.get("due_at"):
            line += f"\nDue {format_local_due(task['due_at'], config.timezone_name)}"
            if task["status"] == "open":
                line += f" ({format_remaining(task['due_at'], config.timezone_name)})"
        if sum(map(len, lines)) + len(line) > 1750:
            lines.append("…more tasks are available in the panel.")
            break
        lines.append(line)
    return "\n".join(lines)


def _parse_task_id(value: str) -> int | None:
    if not _TASK_ID_RE.fullmatch(value.strip()):
        return None
    return int(value.strip())


def register_todo_slash_commands(
    tree: app_commands.CommandTree,
    config: Config,
    service: TodoService,
    panel: TodoPanelManager,
) -> None:
    """Register to-do application commands only in the configured guild."""
    target_guild = discord.Object(id=config.guild_id)
    priorities = [
        app_commands.Choice(name="Low", value="low"),
        app_commands.Choice(name="Normal", value="normal"),
        app_commands.Choice(name="High", value="high"),
    ]

    async def authorized(interaction: discord.Interaction) -> bool:
        if (
            interaction.guild_id == config.guild_id
            and interaction.user.id == config.owner_id
        ):
            return True
        await interaction.response.send_message(
            "Only the configured owner can manage tasks in the configured server.",
            ephemeral=True,
        )
        return False

    @app_commands.command(name="todo", description="Open the persistent to-do panel.")
    async def todo_command(interaction: discord.Interaction) -> None:
        if not await authorized(interaction):
            return
        try:
            if interaction.guild is not None:
                await panel.ensure_panel(interaction.guild)
            counts = await service.counts()
            link = await panel.panel_link()
            panel_link = f" [Open the task panel]({link})" if link else ""
            await interaction.response.send_message(
                f"To-do list: {counts['open']} outstanding, "
                f"{counts['completed']} completed.{panel_link}",
                ephemeral=True,
            )
        except Exception:
            logger.exception("Slash todo panel command failed")
            await interaction.response.send_message(
                "YBOT couldn't open the task panel. Please try again.", ephemeral=True
            )

    @app_commands.command(name="todo-add", description="Add a task to your to-do list.")
    @app_commands.describe(
        title="Task title",
        description="Optional task details",
        class_name="Optional class label",
        due_text="Optional local date/time, e.g. Friday at 5 PM",
        priority="Task priority",
    )
    @app_commands.rename(class_name="class", due_text="due")
    @app_commands.choices(priority=priorities)
    async def todo_add_command(
        interaction: discord.Interaction,
        title: str,
        description: str | None = None,
        class_name: str | None = None,
        due_text: str | None = None,
        priority: str = "normal",
    ) -> None:
        if not await authorized(interaction):
            return
        try:
            todo_id = await service.add_task(
                title,
                description=description,
                class_name=class_name,
                due_text=due_text,
                priority=priority,
                channel=interaction.channel,
            )
            refreshed = await panel.refresh()
            response = f"Task #{todo_id} added."
            if not refreshed:
                response += " The task is saved; use Refresh Panel to retry the panel update."
            await interaction.response.send_message(response, ephemeral=True)
        except TodoValidationError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
        except Exception:
            logger.exception("Slash todo creation failed")
            await interaction.response.send_message(
                "YBOT couldn't save that task. Please try again.", ephemeral=True
            )

    @app_commands.command(name="todo-complete", description="Complete an open task.")
    @app_commands.describe(todo_id="Select an open task")
    async def todo_complete_command(
        interaction: discord.Interaction, todo_id: int
    ) -> None:
        if not await authorized(interaction):
            return
        try:
            changed = await service.complete_task(todo_id)
            if not changed:
                await interaction.response.send_message(
                    "Open task not found.", ephemeral=True
                )
                return
            refreshed = await panel.refresh()
            response = f"Task #{todo_id} completed."
            if not refreshed:
                response += " The task is saved; use Refresh Panel to retry the panel update."
            await interaction.response.send_message(
                response, ephemeral=True
            )
        except Exception:
            logger.exception("Slash task completion failed for ID %s", todo_id)
            await interaction.response.send_message(
                "YBOT couldn't complete that task. Please try again.", ephemeral=True
            )

    @todo_complete_command.autocomplete("todo_id")
    async def todo_complete_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        return await _task_choices(interaction, current, config, service, "open")

    @app_commands.command(name="todo-remove", description="Archive a task from the open list.")
    @app_commands.describe(todo_id="Select a task to archive")
    async def todo_remove_command(
        interaction: discord.Interaction, todo_id: int
    ) -> None:
        if not await authorized(interaction):
            return
        try:
            changed = await service.archive_task(todo_id)
            if not changed:
                await interaction.response.send_message(
                    "Task not found or already archived.", ephemeral=True
                )
                return
            refreshed = await panel.refresh()
            response = f"Task #{todo_id} archived and preserved."
            if not refreshed:
                response += " Use Refresh Panel to retry the panel update."
            await interaction.response.send_message(
                response, ephemeral=True
            )
        except Exception:
            logger.exception("Slash task archive failed for ID %s", todo_id)
            await interaction.response.send_message(
                "YBOT couldn't archive that task. Please try again.", ephemeral=True
            )

    @todo_remove_command.autocomplete("todo_id")
    async def todo_remove_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        return await _task_choices(interaction, current, config, service, "all")

    @app_commands.command(name="todo-edit", description="Edit a task.")
    @app_commands.describe(
        todo_id="Select a task",
        title="New title (optional)",
        description="New description; enter clear to remove it",
        class_name="New class label; enter clear to remove it",
        due_text="New due time; enter clear to remove it",
        priority="New priority",
    )
    @app_commands.rename(class_name="class", due_text="due")
    @app_commands.choices(priority=priorities)
    async def todo_edit_command(
        interaction: discord.Interaction,
        todo_id: int,
        title: str | None = None,
        description: str | None = None,
        class_name: str | None = None,
        due_text: str | None = None,
        priority: str | None = None,
    ) -> None:
        if not await authorized(interaction):
            return
        updates: dict[str, Any] = {}
        if title is not None:
            updates["title"] = title
        if description is not None:
            updates["description"] = None if description.casefold() == "clear" else description
        if class_name is not None:
            updates["class_name"] = None if class_name.casefold() == "clear" else class_name
        if due_text is not None:
            updates["due_text"] = None if due_text.casefold() == "clear" else due_text
        if priority is not None:
            updates["priority"] = priority
        try:
            if not updates:
                raise TodoValidationError("Provide at least one field to update.")
            changed = await service.edit_task(
                todo_id, **updates, channel=interaction.channel
            )
            if not changed:
                await interaction.response.send_message(
                    "Task not found or unavailable for editing.", ephemeral=True
                )
                return
            refreshed = await panel.refresh()
            response = f"Task #{todo_id} updated."
            if not refreshed:
                response += " The task is saved; use Refresh Panel to retry the panel update."
            await interaction.response.send_message(
                response, ephemeral=True
            )
        except TodoValidationError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
        except Exception:
            logger.exception("Slash task edit failed for ID %s", todo_id)
            await interaction.response.send_message(
                "YBOT couldn't update that task. Please try again.", ephemeral=True
            )

    @todo_edit_command.autocomplete("todo_id")
    async def todo_edit_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        return await _task_choices(interaction, current, config, service, "all")

    @app_commands.command(
        name="todo-completed", description="Show completed tasks."
    )
    async def todo_completed_command(interaction: discord.Interaction) -> None:
        if not await authorized(interaction):
            return
        try:
            result = await _format_task_list(service, config, "completed")
            await interaction.response.send_message(result, ephemeral=True)
        except Exception:
            logger.exception("Slash completed-task listing failed")
            await interaction.response.send_message(
                "YBOT couldn't load completed tasks.", ephemeral=True
            )

    commands = (
        todo_command,
        todo_add_command,
        todo_complete_command,
        todo_remove_command,
        todo_edit_command,
        todo_completed_command,
    )
    for command in commands:
        tree.add_command(command, guild=target_guild)


async def _task_choices(
    interaction: discord.Interaction,
    current: str,
    config: Config,
    service: TodoService,
    status: str,
) -> list[app_commands.Choice[int]]:
    if interaction.guild_id != config.guild_id or interaction.user.id != config.owner_id:
        return []
    try:
        tasks = await service.list_tasks(status=status, limit=100)
    except Exception:
        logger.exception("Task autocomplete query failed")
        return []
    choices = []
    for task in tasks:
        if status == "all" and task["status"] == "archived":
            continue
        label = f"#{task['id']} {task['title']}"[:100]
        if current.casefold() not in label.casefold() and current not in str(task["id"]):
            continue
        choices.append(app_commands.Choice(name=label, value=task["id"]))
        if len(choices) >= 25:
            break
    return choices
