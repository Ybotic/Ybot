"""Persistent class-name normalization and channel-to-class lookup."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from database.db import Database


class ClassValidationError(ValueError):
    """Raised for invalid class names or unavailable class records."""


def normalize_class_name(value: str) -> tuple[str, str, str]:
    """Return display name, case-insensitive key, and Discord channel slug."""
    if not isinstance(value, str):
        raise ClassValidationError("Provide a class name.")
    display_name = " ".join(value.split()).strip()
    if not display_name:
        raise ClassValidationError("Class name cannot be empty.")
    if len(display_name) > 80:
        raise ClassValidationError("Class names must be 80 characters or fewer.")
    if any(
        not (character.isalnum() or character.isspace() or character in "._-")
        for character in display_name
    ):
        raise ClassValidationError(
            "Use letters, numbers, spaces, periods, hyphens, or underscores in class names."
        )
    normalized_name = "".join(
        character.casefold() for character in display_name if character.isalnum()
    )
    ascii_name = (
        unicodedata.normalize("NFKD", display_name)
        .encode("ascii", errors="ignore")
        .decode("ascii")
        .casefold()
    )
    channel_name = re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")
    if not normalized_name or not channel_name:
        raise ClassValidationError("That class name cannot be used for a Discord channel.")
    return display_name, normalized_name, channel_name


def class_name_key(value: str) -> str:
    """Normalize manual class labels for lookup without validating channel syntax."""
    return "".join(character.casefold() for character in value if character.isalnum())


async def infer_class_name(
    database: Database, channel: Any | None
) -> str | None:
    """Return a class only for an active registered channel mapping."""
    if channel is None:
        return None
    guild = getattr(channel, "guild", None)
    channel_id = getattr(channel, "parent_id", None) or getattr(channel, "id", None)
    if guild is None or channel_id is None:
        return None
    class_record = await database.get_class_by_channel(guild.id, channel_id)
    return class_record["class_name"] if class_record is not None else None


async def resolve_class_label(
    database: Database,
    explicit_class: str | None,
    channel: Any | None,
) -> str | None:
    """Prefer an explicit class, canonicalizing it when it matches a record."""
    if explicit_class is not None and explicit_class.strip():
        clean_value = " ".join(explicit_class.split()).strip()
        guild = getattr(channel, "guild", None) if channel is not None else None
        key = class_name_key(clean_value)
        if guild is not None and key:
            record = await database.get_class_by_name(guild.id, key)
            if record is not None:
                return record["class_name"]
        return clean_value
    return await infer_class_name(database, channel)


class ClassService:
    """Database-facing class record helpers shared by class commands."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def get_by_name(self, guild_id: int, name: str) -> dict[str, Any] | None:
        _, normalized_name, _ = normalize_class_name(name)
        return await self.database.get_class_by_name(guild_id, normalized_name)

    async def get(self, guild_id: int, class_id: int) -> dict[str, Any] | None:
        return await self.database.get_class_by_id(guild_id, class_id)

    async def list(
        self, guild_id: int, *, status: str = "active"
    ) -> list[dict[str, Any]]:
        return await self.database.list_classes(guild_id, status=status)

    async def get_active_for_channel(
        self, guild_id: int, channel_id: int
    ) -> dict[str, Any] | None:
        return await self.database.get_class_by_channel(guild_id, channel_id)


async def resolve_explicit_class_name(
    database: Database,
    guild_id: int,
    class_name: str | None,
) -> str | None:
    """Canonicalize explicit labels that refer to registered class records."""
    if class_name is None or not class_name.strip():
        return None
    clean = " ".join(class_name.split()).strip()
    key = class_name_key(clean)
    if key:
        record = await database.get_class_by_name(guild_id, key)
        if record is not None:
            return record["class_name"]
    return clean
