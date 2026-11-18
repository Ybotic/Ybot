"""APScheduler-backed delivery service for persisted reminders."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from commands.reminder_view import ReminderActionView
from config import Config
from database.db import Database
from services.reminder_format import format_local_due

logger = logging.getLogger(__name__)


class ReminderScheduler:
    """Poll due reminders and deliver each claimed reminder to #reminders."""

    def __init__(
        self,
        client: Any,
        config: Config,
        database: Database,
        *,
        interval_seconds: int = 15,
    ) -> None:
        self.client = client
        self.config = config
        self.database = database
        self.interval_seconds = interval_seconds
        self._scheduler: AsyncIOScheduler | None = None
        self._dispatch_lock = asyncio.Lock()
        self._missing_channel_logged = False

    async def start(self) -> None:
        if self._scheduler is not None:
            return
        logger.info("Starting reminder scheduler")
        self._scheduler = AsyncIOScheduler(timezone=timezone.utc)
        self._scheduler.add_job(
            self.run_due_reminders,
            "interval",
            seconds=self.interval_seconds,
            id="ybot-reminder-delivery",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=self.interval_seconds * 4,
        )
        self._scheduler.start()
        # Process reminders that became due while YBOT was offline immediately.
        await self.run_due_reminders()

    async def stop(self) -> None:
        if self._scheduler is None:
            return
        self._scheduler.shutdown(wait=False)
        self._scheduler = None
        # Do not close SQLite under a delivery callback already in progress.
        async with self._dispatch_lock:
            pass
        logger.info("Reminder scheduler stopped")

    async def run_due_reminders(self, *, now: datetime | None = None) -> int:
        """Attempt delivery of currently due reminders; safe for test invocation."""
        async with self._dispatch_lock:
            channel = await self._get_reminders_channel()
            if channel is None:
                return 0

            current_time = now or datetime.now(timezone.utc)
            claimed = await self.database.claim_due_reminders(current_time)
            delivered = 0
            for reminder in claimed:
                try:
                    message = await self._send_reminder(channel, reminder)
                    if message is None or not await self.database.mark_reminder_notified(
                        reminder["id"], datetime.now(timezone.utc), message.id
                    ):
                        logger.error(
                            "Reminder ID %s was sent but its delivered state could not be saved",
                            reminder["id"],
                        )
                        continue
                    delivered += 1
                    logger.info("Delivered reminder ID %s", reminder["id"])
                except Exception:
                    logger.exception("Failed to deliver reminder ID %s", reminder["id"])
                    try:
                        await self.database.release_reminder_claim(reminder["id"])
                    except Exception:
                        logger.exception(
                            "Could not release delivery claim for reminder ID %s",
                            reminder["id"],
                        )
            return delivered

    async def restore_persistent_views(self) -> None:
        """Re-register message-scoped controls after a process restart."""
        rows = await self.database.list_reminders_with_messages()
        registered = 0
        for reminder in rows:
            try:
                self.client.add_view(
                    ReminderActionView(
                        self.database,
                        self.config.owner_id,
                        self.config.guild_id,
                        self.config.timezone_name,
                        reminder["id"],
                    ),
                    message_id=reminder["discord_message_id"],
                )
                registered += 1
            except Exception:
                logger.exception(
                    "Failed to restore controls for reminder ID %s", reminder["id"]
                )
        logger.info("Restored %s persistent reminder control view(s)", registered)

    async def _get_reminders_channel(self) -> Any | None:
        guild = self.client.get_guild(self.config.guild_id)
        if guild is None:
            logger.error(
                "Cannot deliver reminders: configured guild ID %s is unavailable",
                self.config.guild_id,
            )
            return None

        resource = await self.database.get_resource(
            self.config.guild_id, "text_channel", "reminders"
        )
        channel = None
        if resource is not None:
            channel = guild.get_channel(resource["resource_id"])
        if channel is None:
            channel = next(
                (
                    candidate
                    for candidate in getattr(guild, "text_channels", ())
                    if candidate.name == "reminders"
                    and getattr(getattr(candidate, "category", None), "name", "PLANNER")
                    == "PLANNER"
                ),
                None,
            )
        if channel is None:
            if not self._missing_channel_logged:
                logger.error(
                    "Cannot deliver reminders: #reminders is missing from the configured "
                    "guild. Create it under PLANNER or restart YBOT to run server bootstrap."
                )
                self._missing_channel_logged = True
            return None
        self._missing_channel_logged = False
        return channel

    async def _send_reminder(self, channel: Any, reminder: dict[str, Any]) -> Any:
        due_text = format_local_due(
            reminder["due_at"], self.config.timezone_name
        )
        description = discord.utils.escape_mentions(reminder["description"])
        class_text = (
            f"\nClass: {discord.utils.escape_mentions(reminder['class_name'])}"
            if reminder.get("class_name")
            else ""
        )
        content = (
            f"<@{self.config.owner_id}>\n"
            "**REMINDER DUE**\n\n"
            f"{description}{class_text}\n\n"
            f"*Originally scheduled for {due_text}.*"
        )
        allowed_mentions = discord.AllowedMentions(
            users=[discord.Object(id=self.config.owner_id)],
            roles=False,
            everyone=False,
            replied_user=False,
        )
        view = ReminderActionView(
            self.database,
            self.config.owner_id,
            self.config.guild_id,
            self.config.timezone_name,
            reminder["id"],
        )
        return await channel.send(
            content,
            allowed_mentions=allowed_mentions,
            view=view,
        )
