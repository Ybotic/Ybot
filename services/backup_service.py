"""Online SQLite snapshots with bounded local retention."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from config import Config
from database.db import Database

logger = logging.getLogger(__name__)


class BackupService:
    def __init__(self, database: Database, config: Config) -> None:
        self.database = database
        self.config = config
        self._lock = asyncio.Lock()

    async def snapshot(self) -> Path:
        async with self._lock:
            directory = Path(self.config.backup_dir).expanduser().resolve()
            await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True, mode=0o700)
            filename = f"ybot-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}.sqlite3"
            target = directory / filename
            await write_snapshot(self.database._require_connection(), target)
            await asyncio.to_thread(self._rotate, directory)
            logger.info("SQLite backup completed")
            return target

    def _rotate(self, directory: Path) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.config.backup_retention_days)
        for path in directory.glob("ybot-*.sqlite3"):
            if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < cutoff:
                path.unlink()


async def write_snapshot(source: aiosqlite.Connection, target: Path) -> None:
    if target.exists():
        raise FileExistsError("The backup destination already exists; choose a new filename")
    await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True, mode=0o700)
    temporary = target.with_name(target.name + ".partial")
    destination = await aiosqlite.connect(temporary)
    try:
        await source.backup(destination, pages=128, sleep=0.05)
    finally:
        await destination.close()
    await asyncio.to_thread(os.chmod, temporary, 0o600)
    await asyncio.to_thread(os.replace, temporary, target)
