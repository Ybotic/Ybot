"""Validation and CRUD operations for private notes."""

from __future__ import annotations

from typing import Any

from database.db import Database
from services.class_service import resolve_class_label


class NoteValidationError(ValueError):
    """Raised when note input is invalid or a note cannot be edited."""


class NoteService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def create_note(
        self,
        content: str,
        *,
        title: str | None = None,
        class_name: str | None = None,
        tags: str | None = None,
        channel: Any | None = None,
    ) -> int:
        clean_content = _clean_text(content, "Note content", maximum_length=20_000)
        clean_title = _optional_text(title, "Note title", maximum_length=200)
        if clean_title is None:
            clean_title = generate_title(clean_content)
        clean_class = _optional_text(class_name, "Class", maximum_length=100)
        clean_class = await resolve_class_label(self.database, clean_class, channel)
        clean_tags = normalize_tags(tags)
        return await self.database.create_note(
            title=clean_title,
            content=clean_content,
            class_name=clean_class,
            tags=clean_tags,
        )

    async def edit_note(
        self,
        note_id: int,
        *,
        title: str | None,
        content: str,
        class_name: str | None,
        tags: str | None,
        channel: Any | None = None,
        clear_class: bool = False,
    ) -> bool:
        note = await self.database.get_note(note_id)
        if note is None or note["is_archived"]:
            raise NoteValidationError(f"Note #{note_id} was not found or is archived.")
        clean_content = _clean_text(content, "Note content", maximum_length=20_000)
        clean_title = _optional_text(title, "Note title", maximum_length=200)
        if clean_title is None:
            clean_title = generate_title(clean_content)
        clean_class = (
            None
            if clear_class
            else _optional_text(class_name, "Class", maximum_length=100)
        )
        if not clear_class:
            clean_class = await resolve_class_label(
                self.database, clean_class, channel
            )
        clean_tags = normalize_tags(tags)
        return await self.database.update_note(
            note_id,
            title=clean_title,
            content=clean_content,
            class_name=clean_class,
            tags=clean_tags,
        )

    async def get_note(self, note_id: int) -> dict[str, Any] | None:
        return await self.database.get_note(note_id)

    async def list_notes(self, *, limit: int = 20) -> list[dict[str, Any]]:
        return await self.database.list_notes(limit=limit)

    async def archive_note(self, note_id: int) -> bool:
        return await self.database.archive_note(note_id)


def generate_title(content: str, *, maximum_length: int = 80) -> str:
    """Derive a display title from the first non-empty content line; no AI used."""
    first_line = next((line.strip() for line in content.splitlines() if line.strip()), "Note")
    first_line = " ".join(first_line.split())
    if len(first_line) > maximum_length:
        first_line = first_line[: maximum_length - 1].rstrip() + "…"
    return first_line


def normalize_tags(value: str | None) -> str | None:
    if value is None:
        return None
    tags: list[str] = []
    for tag in value.split(","):
        normalized = " ".join(tag.strip().lstrip("#").split()).casefold()
        if normalized and normalized not in tags:
            if len(normalized) > 40:
                raise NoteValidationError("Each tag must be 40 characters or fewer.")
            tags.append(normalized)
        if len(tags) > 20:
            raise NoteValidationError("Use no more than 20 tags.")
    joined = ",".join(tags)
    if len(joined) > 500:
        raise NoteValidationError("Tags must total 500 characters or fewer.")
    return joined or None


def _clean_text(value: str, label: str, *, maximum_length: int) -> str:
    if not isinstance(value, str):
        raise NoteValidationError(f"{label} is required.")
    normalized = value.strip()
    if not normalized:
        raise NoteValidationError(f"{label} is required.")
    if len(normalized) > maximum_length:
        raise NoteValidationError(f"{label} must be {maximum_length} characters or fewer.")
    return normalized


def _optional_text(
    value: str | None, label: str, *, maximum_length: int
) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split()).strip()
    if not normalized:
        return None
    if len(normalized) > maximum_length:
        raise NoteValidationError(f"{label} must be {maximum_length} characters or fewer.")
    return normalized
