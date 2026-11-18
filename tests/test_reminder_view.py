from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import discord
import pytest

from commands.reminder_view import ReminderActionView
from database.db import Database


class FakeResponse:
    def __init__(self) -> None:
        self.sent = []

    async def send_message(self, content, *, ephemeral=False):
        self.sent.append((content, ephemeral))


class FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id
        self.edited_view = "unchanged"

    async def edit(self, *, view):
        self.edited_view = view


def interaction(user_id: int, message_id: int):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        guild_id=456,
        message=FakeMessage(message_id),
        response=FakeResponse(),
    )


@pytest.mark.asyncio
async def test_reminder_controls_are_owner_only_and_complete_without_deleting(tmp_path) -> None:
    database = Database(tmp_path / "ybot.sqlite3")
    await database.initialize()
    now = datetime.now(timezone.utc)
    reminder_id = await database.create_reminder("Test reminder", now - timedelta(minutes=1))
    assert [row["id"] for row in await database.claim_due_reminders(now)] == [reminder_id]
    assert await database.mark_reminder_notified(reminder_id, now, 7001)
    view = ReminderActionView(database, 123, 456, "America/New_York", reminder_id)
    complete = next(
        child for child in view.children if child.custom_id == "ybot:reminder:complete"
    )

    unauthorized = interaction(999, 7001)
    await complete.callback(unauthorized)
    assert "Only the configured owner" in unauthorized.response.sent[0][0]
    assert (await database.get_reminder(reminder_id))["status"] == "notified"

    owner = interaction(123, 7001)
    await complete.callback(owner)
    saved = await database.get_reminder(reminder_id)
    assert saved["status"] == "completed"
    assert saved["description"] == "Test reminder"
    assert owner.response.sent == [("Reminder marked complete.", True)]
    assert owner.message.edited_view is None
    await database.close()
