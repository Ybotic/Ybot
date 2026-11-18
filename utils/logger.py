"""Logging configuration for YBOT."""

import logging


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    # HTTP/provider debug output may contain request details; keep it quiet.
    for name in ("openai", "httpx", "httpcore", "aiohttp", "aiosqlite"):
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger("discord").setLevel(logging.INFO)
