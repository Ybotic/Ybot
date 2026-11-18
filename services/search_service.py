"""Unified local search across notes, saved links, and active to-dos."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from database.db import Database
from services.reminder_format import parse_utc_timestamp

SEARCH_TYPES = {"all", "notes", "links", "todos"}
RESULTS_PER_PAGE = 5
MAX_CANDIDATES_PER_RESOURCE = 1000


class SearchValidationError(ValueError):
    """Raised for invalid search parameters."""


@dataclass(frozen=True, slots=True)
class SearchResult:
    resource_type: str
    resource_id: int
    title: str
    excerpt: str
    class_name: str | None
    timestamp: str
    url: str | None = None


@dataclass(frozen=True, slots=True)
class SearchPage:
    query: str
    resource_type: str
    class_name: str | None
    page: int
    results: tuple[SearchResult, ...]
    has_previous: bool
    has_next: bool


class SearchService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def search(
        self,
        query: str,
        *,
        resource_type: str = "all",
        class_name: str | None = None,
        page: int = 0,
    ) -> SearchPage:
        normalized_query = " ".join(query.split())
        if not normalized_query:
            raise SearchValidationError("Enter a search query.")
        if len(normalized_query) > 200:
            raise SearchValidationError("Search queries must be 200 characters or fewer.")
        resource_type = resource_type.casefold()
        if resource_type not in SEARCH_TYPES:
            raise SearchValidationError("Search type must be all, notes, links, or todos.")
        maximum_page = 599 if resource_type == "all" else 199
        if page < 0 or page > maximum_page:
            raise SearchValidationError("That search page is not available.")
        clean_class = " ".join(class_name.split()) if class_name else None
        if clean_class is not None and len(clean_class) > 100:
            raise SearchValidationError("Class filters must be 100 characters or fewer.")
        batch_limit = min(
            MAX_CANDIDATES_PER_RESOURCE,
            (page + 1) * RESULTS_PER_PAGE + 1,
        )

        resource_types = (
            ("notes", "links", "todos") if resource_type == "all" else (resource_type,)
        )
        matches: list[SearchResult] = []
        for kind in resource_types:
            rows = await self._search_kind(
                kind, normalized_query, clean_class, batch_limit
            )
            matches.extend(
                self._to_result(kind, row, normalized_query) for row in rows
            )
        matches.sort(
            key=lambda result: (
                parse_utc_timestamp(result.timestamp),
                result.resource_type,
                result.resource_id,
            ),
            reverse=True,
        )
        offset = page * RESULTS_PER_PAGE
        visible = tuple(matches[offset : offset + RESULTS_PER_PAGE])
        return SearchPage(
            query=normalized_query,
            resource_type=resource_type,
            class_name=clean_class,
            page=page,
            results=visible,
            has_previous=page > 0,
            has_next=(offset + RESULTS_PER_PAGE) < len(matches),
        )

    async def _search_kind(
        self, kind: str, query: str, class_name: str | None, limit: int
    ) -> list[dict[str, Any]]:
        if kind == "notes":
            return await self.database.search_notes(
                query, class_name=class_name, limit=limit
            )
        if kind == "links":
            return await self.database.search_links(
                query, class_name=class_name, limit=limit
            )
        return await self.database.search_todos(
            query, class_name=class_name, limit=limit
        )

    @staticmethod
    def _to_result(
        kind: str, row: dict[str, Any], query: str
    ) -> SearchResult:
        if kind == "notes":
            text = row["content"]
            date = row["created_at"]
            url = None
        elif kind == "links":
            text = row.get("description") or row.get("extracted_text") or row["url"]
            date = row["saved_at"]
            url = row["url"]
        else:
            text = row.get("description") or row["title"]
            date = row["created_at"]
            url = None
        return SearchResult(
            resource_type=kind,
            resource_id=int(row["id"]),
            title=row.get("title") or "Untitled note",
            excerpt=_matching_excerpt(text, query),
            class_name=row.get("class_name"),
            timestamp=date,
            url=url,
        )


def _matching_excerpt(text: str | None, query: str, *, maximum_length: int = 220) -> str:
    if not text:
        return ""
    flattened = " ".join(text.split())
    lowered = flattened.casefold()
    positions = [position for term in query.casefold().split() if (position := lowered.find(term)) >= 0]
    if positions:
        start = max(0, min(positions) - 70)
        end = min(len(flattened), start + maximum_length)
        excerpt = flattened[start:end]
        if start:
            excerpt = "…" + excerpt
        if end < len(flattened):
            excerpt += "…"
        return excerpt
    return flattened[:maximum_length] + ("…" if len(flattened) > maximum_length else "")
