"""Async SQLite database access."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


class Database:
    """Small async repository around the YBOT SQLite database."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._connection: aiosqlite.Connection | None = None
        self._fts5_available = False

    @property
    def is_connected(self) -> bool:
        return self._connection is not None

    async def initialize(self) -> None:
        if self._connection is not None:
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self.path)
        connection.row_factory = aiosqlite.Row
        await connection.create_function(
            "ybot_class_key", 1, _class_key, deterministic=True
        )
        await connection.execute("PRAGMA foreign_keys = ON")
        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        await connection.executescript(schema)
        # Additive migration for completion dates not recorded in earlier phases.
        async with connection.execute("PRAGMA table_info(reminders)") as cursor:
            columns = {row["name"] for row in await cursor.fetchall()}
        if "completed_at" not in columns:
            await connection.execute("ALTER TABLE reminders ADD COLUMN completed_at TEXT")
        await connection.commit()
        self._connection = connection
        await self._initialize_fts5()
        logger.info("SQLite database initialized at %s", self.path)

    async def close(self) -> None:
        if self._connection is None:
            return
        await self._connection.close()
        self._connection = None
        logger.info("SQLite database connection closed")

    async def ping(self) -> bool:
        if self._connection is None:
            return False
        try:
            async with self._connection.execute("SELECT 1") as cursor:
                await cursor.fetchone()
            return True
        except aiosqlite.Error:
            logger.exception("SQLite health check failed")
            return False

    @property
    def fts5_available(self) -> bool:
        return self._fts5_available

    async def _initialize_fts5(self) -> None:
        connection = self._require_connection()
        definitions = (
            (
                "notes_fts",
                "CREATE VIRTUAL TABLE notes_fts USING fts5("
                "title, content, class_name, tags, content='notes', content_rowid='id')",
                """
                CREATE TRIGGER IF NOT EXISTS notes_fts_ai AFTER INSERT ON notes BEGIN
                    INSERT INTO notes_fts(rowid, title, content, class_name, tags)
                    VALUES (new.id, new.title, new.content, new.class_name, new.tags);
                END;
                CREATE TRIGGER IF NOT EXISTS notes_fts_ad AFTER DELETE ON notes BEGIN
                    INSERT INTO notes_fts(notes_fts, rowid, title, content, class_name, tags)
                    VALUES ('delete', old.id, old.title, old.content, old.class_name, old.tags);
                END;
                CREATE TRIGGER IF NOT EXISTS notes_fts_au AFTER UPDATE ON notes BEGIN
                    INSERT INTO notes_fts(notes_fts, rowid, title, content, class_name, tags)
                    VALUES ('delete', old.id, old.title, old.content, old.class_name, old.tags);
                    INSERT INTO notes_fts(rowid, title, content, class_name, tags)
                    VALUES (new.id, new.title, new.content, new.class_name, new.tags);
                END;
                """,
            ),
            (
                "links_fts",
                "CREATE VIRTUAL TABLE links_fts USING fts5("
                "url, normalized_url, title, description, extracted_text, class_name, tags, "
                "content='links', content_rowid='id')",
                """
                CREATE TRIGGER IF NOT EXISTS links_fts_ai AFTER INSERT ON links BEGIN
                    INSERT INTO links_fts(
                        rowid, url, normalized_url, title, description,
                        extracted_text, class_name, tags
                    ) VALUES (
                        new.id, new.url, new.normalized_url, new.title, new.description,
                        new.extracted_text, new.class_name, new.tags
                    );
                END;
                CREATE TRIGGER IF NOT EXISTS links_fts_ad AFTER DELETE ON links BEGIN
                    INSERT INTO links_fts(
                        links_fts, rowid, url, normalized_url, title, description,
                        extracted_text, class_name, tags
                    ) VALUES (
                        'delete', old.id, old.url, old.normalized_url, old.title, old.description,
                        old.extracted_text, old.class_name, old.tags
                    );
                END;
                CREATE TRIGGER IF NOT EXISTS links_fts_au AFTER UPDATE ON links BEGIN
                    INSERT INTO links_fts(
                        links_fts, rowid, url, normalized_url, title, description,
                        extracted_text, class_name, tags
                    ) VALUES (
                        'delete', old.id, old.url, old.normalized_url, old.title, old.description,
                        old.extracted_text, old.class_name, old.tags
                    );
                    INSERT INTO links_fts(
                        rowid, url, normalized_url, title, description,
                        extracted_text, class_name, tags
                    ) VALUES (
                        new.id, new.url, new.normalized_url, new.title, new.description,
                        new.extracted_text, new.class_name, new.tags
                    );
                END;
                """,
            ),
            (
                "todos_fts",
                "CREATE VIRTUAL TABLE todos_fts USING fts5("
                "title, description, class_name, content='todos', content_rowid='id')",
                """
                CREATE TRIGGER IF NOT EXISTS todos_fts_ai AFTER INSERT ON todos BEGIN
                    INSERT INTO todos_fts(rowid, title, description, class_name)
                    VALUES (new.id, new.title, new.description, new.class_name);
                END;
                CREATE TRIGGER IF NOT EXISTS todos_fts_ad AFTER DELETE ON todos BEGIN
                    INSERT INTO todos_fts(todos_fts, rowid, title, description, class_name)
                    VALUES ('delete', old.id, old.title, old.description, old.class_name);
                END;
                CREATE TRIGGER IF NOT EXISTS todos_fts_au AFTER UPDATE ON todos BEGIN
                    INSERT INTO todos_fts(todos_fts, rowid, title, description, class_name)
                    VALUES ('delete', old.id, old.title, old.description, old.class_name);
                    INSERT INTO todos_fts(rowid, title, description, class_name)
                    VALUES (new.id, new.title, new.description, new.class_name);
                END;
                """,
            ),
        )

        try:
            for table_name, create_sql, trigger_sql in definitions:
                async with connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (table_name,),
                ) as cursor:
                    exists = await cursor.fetchone() is not None
                if not exists:
                    await connection.execute(create_sql)
                await connection.executescript(trigger_sql)
                if not exists:
                    await connection.execute(
                        f"INSERT INTO {table_name}({table_name}) VALUES ('rebuild')"
                    )
            await connection.commit()
            self._fts5_available = True
        except aiosqlite.Error as exc:
            await connection.rollback()
            self._fts5_available = False
            logger.info(
                "SQLite FTS5 unavailable; knowledge search will use LIKE fallback (%s)",
                type(exc).__name__,
            )

    async def set_setup_completed(self, guild_id: int, completed: bool) -> None:
        connection = self._require_connection()
        await connection.execute(
            """
            INSERT INTO server_config (guild_id, setup_completed)
            VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                setup_completed = excluded.setup_completed,
                updated_at = CURRENT_TIMESTAMP
            """,
            (guild_id, int(completed)),
        )
        await connection.commit()

    async def get_server_config(self, guild_id: int) -> dict[str, Any] | None:
        connection = self._require_connection()
        async with connection.execute(
            "SELECT * FROM server_config WHERE guild_id = ?", (guild_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def upsert_resource(
        self,
        *,
        resource_id: int,
        guild_id: int,
        resource_type: str,
        resource_name: str,
        parent_category_id: int | None,
    ) -> None:
        connection = self._require_connection()
        await connection.execute(
            """
            INSERT INTO discord_resources (
                resource_id, guild_id, resource_type, resource_name,
                parent_category_id
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, resource_type, resource_name) DO UPDATE SET
                resource_id = excluded.resource_id,
                parent_category_id = excluded.parent_category_id
            """,
            (
                resource_id,
                guild_id,
                resource_type,
                resource_name,
                parent_category_id,
            ),
        )
        await connection.commit()

    async def list_resources(self, guild_id: int) -> list[dict[str, Any]]:
        connection = self._require_connection()
        async with connection.execute(
            """
            SELECT * FROM discord_resources
            WHERE guild_id = ?
            ORDER BY resource_type, resource_name
            """,
            (guild_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_resource(
        self, guild_id: int, resource_type: str, resource_name: str
    ) -> dict[str, Any] | None:
        connection = self._require_connection()
        async with connection.execute(
            """
            SELECT * FROM discord_resources
            WHERE guild_id = ? AND resource_type = ? AND resource_name = ?
            """,
            (guild_id, resource_type, resource_name),
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def create_class(
        self,
        *,
        guild_id: int,
        class_name: str,
        normalized_name: str,
        category_id: int,
    ) -> int:
        now = _utc_iso(datetime.now(timezone.utc))
        connection = self._require_connection()
        cursor = await connection.execute(
            """
            INSERT INTO classes (
                guild_id, class_name, normalized_name, channel_id, category_id,
                status, created_at, archived_at, updated_at
            ) VALUES (?, ?, ?, NULL, ?, 'active', ?, NULL, ?)
            """,
            (guild_id, class_name, normalized_name, category_id, now, now),
        )
        await connection.commit()
        return int(cursor.lastrowid)

    async def get_class_by_name(
        self, guild_id: int, normalized_name: str
    ) -> dict[str, Any] | None:
        connection = self._require_connection()
        async with connection.execute(
            "SELECT * FROM classes WHERE guild_id = ? AND normalized_name = ?",
            (guild_id, normalized_name),
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def get_class_by_id(
        self, guild_id: int, class_id: int
    ) -> dict[str, Any] | None:
        connection = self._require_connection()
        async with connection.execute(
            "SELECT * FROM classes WHERE guild_id = ? AND id = ?",
            (guild_id, class_id),
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def get_class_by_channel(
        self,
        guild_id: int,
        channel_id: int,
        *,
        active_only: bool = True,
    ) -> dict[str, Any] | None:
        connection = self._require_connection()
        status_clause = " AND status = 'active'" if active_only else ""
        async with connection.execute(
            "SELECT * FROM classes WHERE guild_id = ? AND channel_id = ?" + status_clause,
            (guild_id, channel_id),
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def list_classes(
        self, guild_id: int, *, status: str = "active"
    ) -> list[dict[str, Any]]:
        if status not in {"active", "archived", "all"}:
            raise ValueError("Invalid class status")
        connection = self._require_connection()
        status_clause = "" if status == "all" else "AND status = ?"
        parameters = (guild_id,) if status == "all" else (guild_id, status)
        async with connection.execute(
            f"""
            SELECT * FROM classes WHERE guild_id = ? {status_clause}
            ORDER BY normalized_name
            """,
            parameters,
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def update_class_channel(
        self,
        guild_id: int,
        class_id: int,
        *,
        channel_id: int,
        category_id: int,
    ) -> bool:
        connection = self._require_connection()
        cursor = await connection.execute(
            """
            UPDATE classes SET channel_id = ?, category_id = ?, updated_at = ?
            WHERE guild_id = ? AND id = ?
            """,
            (
                channel_id,
                category_id,
                _utc_iso(datetime.now(timezone.utc)),
                guild_id,
                class_id,
            ),
        )
        await connection.commit()
        return cursor.rowcount > 0

    async def set_class_status(
        self, guild_id: int, class_id: int, status: str
    ) -> bool:
        if status not in {"active", "archived"}:
            raise ValueError("Invalid class status")
        now = _utc_iso(datetime.now(timezone.utc))
        archived_at = now if status == "archived" else None
        connection = self._require_connection()
        cursor = await connection.execute(
            """
            UPDATE classes SET status = ?, archived_at = ?, updated_at = ?
            WHERE guild_id = ? AND id = ?
            """,
            (status, archived_at, now, guild_id, class_id),
        )
        await connection.commit()
        return cursor.rowcount > 0

    async def class_counts(self, class_name: str) -> dict[str, int]:
        connection = self._require_connection()
        async with connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM todos
                    WHERE ybot_class_key(class_name) = ? AND status = 'open') AS open_todos,
                (SELECT COUNT(*) FROM todos
                    WHERE ybot_class_key(class_name) = ? AND status = 'completed') AS completed_todos,
                (SELECT COUNT(*) FROM notes
                    WHERE ybot_class_key(class_name) = ? AND is_archived = 0) AS notes,
                (SELECT COUNT(*) FROM links
                    WHERE ybot_class_key(class_name) = ? AND is_archived = 0) AS links,
                (SELECT COUNT(*) FROM reminders
                    WHERE ybot_class_key(class_name) = ? AND status = 'pending') AS reminders
            """,
            (_class_key(class_name),) * 5,
        ) as cursor:
            row = await cursor.fetchone()
        return {key: int(row[key]) for key in row.keys()}

    async def create_reminder(
        self,
        description: str,
        due_at: datetime,
        class_name: str | None = None,
    ) -> int:
        connection = self._require_connection()
        cursor = await connection.execute(
            """
            INSERT INTO reminders (description, due_at, class_name)
            VALUES (?, ?, ?)
            """,
            (description, _utc_iso(due_at), class_name),
        )
        await connection.commit()
        return int(cursor.lastrowid)

    async def get_reminder(self, reminder_id: int) -> dict[str, Any] | None:
        connection = self._require_connection()
        async with connection.execute(
            "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def list_pending_reminders(
        self,
        *,
        due_from: datetime | None = None,
        due_before: datetime | None = None,
        class_name: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        connection = self._require_connection()
        clauses = ["status = 'pending'"]
        parameters: list[Any] = []
        if due_from is not None:
            clauses.append("due_at >= ?")
            parameters.append(_utc_iso(due_from))
        if due_before is not None:
            clauses.append("due_at < ?")
            parameters.append(_utc_iso(due_before))
        if class_name is not None:
            clauses.append("ybot_class_key(class_name) = ?")
            parameters.append(_class_key(class_name))
        query = (
            "SELECT * FROM reminders WHERE "
            + " AND ".join(clauses)
            + " ORDER BY due_at, id"
        )
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(max(1, min(limit, 100)))
        async with connection.execute(query, parameters) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def complete_reminder(self, reminder_id: int) -> bool:
        return await self._change_reminder_status(reminder_id, "completed")

    async def cancel_reminder(self, reminder_id: int) -> bool:
        return await self._change_reminder_status(reminder_id, "cancelled")

    async def _change_reminder_status(self, reminder_id: int, status: str) -> bool:
        connection = self._require_connection()
        cursor = await connection.execute(
            """
            UPDATE reminders
            SET status = ?, notification_claimed_at = NULL,
                completed_at = CASE WHEN ? = 'completed' THEN ? ELSE completed_at END
            WHERE id = ? AND status IN ('pending', 'notified')
            """,
            (status, status, _utc_iso(datetime.now(timezone.utc)), reminder_id),
        )
        await connection.commit()
        return cursor.rowcount > 0

    async def delete_reminder(self, reminder_id: int) -> bool:
        connection = self._require_connection()
        cursor = await connection.execute(
            "DELETE FROM reminders WHERE id = ?", (reminder_id,)
        )
        await connection.commit()
        return cursor.rowcount > 0

    async def snooze_reminder(self, reminder_id: int, new_due_at: datetime) -> bool:
        connection = self._require_connection()
        cursor = await connection.execute(
            """
            UPDATE reminders
            SET due_at = ?, status = 'pending', notified_at = NULL,
                discord_message_id = NULL, notification_claimed_at = NULL,
                notification_attempts = 0
            WHERE id = ? AND status IN ('pending', 'notified')
            """,
            (_utc_iso(new_due_at), reminder_id),
        )
        await connection.commit()
        return cursor.rowcount > 0

    async def claim_due_reminders(
        self,
        now: datetime,
        *,
        lease_duration: timedelta = timedelta(minutes=5),
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Atomically claim due rows, preventing overlapping scheduler sends."""
        connection = self._require_connection()
        now_iso = _utc_iso(now)
        expired_claim = _utc_iso(now - lease_duration)
        async with connection.execute(
            """
            UPDATE reminders
            SET notification_claimed_at = ?,
                notification_attempts = notification_attempts + 1
            WHERE id IN (
                SELECT id FROM reminders
                WHERE status = 'pending' AND due_at <= ?
                    AND (notification_claimed_at IS NULL
                         OR notification_claimed_at <= ?)
                ORDER BY due_at, id
                LIMIT ?
            ) AND status = 'pending'
            RETURNING *
            """,
            (now_iso, now_iso, expired_claim, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        await connection.commit()
        return [dict(row) for row in sorted(rows, key=lambda row: (row["due_at"], row["id"]))]

    async def release_reminder_claim(self, reminder_id: int) -> None:
        connection = self._require_connection()
        await connection.execute(
            "UPDATE reminders SET notification_claimed_at = NULL WHERE id = ? AND status = 'pending'",
            (reminder_id,),
        )
        await connection.commit()

    async def mark_reminder_notified(
        self, reminder_id: int, notified_at: datetime, discord_message_id: int
    ) -> bool:
        connection = self._require_connection()
        cursor = await connection.execute(
            """
            UPDATE reminders
            SET status = 'notified', notified_at = ?, discord_message_id = ?,
                notification_claimed_at = NULL
            WHERE id = ? AND status = 'pending'
            """,
            (_utc_iso(notified_at), discord_message_id, reminder_id),
        )
        await connection.commit()
        return cursor.rowcount > 0

    async def list_reminders_with_messages(self) -> list[dict[str, Any]]:
        connection = self._require_connection()
        async with connection.execute(
            """
            SELECT * FROM reminders
            WHERE discord_message_id IS NOT NULL
            ORDER BY id
            """
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def create_todo(
        self,
        *,
        title: str,
        description: str | None = None,
        class_name: str | None = None,
        due_at: datetime | None = None,
        priority: str = "normal",
        position: int | None = None,
    ) -> int:
        connection = self._require_connection()
        now = _utc_iso(datetime.now(timezone.utc))
        if position is None:
            async with connection.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM todos WHERE status = 'open'"
            ) as cursor:
                row = await cursor.fetchone()
            position = int(row[0])
        cursor = await connection.execute(
            """
            INSERT INTO todos (
                title, description, class_name, due_at, created_at, updated_at,
                status, priority, position
            ) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)
            """,
            (
                title,
                description,
                class_name,
                _utc_iso(due_at) if due_at is not None else None,
                now,
                now,
                priority,
                position,
            ),
        )
        await connection.commit()
        return int(cursor.lastrowid)

    async def get_todo(self, todo_id: int) -> dict[str, Any] | None:
        connection = self._require_connection()
        async with connection.execute(
            "SELECT * FROM todos WHERE id = ?", (todo_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def list_todos(
        self,
        *,
        status: str = "open",
        limit: int = 25,
        offset: int = 0,
        class_name: str | None = None,
    ) -> list[dict[str, Any]]:
        if status not in {"open", "completed", "archived", "all"}:
            raise ValueError("Invalid to-do status filter")
        if limit < 1 or offset < 0:
            raise ValueError("Invalid to-do page range")
        connection = self._require_connection()
        conditions: list[str] = []
        parameters: list[Any] = []
        if status != "all":
            conditions.append("status = ?")
            parameters.append(status)
        if class_name is not None:
            conditions.append("ybot_class_key(class_name) = ?")
            parameters.append(_class_key(class_name))
        where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""
        parameters.extend((limit, offset))
        async with connection.execute(
            f"""
            SELECT * FROM todos
            {where_clause}
            ORDER BY
                CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,
                CASE WHEN due_at IS NULL THEN 1 ELSE 0 END,
                due_at ASC,
                created_at ASC,
                id ASC
            LIMIT ? OFFSET ?
            """,
            parameters,
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def count_todos(self, status: str | None = None) -> int:
        connection = self._require_connection()
        if status is None:
            query = "SELECT COUNT(*) FROM todos"
            parameters: tuple[Any, ...] = ()
        else:
            if status not in {"open", "completed", "archived"}:
                raise ValueError("Invalid to-do status")
            query = "SELECT COUNT(*) FROM todos WHERE status = ?"
            parameters = (status,)
        async with connection.execute(query, parameters) as cursor:
            row = await cursor.fetchone()
        return int(row[0])

    async def todo_counts(self) -> dict[str, int]:
        connection = self._require_connection()
        async with connection.execute(
            "SELECT status, COUNT(*) AS task_count FROM todos GROUP BY status"
        ) as cursor:
            rows = await cursor.fetchall()
        counts = {"open": 0, "completed": 0, "archived": 0}
        counts.update({row["status"]: int(row["task_count"]) for row in rows})
        return counts

    async def update_todo(self, todo_id: int, **updates: Any) -> bool:
        allowed_fields = {
            "title",
            "description",
            "class_name",
            "due_at",
            "priority",
            "position",
        }
        if not updates or not updates.keys() <= allowed_fields:
            raise ValueError("Invalid to-do update fields")
        values = dict(updates)
        if "due_at" in values:
            values["due_at"] = (
                _utc_iso(values["due_at"]) if values["due_at"] is not None else None
            )
        assignments = ", ".join(f"{field} = ?" for field in values)
        assignments += ", updated_at = ?"
        parameters = [*values.values(), _utc_iso(datetime.now(timezone.utc)), todo_id]
        connection = self._require_connection()
        cursor = await connection.execute(
            f"UPDATE todos SET {assignments} WHERE id = ? AND status != 'archived'",
            parameters,
        )
        await connection.commit()
        return cursor.rowcount > 0

    async def complete_todo(self, todo_id: int) -> bool:
        now = _utc_iso(datetime.now(timezone.utc))
        connection = self._require_connection()
        cursor = await connection.execute(
            """
            UPDATE todos
            SET status = 'completed', completed_at = ?, updated_at = ?
            WHERE id = ? AND status = 'open'
            """,
            (now, now, todo_id),
        )
        await connection.commit()
        return cursor.rowcount > 0

    async def archive_todo(self, todo_id: int) -> bool:
        connection = self._require_connection()
        cursor = await connection.execute(
            """
            UPDATE todos SET status = 'archived', updated_at = ?
            WHERE id = ? AND status != 'archived'
            """,
            (_utc_iso(datetime.now(timezone.utc)), todo_id),
        )
        await connection.commit()
        return cursor.rowcount > 0

    async def archive_open_todos(self) -> int:
        connection = self._require_connection()
        cursor = await connection.execute(
            """
            UPDATE todos SET status = 'archived', updated_at = ?
            WHERE status = 'open'
            """,
            (_utc_iso(datetime.now(timezone.utc)),),
        )
        await connection.commit()
        return cursor.rowcount

    async def get_todo_panel(self, guild_id: int) -> dict[str, Any] | None:
        connection = self._require_connection()
        async with connection.execute(
            "SELECT * FROM todo_panels WHERE guild_id = ?", (guild_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def save_todo_panel(
        self, guild_id: int, channel_id: int, message_id: int
    ) -> None:
        connection = self._require_connection()
        await connection.execute(
            """
            INSERT INTO todo_panels (
                guild_id, channel_id, message_id, page, panel_filter, updated_at
            ) VALUES (?, ?, ?, 0, 'open', ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                channel_id = excluded.channel_id,
                message_id = excluded.message_id,
                updated_at = excluded.updated_at
            """,
            (guild_id, channel_id, message_id, _utc_iso(datetime.now(timezone.utc))),
        )
        await connection.commit()

    async def set_todo_panel_state(
        self,
        guild_id: int,
        *,
        page: int | None = None,
        panel_filter: str | None = None,
    ) -> bool:
        if page is not None and page < 0:
            raise ValueError("Panel page cannot be negative")
        if panel_filter is not None and panel_filter not in {
            "open",
            "completed",
            "archived",
            "all",
        }:
            raise ValueError("Invalid panel filter")
        updates: dict[str, Any] = {}
        if page is not None:
            updates["page"] = page
        if panel_filter is not None:
            updates["panel_filter"] = panel_filter
        if not updates:
            return False
        assignments = ", ".join(f"{field} = ?" for field in updates)
        assignments += ", updated_at = ?"
        connection = self._require_connection()
        cursor = await connection.execute(
            f"UPDATE todo_panels SET {assignments} WHERE guild_id = ?",
            (*updates.values(), _utc_iso(datetime.now(timezone.utc)), guild_id),
        )
        await connection.commit()
        return cursor.rowcount > 0

    async def create_note(
        self,
        *,
        title: str | None,
        content: str,
        class_name: str | None = None,
        tags: str | None = None,
    ) -> int:
        now = _utc_iso(datetime.now(timezone.utc))
        connection = self._require_connection()
        cursor = await connection.execute(
            """
            INSERT INTO notes (title, content, class_name, tags, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (title, content, class_name, tags, now, now),
        )
        await connection.commit()
        return int(cursor.lastrowid)

    async def get_note(self, note_id: int) -> dict[str, Any] | None:
        connection = self._require_connection()
        async with connection.execute(
            "SELECT * FROM notes WHERE id = ?", (note_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def list_notes(
        self, *, limit: int = 20, class_name: str | None = None
    ) -> list[dict[str, Any]]:
        connection = self._require_connection()
        where_clause = "is_archived = 0"
        parameters: list[Any] = []
        if class_name is not None:
            where_clause += " AND ybot_class_key(class_name) = ?"
            parameters.append(_class_key(class_name))
        parameters.append(max(1, min(limit, 100)))
        async with connection.execute(
            f"""
            SELECT * FROM notes WHERE {where_clause}
            ORDER BY updated_at DESC, id DESC LIMIT ?
            """,
            parameters,
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def update_note(
        self,
        note_id: int,
        *,
        title: str | None,
        content: str,
        class_name: str | None,
        tags: str | None,
    ) -> bool:
        connection = self._require_connection()
        now = _utc_iso(datetime.now(timezone.utc))
        cursor = await connection.execute(
            """
            UPDATE notes SET title = ?, content = ?, class_name = ?, tags = ?, updated_at = ?
            WHERE id = ? AND is_archived = 0
            """,
            (title, content, class_name, tags, now, note_id),
        )
        await connection.commit()
        return cursor.rowcount > 0

    async def archive_note(self, note_id: int) -> bool:
        connection = self._require_connection()
        cursor = await connection.execute(
            """
            UPDATE notes SET is_archived = 1, updated_at = ?
            WHERE id = ? AND is_archived = 0
            """,
            (_utc_iso(datetime.now(timezone.utc)), note_id),
        )
        await connection.commit()
        return cursor.rowcount > 0

    async def create_link(
        self,
        *,
        url: str,
        normalized_url: str,
        title: str,
        description: str | None = None,
        extracted_text: str | None = None,
        class_name: str | None = None,
        tags: str | None = None,
        extraction_status: str = "pending",
    ) -> int:
        now = _utc_iso(datetime.now(timezone.utc))
        connection = self._require_connection()
        cursor = await connection.execute(
            """
            INSERT INTO links (
                url, normalized_url, title, description, extracted_text, class_name,
                tags, saved_at, updated_at, extraction_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                url,
                normalized_url,
                title,
                description,
                extracted_text,
                class_name,
                tags,
                now,
                now,
                extraction_status,
            ),
        )
        await connection.commit()
        return int(cursor.lastrowid)

    async def get_link(self, link_id: int) -> dict[str, Any] | None:
        connection = self._require_connection()
        async with connection.execute(
            "SELECT * FROM links WHERE id = ?", (link_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def list_links(
        self, *, limit: int = 20, class_name: str | None = None
    ) -> list[dict[str, Any]]:
        connection = self._require_connection()
        where_clause = "is_archived = 0"
        parameters: list[Any] = []
        if class_name is not None:
            where_clause += " AND ybot_class_key(class_name) = ?"
            parameters.append(_class_key(class_name))
        parameters.append(max(1, min(limit, 100)))
        async with connection.execute(
            f"""
            SELECT * FROM links WHERE {where_clause}
            ORDER BY saved_at DESC, id DESC LIMIT ?
            """,
            parameters,
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def update_link_metadata(
        self,
        link_id: int,
        *,
        normalized_url: str,
        title: str,
        description: str | None,
        extracted_text: str | None,
        extraction_status: str,
    ) -> bool:
        connection = self._require_connection()
        cursor = await connection.execute(
            """
            UPDATE links SET normalized_url = ?, title = ?, description = ?,
                extracted_text = ?, extraction_status = ?, updated_at = ?
            WHERE id = ? AND is_archived = 0
            """,
            (
                normalized_url,
                title,
                description,
                extracted_text,
                extraction_status,
                _utc_iso(datetime.now(timezone.utc)),
                link_id,
            ),
        )
        await connection.commit()
        return cursor.rowcount > 0

    async def update_link(self, link_id: int, **updates: Any) -> bool:
        allowed_fields = {
            "url",
            "normalized_url",
            "title",
            "description",
            "extracted_text",
            "class_name",
            "tags",
            "extraction_status",
        }
        if not updates or not updates.keys() <= allowed_fields:
            raise ValueError("Invalid saved-link update fields")
        assignments = ", ".join(f"{field} = ?" for field in updates)
        assignments += ", updated_at = ?"
        connection = self._require_connection()
        cursor = await connection.execute(
            f"UPDATE links SET {assignments} WHERE id = ? AND is_archived = 0",
            (*updates.values(), _utc_iso(datetime.now(timezone.utc)), link_id),
        )
        await connection.commit()
        return cursor.rowcount > 0

    async def set_link_discord_message_id(
        self, link_id: int, message_id: int
    ) -> bool:
        connection = self._require_connection()
        cursor = await connection.execute(
            "UPDATE links SET discord_message_id = ?, updated_at = ? WHERE id = ?",
            (message_id, _utc_iso(datetime.now(timezone.utc)), link_id),
        )
        await connection.commit()
        return cursor.rowcount > 0

    async def archive_link(self, link_id: int) -> bool:
        connection = self._require_connection()
        cursor = await connection.execute(
            """
            UPDATE links SET is_archived = 1, updated_at = ?
            WHERE id = ? AND is_archived = 0
            """,
            (_utc_iso(datetime.now(timezone.utc)), link_id),
        )
        await connection.commit()
        return cursor.rowcount > 0

    async def search_notes(
        self, query: str, *, class_name: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        return await self._search_knowledge(
            "notes", query, class_name=class_name, limit=limit
        )

    async def search_links(
        self, query: str, *, class_name: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        return await self._search_knowledge(
            "links", query, class_name=class_name, limit=limit
        )

    async def search_todos(
        self, query: str, *, class_name: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        return await self._search_knowledge(
            "todos", query, class_name=class_name, limit=limit
        )

    async def _search_knowledge(
        self,
        resource_type: str,
        query: str,
        *,
        class_name: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        tokens = _search_tokens(query)
        if not tokens:
            return []
        limit = max(1, min(limit, 1000))
        connection = self._require_connection()
        if self._fts5_available:
            try:
                fts_rows = await self._search_fts(
                    connection, resource_type, tokens, class_name, limit
                )
                # FTS prefix matching is fast; LIKE supplements it for infix partials.
                like_rows = await self._search_like(
                    connection, resource_type, tokens, class_name, limit
                )
                unique_rows = {int(row["id"]): row for row in (*fts_rows, *like_rows)}
                date_field = {
                    "notes": "created_at",
                    "links": "saved_at",
                    "todos": "created_at",
                }[resource_type]
                return sorted(
                    unique_rows.values(),
                    key=lambda row: (row[date_field], row["id"]),
                    reverse=True,
                )[:limit]
            except aiosqlite.Error as exc:
                logger.info(
                    "FTS5 query unavailable; using LIKE search (%s)",
                    type(exc).__name__,
                )
                self._fts5_available = False
        return await self._search_like(
            connection, resource_type, tokens, class_name, limit
        )

    async def _search_fts(
        self,
        connection: aiosqlite.Connection,
        resource_type: str,
        tokens: list[str],
        class_name: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        fts_table = f"{resource_type}_fts"
        expression = " AND ".join(f'"{token}"*' for token in tokens)
        if resource_type == "notes":
            select = "SELECT n.*"
            join = "JOIN notes AS n ON n.id = notes_fts.rowid"
            active = "n.is_archived = 0"
            class_column = "n.class_name"
            order = "n.created_at DESC"
        elif resource_type == "links":
            select = "SELECT l.*"
            join = "JOIN links AS l ON l.id = links_fts.rowid"
            active = "l.is_archived = 0"
            class_column = "l.class_name"
            order = "l.saved_at DESC"
        elif resource_type == "todos":
            select = "SELECT t.*"
            join = "JOIN todos AS t ON t.id = todos_fts.rowid"
            active = "t.status IN ('open', 'completed')"
            class_column = "t.class_name"
            order = "t.created_at DESC"
        else:
            raise ValueError("Unknown search resource type")
        class_clause = ""
        parameters: list[Any] = [expression]
        if class_name:
            class_clause = f" AND ybot_class_key({class_column}) = ?"
            parameters.append(_class_key(class_name))
        parameters.append(limit)
        async with connection.execute(
            f"""
            {select} FROM {fts_table} {join}
            WHERE {fts_table} MATCH ? AND {active}{class_clause}
            ORDER BY {order} LIMIT ?
            """,
            parameters,
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def _search_like(
        self,
        connection: aiosqlite.Connection,
        resource_type: str,
        tokens: list[str],
        class_name: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        if resource_type == "notes":
            table, alias = "notes", "n"
            fields = ("title", "content", "class_name", "tags")
            active = "n.is_archived = 0"
            order = "n.created_at DESC"
            class_column = "n.class_name"
        elif resource_type == "links":
            table, alias = "links", "l"
            fields = (
                "url", "normalized_url", "title", "description",
                "extracted_text", "class_name", "tags",
            )
            active = "l.is_archived = 0"
            order = "l.saved_at DESC"
            class_column = "l.class_name"
        elif resource_type == "todos":
            table, alias = "todos", "t"
            fields = ("title", "description", "class_name")
            active = "t.status IN ('open', 'completed')"
            order = "t.created_at DESC"
            class_column = "t.class_name"
        else:
            raise ValueError("Unknown search resource type")

        text_clause, parameters = _like_search_clause(alias, fields, tokens)
        class_clause = ""
        if class_name:
            class_clause = f" AND ybot_class_key({class_column}) = ?"
            parameters.append(_class_key(class_name))
        parameters.append(limit)
        async with connection.execute(
            f"""
            SELECT {alias}.* FROM {table} AS {alias}
            WHERE {active} AND {text_clause}{class_clause}
            ORDER BY {order} LIMIT ?
            """,
            parameters,
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    def _require_connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("Database has not been initialized")
        return self._connection


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Datetime values must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _class_key(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(character.casefold() for character in value if character.isalnum())


def _search_tokens(query: str) -> list[str]:
    return list(dict.fromkeys(token.casefold() for token in re.findall(r"[^\W_]+", query)))[:12]


def _like_search_clause(
    alias: str, fields: tuple[str, ...], tokens: list[str]
) -> tuple[str, list[str]]:
    token_clauses: list[str] = []
    parameters: list[str] = []
    for token in tokens:
        alternatives = " OR ".join(
            f"LOWER(COALESCE({alias}.{field}, '')) LIKE ?" for field in fields
        )
        token_clauses.append(f"({alternatives})")
        parameters.extend([f"%{token}%"] * len(fields))
    return " AND ".join(token_clauses), parameters
