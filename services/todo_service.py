"""Validation and persistence-facing operations for personal to-dos."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from config import Config
from database.db import Database
from services.class_service import resolve_class_label
from services.reminder_parser import ReminderParseError, parse_time_expression


class TodoValidationError(ValueError):
    """Raised when task input is invalid or the requested task is unavailable."""


_UNSET = object()


class TodoService:
    def __init__(self, database: Database, config: Config) -> None:
        self.database = database
        self.config = config

    async def add_task(
        self,
        title: str,
        *,
        description: str | None = None,
        class_name: str | None = None,
        due_text: str | None = None,
        priority: str = "normal",
        channel: Any | None = None,
    ) -> int:
        clean_title = _clean_text(title, "Task title", maximum_length=100)
        clean_description = _optional_text(
            description, "Task description", maximum_length=1000
        )
        clean_class = _optional_text(class_name, "Class", maximum_length=100)
        clean_class = await resolve_class_label(
            self.database, clean_class, channel
        )
        clean_priority = _validate_priority(priority)
        due_at = self.parse_due_text(due_text)
        return await self.database.create_todo(
            title=clean_title,
            description=clean_description,
            class_name=clean_class,
            due_at=due_at,
            priority=clean_priority,
        )

    async def edit_task(
        self,
        todo_id: int,
        *,
        title: Any = _UNSET,
        description: Any = _UNSET,
        class_name: Any = _UNSET,
        due_text: Any = _UNSET,
        priority: Any = _UNSET,
        channel: Any | None = None,
    ) -> bool:
        task = await self.database.get_todo(todo_id)
        if task is None:
            raise TodoValidationError(f"Task #{todo_id} was not found.")
        if task["status"] == "archived":
            raise TodoValidationError("Archived tasks cannot be edited.")

        updates: dict[str, Any] = {}
        if title is not _UNSET and title is not None:
            updates["title"] = _clean_text(title, "Task title", maximum_length=100)
        if description is not _UNSET:
            updates["description"] = _optional_text(
                description, "Task description", maximum_length=1000
            )
        if class_name is not _UNSET:
            clean_class = _optional_text(class_name, "Class", maximum_length=100)
            updates["class_name"] = (
                await resolve_class_label(self.database, clean_class, channel)
                if clean_class is not None
                else None
            )
        elif channel is not None:
            inferred_class = await resolve_class_label(self.database, None, channel)
            if inferred_class is not None:
                updates["class_name"] = inferred_class
        if due_text is not _UNSET:
            updates["due_at"] = self.parse_due_text(due_text)
        if priority is not _UNSET and priority is not None:
            updates["priority"] = _validate_priority(priority)
        if not updates:
            raise TodoValidationError("Provide at least one field to update.")
        return await self.database.update_todo(todo_id, **updates)

    def parse_due_text(self, value: str | None) -> datetime | None:
        if value is None or not value.strip():
            return None
        try:
            # Tasks may intentionally be overdue; reminders remain future-only.
            return parse_time_expression(
                value, self.config.timezone_name, allow_past=True
            )
        except ReminderParseError as exc:
            raise TodoValidationError(str(exc)) from exc

    async def get_task(self, todo_id: int) -> dict[str, Any] | None:
        return await self.database.get_todo(todo_id)

    async def list_tasks(
        self, *, status: str = "open", limit: int = 25, offset: int = 0
    ) -> list[dict[str, Any]]:
        return await self.database.list_todos(
            status=status, limit=limit, offset=offset
        )

    async def count_tasks(self, status: str | None = None) -> int:
        return await self.database.count_todos(status)

    async def counts(self) -> dict[str, int]:
        return await self.database.todo_counts()

    async def complete_task(self, todo_id: int) -> bool:
        return await self.database.complete_todo(todo_id)

    async def archive_task(self, todo_id: int) -> bool:
        return await self.database.archive_todo(todo_id)

    async def archive_all_open(self) -> int:
        return await self.database.archive_open_todos()


def _clean_text(value: str, label: str, *, maximum_length: int) -> str:
    if not isinstance(value, str):
        raise TodoValidationError(f"{label} is required.")
    normalized = " ".join(value.split())
    if not normalized:
        raise TodoValidationError(f"{label} is required.")
    if len(normalized) > maximum_length:
        raise TodoValidationError(f"{label} must be {maximum_length} characters or fewer.")
    return normalized


def _optional_text(
    value: str | None, label: str, *, maximum_length: int
) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split())
    if not normalized:
        return None
    if len(normalized) > maximum_length:
        raise TodoValidationError(f"{label} must be {maximum_length} characters or fewer.")
    return normalized


def _validate_priority(value: str) -> str:
    priority = value.casefold().strip()
    if priority not in {"low", "normal", "high"}:
        raise TodoValidationError("Priority must be low, normal, or high.")
    return priority
