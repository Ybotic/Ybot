"""Manual online SQLite backup CLI; no Discord connection or API keys needed."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import aiosqlite
from dotenv import load_dotenv

from services.backup_service import write_snapshot


async def backup(database: Path, output: Path) -> None:
    if not database.is_file():
        raise FileNotFoundError("The source database does not exist")
    source = await aiosqlite.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        await write_snapshot(source, output.resolve())
    finally:
        await source.close()


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Create a consistent live SQLite backup")
    parser.add_argument("--database", type=Path, default=Path(os.getenv("DATABASE_PATH", "data/ybot.sqlite3")))
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    asyncio.run(backup(arguments.database, arguments.output))


if __name__ == "__main__":
    main()
