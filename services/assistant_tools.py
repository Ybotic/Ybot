"""Allowlisted assistant operations backed by existing YBOT services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from config import Config
from database.activity import ActivityRepository
from database.db import Database
from services.class_service import resolve_class_label
from services.digest_service import DigestService
from services.reminder_parser import parse_time_expression, validate_description
from services.todo_service import TodoService


def text(description: str, *, enum: list[str] | None = None) -> dict:
    result = {"type": "string", "description": description, "maxLength": 1000}
    if enum:
        result["enum"] = enum
    return result


ID = {"type": "integer", "minimum": 1, "maximum": 9_007_199_254_740_991, "description": "Exact resource ID from a retrieval tool; never guess"}
CLASS = text("Optional explicit class label; otherwise infer the active registered channel")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    properties: dict
    required: tuple[str, ...] = ()
    confirm: bool = False

    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": {"type": "object", "properties": self.properties,
                           "required": list(self.required), "additionalProperties": False},
        }}


SPECS = [
    ToolSpec("list_reminders", "Retrieve pending reminders and due times.", {}),
    ToolSpec("get_reminder", "Retrieve one reminder by ID.", {"id": ID}, ("id",)),
    ToolSpec("create_reminder", "Create a reminder using a supported local time expression.", {"time": text("in 30 minutes, tomorrow at 5 PM, Friday at 17:00"), "description": text("Reminder text"), "class_name": CLASS}, ("time", "description")),
    ToolSpec("cancel_reminder", "Request owner confirmation to cancel a reminder.", {"id": ID}, ("id",), True),
    ToolSpec("list_tasks", "Retrieve tasks, including IDs, due dates, and priorities.", {"status": text("Task status", enum=["open", "completed", "all"]), "class_name": CLASS}),
    ToolSpec("create_task", "Create a task only when the owner asks to add it.", {"title": text("Task title"), "description": text("Optional details"), "due_text": text("Optional due date/time"), "priority": text("Priority", enum=["low", "normal", "high"]), "class_name": CLASS}, ("title",)),
    ToolSpec("edit_task", "Edit a task by exact ID; omit fields that should remain unchanged.", {"id": ID, "title": text("New title"), "description": text("New description, empty clears"), "due_text": text("New due time, empty clears"), "priority": text("New priority", enum=["low", "normal", "high"]), "class_name": text("New explicit class, empty clears")}, ("id",)),
    ToolSpec("complete_task", "Complete an open task by exact ID.", {"id": ID}, ("id",)),
    ToolSpec("archive_task", "Request owner confirmation to remove a task by archiving it.", {"id": ID}, ("id",), True),
    ToolSpec("list_notes", "Retrieve recent note titles and content.", {}),
    ToolSpec("get_note", "Retrieve the complete content of one private note.", {"id": ID}, ("id",)),
    ToolSpec("save_note", "Save a private note requested by the owner.", {"content": text("Note content"), "title": text("Optional title"), "class_name": CLASS, "tags": text("Comma-separated tags")}, ("content",)),
    ToolSpec("archive_note", "Request owner confirmation to delete/archive a note.", {"id": ID}, ("id",), True),
    ToolSpec("list_links", "Retrieve recent saved links and summaries.", {}),
    ToolSpec("get_link", "Retrieve one saved link.", {"id": ID}, ("id",)),
    ToolSpec("save_link", "Save and safely extract a public HTTP/HTTPS webpage.", {"url": text("Complete public webpage URL"), "class_name": CLASS, "tags": text("Comma-separated tags")}, ("url",)),
    ToolSpec("archive_link", "Request owner confirmation to delete/archive a link.", {"id": ID}, ("id",), True),
    ToolSpec("search", "Search titles/content/tags/URLs in notes, links, and open/completed tasks.", {"query": text("Search words"), "type": text("Resource type", enum=["all", "notes", "links", "todos"]), "class_name": CLASS}, ("query",)),
    ToolSpec("list_classes", "Retrieve registered active and archived classes.", {}),
    ToolSpec("class_info", "Retrieve a class's counts, progress, recent notes/links, and next reminder.", {"name": text("Registered class name")}, ("name",)),
    ToolSpec("overview", "Generate a database-derived daily or weekly overview.", {"kind": text("Overview period", enum=["daily", "weekly"])}, ("kind",)),
]


class AssistantTools:
    def __init__(self, client: Any, database: Database, config: Config, digests: DigestService) -> None:
        self.client = client
        self.database = database
        self.config = config
        self.digests = digests
        self.repository = ActivityRepository(database)
        self.specs = {spec.name: spec for spec in SPECS}

    @property
    def schemas(self) -> list[dict]:
        return [spec.schema() for spec in SPECS]

    def validate(self, name: str, arguments: Any) -> dict:
        spec = self.specs.get(name)
        if spec is None or not isinstance(arguments, dict):
            raise ValueError("Unknown assistant tool or invalid arguments")
        if set(arguments) - set(spec.properties) or set(spec.required) - set(arguments):
            raise ValueError("Missing or unsupported tool arguments")
        for key, value in arguments.items():
            schema = spec.properties[key]
            if schema["type"] == "integer":
                if type(value) is not int or not schema["minimum"] <= value <= schema["maximum"]:
                    raise ValueError("Resource ID must be a positive integer")
            else:
                if not isinstance(value, str) or len(value) > schema["maxLength"]:
                    raise ValueError("Tool text is invalid or too long")
                if "enum" in schema and value not in schema["enum"]:
                    raise ValueError("Invalid tool option")
        return dict(arguments)

    async def confirmation_target(self, name: str, arguments: dict) -> str:
        resource_id = arguments["id"]
        if name == "archive_note":
            row = await self.database.get_note(resource_id)
            active = row is not None and not row["is_archived"]
            label = row["title"] if row else ""
        elif name == "archive_link":
            row = await self.database.get_link(resource_id)
            active = row is not None and not row["is_archived"]
            label = row["title"] if row else ""
        elif name == "archive_task":
            row = await self.database.get_todo(resource_id)
            active = row is not None and row["status"] != "archived"
            label = row["title"] if row else ""
        elif name == "cancel_reminder":
            row = await self.database.get_reminder(resource_id)
            active = row is not None and row["status"] in {"pending", "notified"}
            label = row["description"] if row else ""
        else:
            raise ValueError("This tool has no confirmation action")
        if not active:
            raise ValueError("The requested resource was not found or is no longer active")
        return f"{name.replace('_', ' ').title()} #{resource_id}: {label or 'Untitled'}"[:200]

    async def execute(self, name: str, arguments: dict, channel: Any, *, confirmed: bool = False) -> Any:
        args = self.validate(name, arguments)
        if self.specs[name].confirm:
            if not confirmed:
                raise ValueError("Owner confirmation is required")
            await self.confirmation_target(name, args)
            if name == "archive_task":
                changed = await self.client.todo_service.archive_task(args["id"])
                await self.client.todo_panel.refresh()
            elif name == "archive_note":
                changed = await self.client.knowledge.notes.archive_note(args["id"])
            elif name == "archive_link":
                changed = await self.client.knowledge.links.archive_link(args["id"])
                if changed:
                    await self.client.knowledge.sync_link_feed(await self.database.get_link(args["id"]))
            else:
                changed = await self.database.cancel_reminder(args["id"])
            return {"changed": changed}
        if name == "list_reminders":
            return (await self.database.list_pending_reminders())[:100]
        if name == "get_reminder":
            return await self.database.get_reminder(args["id"])
        if name == "create_reminder":
            due = parse_time_expression(args["time"], self.config.timezone_name)
            description = validate_description(args["description"])
            class_name = await resolve_class_label(self.database, args.get("class_name"), channel)
            resource_id = await self.database.create_reminder(description, due, class_name)
            return {"id": resource_id, "due_at": due.isoformat(), "class_name": class_name}
        if name == "list_tasks":
            return await self.database.list_todos(status=args.get("status", "open"), class_name=args.get("class_name"), limit=100)
        if name == "create_task":
            resource_id = await self.client.todo_service.add_task(**args, channel=channel)
            return {"id": resource_id, "panel_updated": await self.client.todo_panel.refresh()}
        if name == "edit_task":
            resource_id = args.pop("id")
            if not args:
                raise ValueError("Provide at least one field to update")
            changed = await self.client.todo_service.edit_task(resource_id, **args, channel=channel)
            return {"changed": changed, "panel_updated": await self.client.todo_panel.refresh()}
        if name == "complete_task":
            changed = await self.client.todo_service.complete_task(args["id"])
            return {"changed": changed, "panel_updated": await self.client.todo_panel.refresh()}
        if name == "list_notes":
            rows = await self.client.knowledge.notes.list_notes(limit=30)
            return [{**row, "content": row["content"][:1200]} for row in rows]
        if name == "get_note":
            row = await self.client.knowledge.notes.get_note(args["id"])
            return row if row and not row["is_archived"] else None
        if name == "save_note":
            return {"id": await self.client.knowledge.notes.create_note(**args, channel=channel)}
        if name == "list_links":
            return [{key: value for key, value in row.items() if key != "extracted_text"} for row in await self.client.knowledge.links.list_links(limit=30)]
        if name == "get_link":
            row = await self.client.knowledge.links.get_link(args["id"])
            return {key: value for key, value in row.items() if key != "extracted_text"} if row and not row["is_archived"] else None
        if name == "save_link":
            link = await self.client.knowledge.links.save_link(**args, channel=channel)
            return {"id": link["id"], "feed_posted": await self.client.knowledge.sync_link_feed(link), "extraction_status": link["extraction_status"]}
        if name == "search":
            page = await self.client.knowledge.search.search(args["query"], resource_type=args.get("type", "all"), class_name=args.get("class_name"))
            from dataclasses import asdict
            return asdict(page)
        if name == "list_classes":
            return await self.database.list_classes(self.config.guild_id, status="all")
        if name == "class_info":
            record = await self.client.classes.service.get_by_name(self.config.guild_id, args["name"])
            if not record:
                return {"error": "Class not found"}
            label = record["class_name"]
            return {
                "class": record,
                "counts": await self.database.class_counts(label),
                "recent_notes": await self.database.list_notes(class_name=label, limit=5),
                "recent_links": [{key: value for key, value in row.items() if key != "extracted_text"} for row in await self.database.list_links(class_name=label, limit=5)],
                "next_reminder": await self.database.list_pending_reminders(class_name=label, due_from=datetime.now(timezone.utc), limit=1),
            }
        if name == "overview":
            return (await self.digests.generate(args["kind"])).as_dict()
        raise ValueError("Unsupported assistant operation")
