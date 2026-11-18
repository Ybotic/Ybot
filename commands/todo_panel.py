"""Persistent Discord to-do panel and its owner-only interactive controls."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import discord

from config import Config
from database.db import Database
from services.reminder_format import (
    format_local_due,
    format_remaining,
    parse_utc_timestamp,
)
from services.todo_service import TodoService, TodoValidationError

logger = logging.getLogger(__name__)
PAGE_SIZE = 25
PANEL_FILTERS = {"open", "completed", "archived", "all"}


class TodoPanelManager:
    """Create, restore, and update the single configured-guild todo panel."""

    def __init__(
        self,
        client: Any,
        config: Config,
        database: Database,
        todo_service: TodoService,
    ) -> None:
        self.client = client
        self.config = config
        self.database = database
        self.todo_service = todo_service
        self._panel_lock = asyncio.Lock()
        self._registered_message_ids: set[int] = set()

    async def restore_persistent_view(self) -> None:
        panel = await self.database.get_todo_panel(self.config.guild_id)
        if panel is None:
            return
        try:
            self._register_view(panel["message_id"], panel["panel_filter"])
            logger.info("Restored persistent to-do panel controls")
        except Exception:
            logger.exception("Could not restore the persistent to-do panel view")

    async def ensure_panel(self, guild: Any) -> bool:
        try:
            return await self._ensure_panel(guild)
        except Exception:
            logger.exception("Could not ensure the persistent to-do panel")
            return False

    async def _ensure_panel(self, guild: Any) -> bool:
        if guild.id != self.config.guild_id:
            return False
        async with self._panel_lock:
            panel = await self.database.get_todo_panel(guild.id)
            channel = await self._resolve_todo_channel(guild, panel)
            if channel is None:
                logger.error(
                    "Cannot create the task panel: #todo is missing in the configured "
                    "guild. Run YBOT server bootstrap and grant View Channels and Send Messages."
                )
                return False

            if panel is not None:
                stored_channel = guild.get_channel(panel["channel_id"])
                if getattr(stored_channel, "name", None) == "todo":
                    channel = stored_channel
                try:
                    message = await channel.fetch_message(panel["message_id"])
                except discord.NotFound:
                    message = None
                except discord.HTTPException:
                    logger.exception("Could not fetch the stored to-do panel message")
                    return False
                if message is not None:
                    if await self._edit_panel_message(message, panel):
                        logger.info("Updated existing to-do panel message ID %s", message.id)
                        return True
                    try:
                        await channel.fetch_message(panel["message_id"])
                    except discord.NotFound:
                        return await self._create_panel(channel)
                    except discord.HTTPException:
                        logger.exception("Could not verify the to-do panel after an edit error")
                    return False

            return await self._create_panel(channel)

    async def refresh(self) -> bool:
        try:
            return await self._refresh()
        except Exception:
            logger.exception("Could not refresh the persistent to-do panel")
            return False

    async def _refresh(self) -> bool:
        panel = await self.database.get_todo_panel(self.config.guild_id)
        guild = self.client.get_guild(self.config.guild_id)
        if guild is None:
            logger.error("Cannot refresh the to-do panel: configured guild is unavailable")
            return False
        if panel is None:
            return await self.ensure_panel(guild)

        channel = guild.get_channel(panel["channel_id"])
        if channel is None:
            channel = await self._resolve_todo_channel(guild, panel)
        if channel is None:
            logger.error("Cannot refresh the to-do panel: #todo is unavailable")
            return False
        try:
            message = await channel.fetch_message(panel["message_id"])
        except discord.NotFound:
            return await self.ensure_panel(guild)
        except discord.HTTPException:
            logger.exception("Could not fetch the to-do panel for refresh")
            return False
        if await self._edit_panel_message(message, panel):
            return True
        try:
            await channel.fetch_message(panel["message_id"])
        except discord.NotFound:
            return await self.ensure_panel(guild)
        except discord.HTTPException:
            logger.exception("Could not verify the to-do panel after a refresh error")
        return False

    async def panel_link(self) -> str | None:
        panel = await self.database.get_todo_panel(self.config.guild_id)
        if panel is None:
            return None
        return (
            f"https://discord.com/channels/{self.config.guild_id}/"
            f"{panel['channel_id']}/{panel['message_id']}"
        )

    async def get_visible_tasks(self) -> tuple[list[dict[str, Any]], str]:
        panel = await self.database.get_todo_panel(self.config.guild_id)
        filter_name = panel["panel_filter"] if panel else "open"
        page = panel["page"] if panel else 0
        tasks = await self.todo_service.list_tasks(
            status=filter_name,
            limit=PAGE_SIZE,
            offset=page * PAGE_SIZE,
        )
        return tasks, filter_name

    async def get_selection_tasks(self, action: str) -> list[dict[str, Any]]:
        tasks, filter_name = await self.get_visible_tasks()
        if action == "complete" and filter_name != "open":
            page = 0
            tasks = await self.todo_service.list_tasks(
                status="open", limit=PAGE_SIZE, offset=page
            )
        if action in {"remove", "edit"} and filter_name == "archived":
            return []
        return tasks

    async def change_filter(self, panel_filter: str) -> bool:
        if panel_filter not in PANEL_FILTERS:
            return False
        await self.database.set_todo_panel_state(
            self.config.guild_id, page=0, panel_filter=panel_filter
        )
        return await self.refresh()

    async def shift_page(self, direction: int) -> bool:
        panel = await self.database.get_todo_panel(self.config.guild_id)
        if panel is None:
            return await self.refresh()
        filter_name = panel["panel_filter"]
        total = await self.todo_service.count_tasks(
            None if filter_name == "all" else filter_name
        )
        page_count = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        new_page = max(0, min(page_count - 1, panel["page"] + direction))
        await self.database.set_todo_panel_state(self.config.guild_id, page=new_page)
        return await self.refresh()

    async def _resolve_todo_channel(
        self, guild: Any, panel: dict[str, Any] | None
    ) -> Any | None:
        if panel is not None:
            stored_channel = guild.get_channel(panel["channel_id"])
            if getattr(stored_channel, "name", None) == "todo":
                return stored_channel
        resource = await self.database.get_resource(
            self.config.guild_id, "text_channel", "todo"
        )
        if resource is not None:
            channel = guild.get_channel(resource["resource_id"])
            if channel is not None:
                return channel
        return next(
            (
                candidate
                for candidate in getattr(guild, "text_channels", ())
                if candidate.name == "todo"
                and getattr(getattr(candidate, "category", None), "name", "PLANNER")
                == "PLANNER"
            ),
            None,
        )

    async def _create_panel(self, channel: Any) -> bool:
        panel = await self.database.get_todo_panel(self.config.guild_id)
        active_filter = panel["panel_filter"] if panel else "open"
        view = TodoPanelView(self, active_filter)
        embed = await self.build_embed()
        try:
            message = await channel.send(
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.exception("Could not create the YBOT to-do panel in #todo")
            return False
        try:
            await self.database.save_todo_panel(
                self.config.guild_id, channel.id, message.id
            )
        except Exception:
            logger.exception("Panel was sent but its message ID could not be stored")
            return False
        self._registered_message_ids.add(message.id)
        logger.info("Created persistent to-do panel message ID %s", message.id)
        return True

    async def _edit_panel_message(
        self, message: Any, panel: dict[str, Any]
    ) -> bool:
        embed = await self.build_embed(panel)
        try:
            await message.edit(
                embed=embed,
                view=TodoPanelView(
                    self, panel.get("panel_filter", "open")
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            self._registered_message_ids.add(message.id)
            return True
        except discord.NotFound:
            return False
        except discord.HTTPException:
            logger.exception("Could not update the YBOT to-do panel")
            return False

    async def build_embed(
        self, panel: dict[str, Any] | None = None
    ) -> discord.Embed:
        if panel is None:
            panel = await self.database.get_todo_panel(self.config.guild_id)
        filter_name = panel["panel_filter"] if panel else "open"
        page = panel["page"] if panel else 0
        counts = await self.todo_service.counts()
        total = counts[filter_name] if filter_name != "all" else sum(counts.values())
        page_count = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, page_count - 1))
        if panel is not None and panel["page"] != page:
            await self.database.set_todo_panel_state(self.config.guild_id, page=page)
        tasks = await self.todo_service.list_tasks(
            status=filter_name,
            limit=PAGE_SIZE,
            offset=page * PAGE_SIZE,
        )

        embed = discord.Embed(
            title="YBOT To-Do Panel",
            description=(
                f"**Outstanding:** {counts['open']}  ·  "
                f"**Completed:** {counts['completed']}\n"
                f"Filter: **{filter_name.title()}**"
            ),
            color=discord.Color.blurple(),
        )
        if not tasks:
            embed.add_field(
                name="No tasks on this page",
                value="Use **Add Task** to add one, or change the filter.",
                inline=False,
            )
        else:
            for task in tasks:
                priority = task["priority"]
                priority_label = {
                    "high": "🔴 HIGH",
                    "normal": "🟡 NORMAL",
                    "low": "🔹 LOW",
                }[priority]
                value_parts = [
                    f"**{discord.utils.escape_mentions(task['title'])}**"
                ]
                if task.get("description"):
                    description = discord.utils.escape_mentions(task["description"])
                    if len(description) > 400:
                        description = description[:397] + "…"
                    value_parts.append(description)
                if task.get("class_name"):
                    value_parts.append(
                        f"Class: {discord.utils.escape_mentions(task['class_name'])}"
                    )
                if task.get("due_at"):
                    due_at = parse_utc_timestamp(task["due_at"])
                    overdue = due_at <= datetime.now(timezone.utc) and task["status"] == "open"
                    due_label = format_local_due(task["due_at"], self.config.timezone_name)
                    if overdue:
                        remaining = format_remaining(
                            task["due_at"], self.config.timezone_name
                        )
                        value_parts.append(f"⚠️ **OVERDUE** · {due_label} ({remaining})")
                    elif task["status"] == "open":
                        remaining = format_remaining(
                            task["due_at"], self.config.timezone_name
                        )
                        value_parts.append(f"Due: {due_label} ({remaining})")
                    else:
                        value_parts.append(f"Due: {due_label}")
                if task["status"] != "open":
                    value_parts.append(f"Status: {task['status'].title()}")
                embed.add_field(
                    name=f"#{task['id']} · {priority_label}",
                    value="\n".join(value_parts),
                    inline=False,
                )
        embed.set_footer(text=f"Page {page + 1} of {page_count} · Tasks are saved automatically")
        return embed

    def _register_view(self, message_id: int, panel_filter: str = "open") -> None:
        if message_id in self._registered_message_ids:
            return
        self.client.add_view(
            TodoPanelView(self, panel_filter), message_id=message_id
        )
        self._registered_message_ids.add(message_id)


class TodoPanelView(discord.ui.View):
    def __init__(self, manager: TodoPanelManager, panel_filter: str = "open") -> None:
        super().__init__(timeout=None)
        self.manager = manager
        self.add_item(TodoFilterSelect(self, panel_filter))

    @discord.ui.button(
        label="Add Task",
        style=discord.ButtonStyle.primary,
        custom_id="ybot:todo:add",
        row=0,
    )
    async def add_task_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        del button
        if not await self._authorize_panel(interaction):
            return
        await interaction.response.send_modal(AddTodoModal(self.manager, interaction.channel))

    @discord.ui.button(
        label="Complete Task",
        style=discord.ButtonStyle.success,
        custom_id="ybot:todo:complete",
        row=0,
    )
    async def complete_task_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        del button
        await self._show_tasks(interaction, "complete")

    @discord.ui.button(
        label="Remove Task",
        style=discord.ButtonStyle.danger,
        custom_id="ybot:todo:remove",
        row=0,
    )
    async def remove_task_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        del button
        await self._show_tasks(interaction, "remove")

    @discord.ui.button(
        label="Edit Task",
        style=discord.ButtonStyle.secondary,
        custom_id="ybot:todo:edit",
        row=0,
    )
    async def edit_task_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        del button
        await self._show_tasks(interaction, "edit")

    @discord.ui.button(
        label="Refresh Panel",
        style=discord.ButtonStyle.secondary,
        custom_id="ybot:todo:refresh",
        row=0,
    )
    async def refresh_panel_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        del button
        if not await self._authorize_panel(interaction):
            return
        await interaction.response.defer()
        if not await self.manager.refresh():
            await interaction.followup.send(
                "YBOT couldn't refresh the panel. The task data is still saved.",
                ephemeral=True,
            )

    @discord.ui.button(
        label="Previous Page",
        style=discord.ButtonStyle.secondary,
        custom_id="ybot:todo:previous",
        row=1,
    )
    async def previous_page_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        del button
        await self._shift_page(interaction, -1)

    @discord.ui.button(
        label="Next Page",
        style=discord.ButtonStyle.secondary,
        custom_id="ybot:todo:next",
        row=1,
    )
    async def next_page_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        del button
        await self._shift_page(interaction, 1)

    async def _show_tasks(self, interaction: discord.Interaction, action: str) -> None:
        if not await self._authorize_panel(interaction):
            return
        try:
            tasks = await self.manager.get_selection_tasks(action)
        except Exception:
            logger.exception("Could not load tasks for panel action %s", action)
            await interaction.response.send_message(
                "YBOT couldn't load tasks. Please try again later.", ephemeral=True
            )
            return
        if not tasks:
            await interaction.response.send_message(
                "There are no tasks available for that action on this page.",
                ephemeral=True,
            )
            return
        view = TodoTaskSelectionView(self.manager, action, tasks)
        await interaction.response.send_message(
            f"Select a task to {action}:", view=view, ephemeral=True
        )

    async def _shift_page(
        self, interaction: discord.Interaction, direction: int
    ) -> None:
        if not await self._authorize_panel(interaction):
            return
        await interaction.response.defer()
        if not await self.manager.shift_page(direction):
            await interaction.followup.send(
                "YBOT couldn't change the panel page.", ephemeral=True
            )

    async def _authorize_panel(self, interaction: discord.Interaction) -> bool:
        if not await _authorize_owner(interaction, self.manager.config):
            return False
        try:
            panel = await self.manager.database.get_todo_panel(
                self.manager.config.guild_id
            )
        except Exception:
            logger.exception("Could not validate the active to-do panel")
            await interaction.response.send_message(
                "YBOT couldn't verify this panel. Please try again later.",
                ephemeral=True,
            )
            return False
        if (
            panel is None
            or getattr(interaction.message, "id", None) != panel["message_id"]
        ):
            await interaction.response.send_message(
                "This panel is no longer active. Use the current panel in #todo.",
                ephemeral=True,
            )
            return False
        return True


class TodoFilterSelect(discord.ui.Select):
    def __init__(self, parent: TodoPanelView, active_filter: str) -> None:
        super().__init__(
            custom_id="ybot:todo:filter",
            placeholder="Filter tasks…",
            min_values=1,
            max_values=1,
            row=2,
            options=[
                discord.SelectOption(
                    label="Open tasks", value="open", default=active_filter == "open"
                ),
                discord.SelectOption(
                    label="Completed tasks",
                    value="completed",
                    default=active_filter == "completed",
                ),
                discord.SelectOption(
                    label="Archived tasks",
                    value="archived",
                    default=active_filter == "archived",
                ),
                discord.SelectOption(
                    label="All tasks", value="all", default=active_filter == "all"
                ),
            ],
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.parent_view._authorize_panel(interaction):
            return
        await interaction.response.defer()
        if not await self.parent_view.manager.change_filter(self.values[0]):
            await interaction.followup.send(
                "YBOT couldn't change the task filter.", ephemeral=True
            )


class TodoTaskSelectionView(discord.ui.View):
    def __init__(
        self,
        manager: TodoPanelManager,
        action: str,
        tasks: list[dict[str, Any]],
    ) -> None:
        super().__init__(timeout=180)
        self.manager = manager
        self.action = action
        self.add_item(TodoTaskSelect(self, tasks))

    async def selected(
        self, interaction: discord.Interaction, todo_id_text: str
    ) -> None:
        if not await _authorize_owner(interaction, self.manager.config):
            return
        if (
            not todo_id_text.isascii()
            or not todo_id_text.isdigit()
            or len(todo_id_text) > 19
            or int(todo_id_text) <= 0
        ):
            await interaction.response.send_message("Choose a valid task.", ephemeral=True)
            return
        try:
            todo_id = int(todo_id_text)
            task = await self.manager.todo_service.get_task(todo_id)
        except Exception:
            logger.exception("Could not validate selected task ID")
            await interaction.response.send_message(
                "YBOT couldn't load that task. Please try again.", ephemeral=True
            )
            return
        if task is None:
            await interaction.response.send_message("Task not found.", ephemeral=True)
            return

        if self.action == "edit":
            if task["status"] == "archived":
                await interaction.response.send_message(
                    "Archived tasks cannot be edited.", ephemeral=True
                )
                return
            await interaction.response.send_modal(
                EditTodoModal(self.manager, task)
            )
            return

        try:
            if self.action == "complete":
                changed = await self.manager.todo_service.complete_task(todo_id)
                message = f"Task #{todo_id} completed."
            else:
                changed = await self.manager.todo_service.archive_task(todo_id)
                message = f"Task #{todo_id} archived."
        except Exception:
            logger.exception("Could not update task ID %s", todo_id)
            await interaction.response.send_message(
                "YBOT couldn't update that task. Please try again.", ephemeral=True
            )
            return
        if not changed:
            await interaction.response.send_message(
                "That task is no longer available for this action.", ephemeral=True
            )
            return
        refreshed = await self.manager.refresh()
        if not refreshed:
            message += " The task is saved; use Refresh Panel to retry the panel update."
        await interaction.response.send_message(message, ephemeral=True)


class TodoTaskSelect(discord.ui.Select):
    def __init__(
        self, parent: TodoTaskSelectionView, tasks: list[dict[str, Any]]
    ) -> None:
        options = []
        for task in tasks:
            label = f"#{task['id']} {task['title']}"[:100]
            description = task.get("class_name") or task["status"].title()
            options.append(
                discord.SelectOption(
                    label=label,
                    value=str(task["id"]),
                    description=description[:100],
                )
            )
        super().__init__(
            custom_id=f"ybot:todo:select:{parent.action}",
            placeholder="Choose a task…",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.parent_view.selected(interaction, self.values[0])


class AddTodoModal(discord.ui.Modal):
    def __init__(self, manager: TodoPanelManager, channel: Any | None) -> None:
        super().__init__(title="Add a task", custom_id="ybot:todo:add-modal")
        self.manager = manager
        self.channel = channel
        self.title_field = discord.ui.TextInput(
            label="Task title", max_length=100, required=True
        )
        self.description_field = discord.ui.TextInput(
            label="Description", max_length=1000, required=False, style=discord.TextStyle.paragraph
        )
        self.class_field = discord.ui.TextInput(
            label="Class (optional)", max_length=100, required=False
        )
        self.due_field = discord.ui.TextInput(
            label="Due date/time (optional)",
            max_length=100,
            required=False,
            placeholder="Friday at 5 PM or in 2 hours",
        )
        self.priority_field = discord.ui.TextInput(
            label="Priority: low, normal, or high",
            max_length=6,
            required=False,
            default="normal",
        )
        for item in (
            self.title_field,
            self.description_field,
            self.class_field,
            self.due_field,
            self.priority_field,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _authorize_owner(interaction, self.manager.config):
            return
        try:
            todo_id = await self.manager.todo_service.add_task(
                self.title_field.value,
                description=self.description_field.value,
                class_name=self.class_field.value,
                due_text=self.due_field.value,
                priority=self.priority_field.value or "normal",
                channel=self.channel,
            )
        except TodoValidationError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        except Exception:
            logger.exception("Could not save task from Add Task modal")
            await interaction.response.send_message(
                "YBOT couldn't save that task. Please try again.", ephemeral=True
            )
            return
        refreshed = await self.manager.refresh()
        response = f"Task #{todo_id} added."
        if not refreshed:
            response += " The task is saved; use Refresh Panel to update the panel."
        await interaction.response.send_message(response, ephemeral=True)


class EditTodoModal(discord.ui.Modal):
    def __init__(
        self, manager: TodoPanelManager, task: dict[str, Any]
    ) -> None:
        super().__init__(
            title=f"Edit task #{task['id']}",
            custom_id=f"ybot:todo:edit-modal:{task['id']}",
        )
        self.manager = manager
        self.todo_id = task["id"]
        self.title_field = discord.ui.TextInput(
            label="Task title",
            max_length=100,
            required=True,
            default=task["title"],
        )
        self.description_field = discord.ui.TextInput(
            label="Description (blank clears)",
            max_length=1000,
            required=False,
            default=task.get("description") or "",
            style=discord.TextStyle.paragraph,
        )
        self.class_field = discord.ui.TextInput(
            label="Class (blank clears)",
            max_length=100,
            required=False,
            default=task.get("class_name") or "",
        )
        self.due_field = discord.ui.TextInput(
            label="Due date/time (blank clears)",
            max_length=100,
            required=False,
            default=_format_due_input(task.get("due_at"), manager.config),
            placeholder="Friday at 5 PM or in 2 hours",
        )
        self.priority_field = discord.ui.TextInput(
            label="Priority: low, normal, or high",
            max_length=6,
            required=False,
            default=task["priority"],
        )
        for item in (
            self.title_field,
            self.description_field,
            self.class_field,
            self.due_field,
            self.priority_field,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _authorize_owner(interaction, self.manager.config):
            return
        try:
            changed = await self.manager.todo_service.edit_task(
                self.todo_id,
                title=self.title_field.value,
                description=self.description_field.value,
                class_name=self.class_field.value,
                due_text=self.due_field.value,
                priority=self.priority_field.value or "normal",
            )
        except TodoValidationError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        except Exception:
            logger.exception("Could not edit task ID %s", self.todo_id)
            await interaction.response.send_message(
                "YBOT couldn't update that task. Please try again.", ephemeral=True
            )
            return
        if not changed:
            await interaction.response.send_message(
                "That task is no longer available for editing.", ephemeral=True
            )
            return
        refreshed = await self.manager.refresh()
        response = f"Task #{self.todo_id} updated."
        if not refreshed:
            response += " The task is saved; use Refresh Panel to update the panel."
        await interaction.response.send_message(response, ephemeral=True)


class TodoEditLauncherView(discord.ui.View):
    """Temporary button used to bridge a prefix edit command to a modal."""

    def __init__(
        self, manager: TodoPanelManager, todo_id: int, owner_id: int, guild_id: int
    ) -> None:
        super().__init__(timeout=180)
        self.manager = manager
        self.todo_id = todo_id
        self.owner_id = owner_id
        self.guild_id = guild_id
        button = discord.ui.Button(
            label="Open task editor",
            style=discord.ButtonStyle.primary,
            custom_id=f"ybot:todo:edit-launch:{todo_id}",
        )
        button.callback = self._open_editor
        self.add_item(button)

    async def _open_editor(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id or interaction.guild_id != self.guild_id:
            await interaction.response.send_message(
                "Only the configured owner can edit tasks.", ephemeral=True
            )
            return
        try:
            task = await self.manager.todo_service.get_task(self.todo_id)
        except Exception:
            logger.exception("Could not load task ID %s for editing", self.todo_id)
            await interaction.response.send_message(
                "YBOT couldn't load that task. Please try again later.", ephemeral=True
            )
            return
        if task is None or task["status"] == "archived":
            await interaction.response.send_message(
                "That task was not found or has been archived.", ephemeral=True
            )
            return
        await interaction.response.send_modal(EditTodoModal(self.manager, task))


async def _authorize_owner(
    interaction: discord.Interaction, config: Config
) -> bool:
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


def _format_due_input(due_at: str | None, config: Config) -> str:
    if due_at is None:
        return ""
    localized = parse_utc_timestamp(due_at).astimezone(ZoneInfo(config.timezone_name))
    hour = localized.strftime("%I").lstrip("0") or "12"
    return (
        f"{localized:%B} {localized.day}, {localized.year} at "
        f"{hour}:{localized:%M} {localized:%p}"
    )
