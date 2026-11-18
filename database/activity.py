"""Persistence for scheduled deliveries, assistant context, and confirmations."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from database.db import Database, _utc_iso


class ActivityRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def rows(self, query: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        async with self.database._require_connection().execute(query, parameters) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def overview_data(self) -> dict[str, list[dict[str, Any]]]:
        # Queries are application-owned constants, never model-provided SQL.
        return {
            "todos": await self.rows("SELECT * FROM todos WHERE status != 'archived' ORDER BY created_at DESC"),
            "reminders": await self.rows("SELECT * FROM reminders ORDER BY due_at"),
            "notes": await self.rows("SELECT id, title, class_name, created_at FROM notes WHERE is_archived = 0 ORDER BY created_at DESC"),
            "links": await self.rows("SELECT id, title, class_name, saved_at FROM links WHERE is_archived = 0 ORDER BY saved_at DESC"),
        }

    async def claim_digest(self, guild_id: int, kind: str, slot: str) -> bool:
        now = datetime.now(timezone.utc)
        connection = self.database._require_connection()
        async with connection.execute(
            """
            INSERT INTO digest_runs (guild_id, kind, slot, status, claimed_at)
            VALUES (?, ?, ?, 'sending', ?)
            ON CONFLICT(guild_id, kind, slot) DO UPDATE SET
                status = 'sending', claimed_at = excluded.claimed_at
            WHERE digest_runs.status != 'sent'
                AND (digest_runs.claimed_at IS NULL OR digest_runs.claimed_at < ?)
            RETURNING slot
            """,
            (guild_id, kind, slot, _utc_iso(now), _utc_iso(now - timedelta(minutes=10))),
        ) as cursor:
            claimed = await cursor.fetchone() is not None
        await connection.commit()
        return claimed

    async def finish_digest(self, guild_id: int, kind: str, slot: str, message_id: int) -> None:
        connection = self.database._require_connection()
        await connection.execute(
            """UPDATE digest_runs SET status = 'sent', sent_at = ?, discord_message_id = ?
            WHERE guild_id = ? AND kind = ? AND slot = ?""",
            (_utc_iso(datetime.now(timezone.utc)), message_id, guild_id, kind, slot),
        )
        await connection.commit()

    async def release_digest(self, guild_id: int, kind: str, slot: str) -> None:
        connection = self.database._require_connection()
        await connection.execute(
            """UPDATE digest_runs SET status = 'pending', claimed_at = NULL
            WHERE guild_id = ? AND kind = ? AND slot = ? AND status != 'sent'""",
            (guild_id, kind, slot),
        )
        await connection.commit()

    async def context(self, guild_id: int, channel_id: int, owner_id: int) -> list[dict[str, str]]:
        rows = await self.rows(
            """SELECT role, content FROM conversation_history
            WHERE guild_id = ? AND channel_id = ? AND owner_id = ? ORDER BY id DESC LIMIT 40""",
            (guild_id, channel_id, owner_id),
        )
        return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]

    async def save_turn(self, guild_id: int, channel_id: int, owner_id: int, prompt: str, reply: str) -> None:
        connection = self.database._require_connection()
        now = _utc_iso(datetime.now(timezone.utc))
        await connection.execute(
            """INSERT INTO conversation_history (guild_id, channel_id, owner_id, role, content, created_at)
            VALUES (?, ?, ?, 'user', ?, ?), (?, ?, ?, 'assistant', ?, ?)""",
            (guild_id, channel_id, owner_id, prompt[:4000], now,
             guild_id, channel_id, owner_id, reply[:4000], now),
        )
        await connection.execute(
            """DELETE FROM conversation_history
            WHERE guild_id = ? AND channel_id = ? AND owner_id = ? AND id NOT IN (
                SELECT id FROM conversation_history WHERE guild_id = ? AND channel_id = ? AND owner_id = ?
                ORDER BY id DESC LIMIT 40)""",
            (guild_id, channel_id, owner_id, guild_id, channel_id, owner_id),
        )
        await connection.commit()

    async def create_action(
        self, guild_id: int, channel_id: int, owner_id: int, tool_name: str, arguments: dict[str, Any]
    ) -> int:
        now = datetime.now(timezone.utc)
        connection = self.database._require_connection()
        cursor = await connection.execute(
            """INSERT INTO assistant_actions
            (guild_id, channel_id, owner_id, tool_name, arguments, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (guild_id, channel_id, owner_id, tool_name, json.dumps(arguments),
             _utc_iso(now), _utc_iso(now + timedelta(minutes=15))),
        )
        await connection.commit()
        return int(cursor.lastrowid)

    async def action_message(self, action_id: int, message_id: int) -> None:
        connection = self.database._require_connection()
        await connection.execute(
            "UPDATE assistant_actions SET discord_message_id = ? WHERE id = ?", (message_id, action_id)
        )
        await connection.commit()

    async def pending_actions(self) -> list[dict[str, Any]]:
        return await self.rows(
            "SELECT * FROM assistant_actions WHERE status = 'pending' AND expires_at > ?",
            (_utc_iso(datetime.now(timezone.utc)),),
        )

    async def claim_action(
        self, action_id: int, guild_id: int, channel_id: int, owner_id: int,
        message_id: int, *, cancel: bool = False,
    ) -> dict[str, Any] | None:
        connection = self.database._require_connection()
        async with connection.execute(
            """UPDATE assistant_actions SET status = ?
            WHERE id = ? AND guild_id = ? AND channel_id = ? AND owner_id = ?
                AND discord_message_id = ? AND status = 'pending' AND expires_at > ?
            RETURNING *""",
            ("cancelled" if cancel else "executing", action_id, guild_id, channel_id, owner_id,
             message_id, _utc_iso(datetime.now(timezone.utc))),
        ) as cursor:
            row = await cursor.fetchone()
        await connection.commit()
        return dict(row) if row else None

    async def finish_action(self, action_id: int, *, succeeded: bool) -> None:
        connection = self.database._require_connection()
        await connection.execute(
            "UPDATE assistant_actions SET status = ? WHERE id = ? AND status = 'executing'",
            ("completed" if succeeded else "failed", action_id),
        )
        await connection.commit()
