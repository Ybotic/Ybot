"""Restart-safe digest delivery and local backup jobs using APScheduler."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import Config
from database.activity import ActivityRepository
from database.db import Database
from services.backup_service import BackupService
from services.digest_service import DigestService

logger = logging.getLogger(__name__)


class BackgroundServices:
    def __init__(self, client: discord.Client, database: Database, config: Config) -> None:
        self.client = client
        self.database = database
        self.config = config
        self.repository = ActivityRepository(database)
        self.digests = DigestService(self.repository, config)
        self.backups = BackupService(database, config)
        self.scheduler: AsyncIOScheduler | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self.scheduler is not None:
            return
        self.scheduler = AsyncIOScheduler(timezone=ZoneInfo(self.config.timezone_name))
        defaults = {"replace_existing": True, "max_instances": 1, "coalesce": True, "misfire_grace_time": 3600}
        self.scheduler.add_job(self.deliver_due, "cron", hour=8, minute=0, id="ybot-daily-digest", **defaults)
        self.scheduler.add_job(self.deliver_due, "cron", day_of_week="sun", hour=19, minute=0, id="ybot-weekly-review", **defaults)
        self.scheduler.add_job(self.deliver_due, "interval", minutes=1, id="ybot-digest-recovery", **defaults)
        self.scheduler.add_job(self.backups.snapshot, "cron", hour=3, minute=0, id="ybot-sqlite-backup", **defaults)
        self.scheduler.start()
        logger.info("Digest, weekly review, recovery, and backup scheduler started")
        try:
            await self.backups.snapshot()
        except Exception as exc:
            logger.error("Startup backup failed (%s)", type(exc).__name__)
        await self.deliver_due()

    async def stop(self) -> None:
        if self.scheduler is not None:
            self.scheduler.pause()
            async with self._lock:
                async with self.backups._lock:
                    self.scheduler.shutdown(wait=False)
                    self.scheduler = None
            await asyncio.sleep(0)

    async def deliver_due(self) -> None:
        async with self._lock:
            now = datetime.now(timezone.utc)
            zone = ZoneInfo(self.config.timezone_name)
            local_now = now.astimezone(zone)
            daily = datetime.combine(local_now.date(), time(8), zone)
            if daily > local_now:
                daily -= timedelta(days=1)
            sunday = local_now.date() - timedelta(days=(local_now.weekday() + 1) % 7)
            weekly = datetime.combine(sunday, time(19), zone)
            if weekly > local_now:
                weekly -= timedelta(days=7)
            for kind, scheduled in (("daily", daily), ("weekly", weekly)):
                try:
                    await self._deliver(kind, scheduled)
                except Exception as exc:
                    logger.error("Scheduled %s delivery failed (%s); recovery will retry", kind, type(exc).__name__)

    async def _deliver(self, kind: str, scheduled: datetime) -> None:
        slot = scheduled.isoformat()
        claimed = await self.repository.claim_digest(self.config.guild_id, kind, slot)
        if not claimed:
            return
        sent = False
        try:
            guild = self.client.get_guild(self.config.guild_id)
            resource = await self.database.get_resource(self.config.guild_id, "text_channel", "digest")
            channel = guild.get_channel(resource["resource_id"]) if guild and resource else None
            if not isinstance(channel, discord.TextChannel):
                logger.error("Cannot deliver digest: #digest is missing. Restart YBOT with bootstrap permissions.")
                return
            # Footer reconciliation covers a restart after send but before recording success.
            # Fail closed if history cannot be read instead of risking duplicate delivery.
            marker = f"YBOT:{kind}:{slot}"
            async for message in channel.history(limit=100):
                if self.client.user and message.author.id == self.client.user.id and any(
                    embed.footer.text == marker for embed in message.embeds
                ):
                    await self.repository.finish_digest(self.config.guild_id, kind, slot, message.id)
                    sent = True
                    return
            overview = await self.digests.generate(kind, now=scheduled)
            message = await channel.send(
                embeds=overview.embeds(marker), allowed_mentions=discord.AllowedMentions.none()
            )
            # Leave the lease intact on DB failure after Discord accepted the message.
            sent = True
            await self.repository.finish_digest(self.config.guild_id, kind, slot, message.id)
            logger.info("Delivered scheduled %s report", kind)
        finally:
            if not sent:
                await self.repository.release_digest(self.config.guild_id, kind, slot)
