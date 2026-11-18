from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from config import Config
from database.db import Database
from services.reminder_scheduler import ReminderScheduler


class FakeChannel:
    def __init__(self) -> None:
        self.name = "reminders"
        self.id = 4567
        self.category = SimpleNamespace(name="PLANNER")
        self.sent = []
        self.next_message_id = 7000

    async def send(self, content, *, allowed_mentions, view):
        self.sent.append((content, allowed_mentions, view))
        self.next_message_id += 1
        return SimpleNamespace(id=self.next_message_id)


class FakeGuild:
    def __init__(self, channel) -> None:
        self.text_channels = [channel]

    def get_channel(self, channel_id):
        return next(
            (channel for channel in self.text_channels if channel.id == channel_id), None
        )


class FakeClient:
    def __init__(self, guild) -> None:
        self.guild = guild
        self.views = []

    def get_guild(self, guild_id):
        return self.guild if guild_id == 456 else None

    def add_view(self, view, *, message_id):
        self.views.append((view, message_id))


@pytest.mark.asyncio
async def test_scheduler_recovers_and_delivers_due_reminder_once_after_restart(tmp_path) -> None:
    path = tmp_path / "ybot.sqlite3"
    first_db = Database(path)
    await first_db.initialize()
    due_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    reminder_id = await first_db.create_reminder("Recover after restart", due_at)
    await first_db.close()

    database = Database(path)
    await database.initialize()
    channel = FakeChannel()
    client = FakeClient(FakeGuild(channel))
    config = Config("not-used", owner_id=123, guild_id=456)
    scheduler = ReminderScheduler(client, config, database)

    # start() performs an immediate recovery sweep before waiting for the next interval.
    await scheduler.start()
    reminder = await database.get_reminder(reminder_id)
    assert reminder["status"] == "notified"
    assert reminder["discord_message_id"] == 7001
    assert len(channel.sent) == 1
    content, allowed_mentions, view = channel.sent[0]
    assert "<@123>" in content
    assert "REMINDER DUE" in content
    assert "Recover after restart" in content
    assert allowed_mentions.users[0].id == 123
    assert view.timeout is None
    await scheduler.restore_persistent_views()
    assert len(client.views) == 1
    assert client.views[0][1] == 7001

    # A second sweep or simulated restart cannot deliver a notified reminder again.
    assert await scheduler.run_due_reminders() == 0
    new_scheduler = ReminderScheduler(client, config, database)
    assert await new_scheduler.run_due_reminders() == 0
    assert len(channel.sent) == 1
    await scheduler.stop()
    await database.close()


@pytest.mark.asyncio
async def test_scheduler_retries_after_discord_delivery_failure(tmp_path) -> None:
    database = Database(tmp_path / "ybot.sqlite3")
    await database.initialize()
    due_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    reminder_id = await database.create_reminder("Retry this", due_at)
    channel = FakeChannel()
    client = FakeClient(FakeGuild(channel))
    scheduler = ReminderScheduler(
        client, Config("not-used", owner_id=123, guild_id=456), database
    )
    original_send = channel.send
    fail_once = True

    async def send_once_fails(content, *, allowed_mentions, view):
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise RuntimeError("temporary Discord failure")
        return await original_send(content, allowed_mentions=allowed_mentions, view=view)

    channel.send = send_once_fails
    assert await scheduler.run_due_reminders() == 0
    reminder = await database.get_reminder(reminder_id)
    assert reminder["status"] == "pending"
    assert reminder["notification_claimed_at"] is None

    assert await scheduler.run_due_reminders() == 1
    assert (await database.get_reminder(reminder_id))["status"] == "notified"
    assert len(channel.sent) == 1
    await database.close()
