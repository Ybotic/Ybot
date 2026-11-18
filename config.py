"""Environment-backed configuration for YBOT."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


class ConfigurationError(ValueError):
    """Raised when required environment configuration is invalid."""


@dataclass(frozen=True, slots=True)
class Config:
    discord_token: str = field(repr=False)
    owner_id: int
    guild_id: int
    timezone_name: str = "America/New_York"
    ai_enabled: bool = False
    ai_provider: str = "openai"
    openai_api_key: str = field(default="", repr=False)
    ai_model: str = ""
    ai_system_prompt_file: str = ""
    database_path: str = str(Path(__file__).resolve().parent / "data" / "ybot.sqlite3")
    backup_dir: str = str(Path(__file__).resolve().parent / "backups")
    backup_retention_days: int = 14
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        """Load and validate settings, without exposing secret values."""
        if env is None:
            load_dotenv()
            values: Mapping[str, str] = os.environ
        else:
            values = env

        errors: list[str] = []
        token = values.get("DISCORD_TOKEN", "").strip()
        if not token:
            errors.append("DISCORD_TOKEN is required")

        owner_id = cls._parse_id("OWNER_ID", values.get("OWNER_ID"), errors)
        guild_id = cls._parse_id("GUILD_ID", values.get("GUILD_ID"), errors)
        timezone_name = values.get("TIMEZONE", "America/New_York").strip()
        if not timezone_name:
            timezone_name = "America/New_York"
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            errors.append("TIMEZONE must be a valid IANA timezone name")

        ai_value = values.get("AI_ENABLED", "false").strip().lower()
        if ai_value not in {"true", "false"}:
            errors.append("AI_ENABLED must be true or false")
        ai_enabled = ai_value == "true"
        ai_provider = values.get("AI_PROVIDER", "openai").strip().lower()
        api_key = values.get("OPENAI_API_KEY", "").strip()
        ai_model = values.get("AI_MODEL", "").strip()
        if ai_enabled:
            if ai_provider != "openai":
                errors.append("AI_PROVIDER must be openai when AI is enabled")
            if not api_key:
                errors.append("OPENAI_API_KEY is required when AI_ENABLED=true")
            if not ai_model:
                errors.append("AI_MODEL is required when AI_ENABLED=true")
        try:
            retention = int(values.get("BACKUP_RETENTION_DAYS", "14"))
            if not 1 <= retention <= 365:
                raise ValueError
        except ValueError:
            retention = 14
            errors.append("BACKUP_RETENTION_DAYS must be between 1 and 365")
        log_level = values.get("LOG_LEVEL", "INFO").strip().upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            errors.append("LOG_LEVEL is invalid")
        for name in ("DATABASE_PATH", "BACKUP_DIR"):
            if name in values and not values[name].strip():
                errors.append(f"{name} must not be empty when supplied")

        if errors:
            raise ConfigurationError("Invalid configuration: " + "; ".join(errors))

        return cls(
            discord_token=token,
            owner_id=owner_id,
            guild_id=guild_id,
            timezone_name=timezone_name,
            ai_enabled=ai_enabled,
            ai_provider=ai_provider,
            openai_api_key=api_key,
            ai_model=ai_model,
            ai_system_prompt_file=values.get("AI_SYSTEM_PROMPT_FILE", "").strip(),
            database_path=values.get("DATABASE_PATH", cls.__dataclass_fields__["database_path"].default).strip(),
            backup_dir=values.get("BACKUP_DIR", cls.__dataclass_fields__["backup_dir"].default).strip(),
            backup_retention_days=retention,
            log_level=log_level,
        )

    @staticmethod
    def _parse_id(name: str, raw_value: str | None, errors: list[str]) -> int:
        if raw_value is None or not raw_value.strip():
            errors.append(f"{name} is required")
            return 0

        try:
            value = int(raw_value.strip())
        except ValueError:
            errors.append(f"{name} must be a positive integer")
            return 0

        if value <= 0:
            errors.append(f"{name} must be a positive integer")
            return 0
        return value
