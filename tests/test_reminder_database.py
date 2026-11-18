from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

from database.db import Database
from services.reminder_format import parse_utc_timestamp


@pytest.mark.asyncio
async def test_reminders_persist_across_database_reopen(tmp_path) -> None:
    path = tmp_path / "ybot.sqlite3"
    database = Database(path)
    await database.initialize()
    due_at = datetime.now(timezone.utc) + timedelta(hours=2)
    reminder_id = await database.create_reminder("Submit lab", due_at, "CMSC216")
    await database.close()

    reopened = Database(path)
    await reopened.initialize()
    reminder = await reopened.get_reminder(reminder_id)

    assert reminder is not None
    assert reminder["description"] == "Submit lab"
    assert reminder["class_name"] == "CMSC216"
    assert reminder["status"] == "pending"
    assert parse_utc_timestamp(reminder["due_at"]) == due_at.replace(microsecond=due_at.microsecond)
    await reopened.close()


@pytest.mark.asyncio
async def test_reminder_completion_cancellation_snooze_and_delete(tmp_path) -> None:
    database = Database(tmp_path / "ybot.sqlite3")
    await database.initialize()
    now = datetime.now(timezone.utc)
    completed_id = await database.create_reminder("Complete me", now + timedelta(hours=1))
    cancelled_id = await database.create_reminder("Cancel me", now + timedelta(hours=2))
    snoozed_id = await database.create_reminder("Snooze me", now + timedelta(hours=3))
    deleted_id = await database.create_reminder("Delete me", now + timedelta(hours=4))

    assert await database.complete_reminder(completed_id)
    assert (await database.get_reminder(completed_id))["status"] == "completed"
    assert await database.cancel_reminder(cancelled_id)
    assert (await database.get_reminder(cancelled_id))["status"] == "cancelled"
    assert await database.snooze_reminder(snoozed_id, now + timedelta(days=1))
    snoozed = await database.get_reminder(snoozed_id)
    assert snoozed["status"] == "pending"
    assert snoozed["notified_at"] is None
    assert await database.delete_reminder(deleted_id)
    assert await database.get_reminder(deleted_id) is None
    await database.close()


@pytest.mark.asyncio
async def test_due_claims_prevent_duplicate_notifications_and_retry_after_release(tmp_path) -> None:
    database = Database(tmp_path / "ybot.sqlite3")
    await database.initialize()
    now = datetime.now(timezone.utc)
    reminder_id = await database.create_reminder("Due now", now - timedelta(seconds=1))

    first_claim = await database.claim_due_reminders(now)
    second_claim = await database.claim_due_reminders(now)
    assert [row["id"] for row in first_claim] == [reminder_id]
    assert second_claim == []

    await database.release_reminder_claim(reminder_id)
    retried_claim = await database.claim_due_reminders(now)
    assert [row["id"] for row in retried_claim] == [reminder_id]
    assert await database.mark_reminder_notified(reminder_id, now, 9001)
    assert await database.claim_due_reminders(now) == []
    reminder = await database.get_reminder(reminder_id)
    assert reminder["status"] == "notified"
    assert reminder["discord_message_id"] == 9001
    await database.close()


@pytest.mark.asyncio
async def test_schema_addition_preserves_existing_phase_one_data(tmp_path) -> None:
    path = tmp_path / "ybot.sqlite3"
    legacy = await aiosqlite.connect(path)
    await legacy.executescript(
        """
        CREATE TABLE server_config (
            guild_id INTEGER PRIMARY KEY,
            setup_completed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE discord_resources (
            resource_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            resource_type TEXT NOT NULL,
            resource_name TEXT NOT NULL,
            parent_category_id INTEGER,
            UNIQUE (guild_id, resource_type, resource_name)
        );
        INSERT INTO server_config (guild_id, setup_completed) VALUES (456, 1);
        INSERT INTO discord_resources (
            resource_id, guild_id, resource_type, resource_name
        ) VALUES (1234, 456, 'category', 'PLANNER');
        """
    )
    await legacy.close()

    upgraded = Database(path)
    await upgraded.initialize()
    assert (await upgraded.get_server_config(456))["setup_completed"] == 1
    assert (await upgraded.get_resource(456, "category", "PLANNER"))["resource_id"] == 1234
    assert await upgraded.ping()
    await upgraded.close()
