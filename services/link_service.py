"""Saved-link persistence and metadata lifecycle."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

from database.db import Database
from services.link_extractor import (
    LinkMetadataExtractor,
    LinkValidationError,
    ValidatedURL,
)
from services.class_service import resolve_class_label

logger = logging.getLogger(__name__)


class LinkServiceError(ValueError):
    """Raised for invalid link metadata or unavailable saved-link records."""


class LinkService:
    def __init__(self, database: Database, extractor: LinkMetadataExtractor) -> None:
        self.database = database
        self.extractor = extractor

    async def save_link(
        self,
        url: str,
        *,
        class_name: str | None = None,
        tags: str | None = None,
        channel: Any | None = None,
    ) -> dict[str, Any]:
        target = await self._validate_url(url)
        clear_class = class_name is not None and class_name.casefold().strip() == "clear"
        clean_class = None if clear_class else _optional_text(class_name, "Class", 100)
        if not clear_class:
            clean_class = await resolve_class_label(self.database, clean_class, channel)
        clean_tags = _normalize_tags(tags)
        fallback_title = _fallback_title(target)
        link_id = await self.database.create_link(
            url=target.url,
            normalized_url=target.normalized_url,
            title=fallback_title,
            class_name=clean_class,
            tags=clean_tags,
            extraction_status="pending",
        )
        await self._extract_and_store(link_id, target, fallback_title)
        link = await self.database.get_link(link_id)
        if link is None:
            raise LinkServiceError("The saved link could not be reloaded.")
        return link

    async def edit_link(
        self,
        link_id: int,
        *,
        url: str | None = None,
        class_name: str | None = None,
        tags: str | None = None,
        channel: Any | None = None,
    ) -> dict[str, Any]:
        link = await self.database.get_link(link_id)
        if link is None or link["is_archived"]:
            raise LinkServiceError(f"Saved link #{link_id} was not found or is archived.")

        clean_class = _optional_text(class_name, "Class", 100)
        clean_class = await resolve_class_label(self.database, clean_class, channel)
        clean_tags = _normalize_tags(tags)
        if url is None or not url.strip():
            await self.database.update_link(
                link_id,
                class_name=clean_class,
                tags=clean_tags,
            )
        else:
            target = await self._validate_url(url)
            fallback_title = _fallback_title(target)
            await self.database.update_link(
                link_id,
                url=target.url,
                normalized_url=target.normalized_url,
                title=fallback_title,
                description=None,
                extracted_text=None,
                class_name=clean_class,
                tags=clean_tags,
                extraction_status="pending",
            )
            await self._extract_and_store(link_id, target, fallback_title)

        updated = await self.database.get_link(link_id)
        if updated is None:
            raise LinkServiceError(f"Saved link #{link_id} could not be reloaded.")
        return updated

    async def get_link(self, link_id: int) -> dict[str, Any] | None:
        return await self.database.get_link(link_id)

    async def list_links(self, *, limit: int = 20) -> list[dict[str, Any]]:
        return await self.database.list_links(limit=limit)

    async def archive_link(self, link_id: int) -> bool:
        return await self.database.archive_link(link_id)

    async def _validate_url(self, url: str) -> ValidatedURL:
        try:
            return await self.extractor.validate_url(url)
        except LinkValidationError as exc:
            raise LinkServiceError(str(exc)) from exc

    async def _extract_and_store(
        self, link_id: int, target: ValidatedURL, fallback_title: str
    ) -> None:
        try:
            metadata = await self.extractor.extract(target.url)
        except Exception as exc:
            # URL remains saved; no page content or URL is written to logs.
            logger.info(
                "Metadata extraction failed for saved link ID %s (%s)",
                link_id,
                type(exc).__name__,
            )
            await self.database.update_link_metadata(
                link_id,
                normalized_url=target.normalized_url,
                title=fallback_title,
                description=None,
                extracted_text=None,
                extraction_status="failed",
            )
            return
        await self.database.update_link_metadata(
            link_id,
            normalized_url=metadata.normalized_url,
            title=metadata.title or fallback_title,
            description=metadata.description,
            extracted_text=metadata.extracted_text,
            extraction_status="success",
        )


def _fallback_title(target: ValidatedURL) -> str:
    host = urlsplit(target.url).hostname or target.host
    return host[:250]


def _optional_text(value: str | None, label: str, maximum_length: int) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split()).strip()
    if not normalized:
        return None
    if len(normalized) > maximum_length:
        raise LinkServiceError(f"{label} must be {maximum_length} characters or fewer.")
    return normalized


def _normalize_tags(value: str | None) -> str | None:
    if value is None:
        return None
    tags: list[str] = []
    for raw_tag in value.split(","):
        tag = " ".join(raw_tag.strip().lstrip("#").split()).casefold()
        if not tag or tag in tags:
            continue
        if len(tag) > 40:
            raise LinkServiceError("Each tag must be 40 characters or fewer.")
        tags.append(tag)
        if len(tags) > 20:
            raise LinkServiceError("Use no more than 20 tags.")
    result = ",".join(tags)
    if len(result) > 500:
        raise LinkServiceError("Tags must total 500 characters or fewer.")
    return result or None
