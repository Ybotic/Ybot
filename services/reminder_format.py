"""Timezone-aware reminder display helpers."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def parse_utc_timestamp(value: str) -> datetime:
    """Parse a persisted timestamp and normalize it to aware UTC."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_local_due(value: str | datetime, timezone_name: str) -> str:
    due_at = parse_utc_timestamp(value) if isinstance(value, str) else value
    localized = due_at.astimezone(ZoneInfo(timezone_name))
    hour = localized.strftime("%I").lstrip("0") or "12"
    return f"{localized:%A, %B %d} at {hour}:{localized:%M} {localized:%p}"


def format_remaining(
    due_at: str | datetime,
    timezone_name: str,
    *,
    now: datetime | None = None,
) -> str:
    due = parse_utc_timestamp(due_at) if isinstance(due_at, str) else due_at
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None or reference.utcoffset() is None:
        raise ValueError("Reference time must be timezone-aware")
    seconds = int((due.astimezone(timezone.utc) - reference.astimezone(timezone.utc)).total_seconds())
    if seconds < 0:
        return f"overdue by {_human_duration(-seconds)}"
    return f"in {_human_duration(seconds)}"


def local_day_bounds(
    timezone_name: str, *, now: datetime | None = None
) -> tuple[datetime, datetime]:
    """Return today's start and tomorrow's start as UTC, respecting DST."""
    zone = ZoneInfo(timezone_name)
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None or reference.utcoffset() is None:
        raise ValueError("Reference time must be timezone-aware")
    local_date = reference.astimezone(zone).date()
    start_local = datetime.combine(local_date, time.min, tzinfo=zone)
    end_local = datetime.combine(local_date + timedelta(days=1), time.min, tzinfo=zone)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def tomorrow_snooze_due(timezone_name: str, *, now: datetime | None = None) -> datetime:
    """Return the same local wall-clock time on the next calendar day."""
    zone = ZoneInfo(timezone_name)
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None or reference.utcoffset() is None:
        raise ValueError("Reference time must be timezone-aware")
    local_now = reference.astimezone(zone)
    return (local_now + timedelta(days=1)).astimezone(timezone.utc)


def _human_duration(seconds: int) -> str:
    if seconds < 60:
        return "less than a minute"
    minutes = seconds // 60
    if minutes < 60:
        return _quantity(minutes, "minute")
    hours = minutes // 60
    remaining_minutes = minutes % 60
    if hours < 24:
        if remaining_minutes:
            return f"{_quantity(hours, 'hour')} and {_quantity(remaining_minutes, 'minute')}"
        return _quantity(hours, "hour")
    days = hours // 24
    remaining_hours = hours % 24
    if remaining_hours:
        return f"{_quantity(days, 'day')} and {_quantity(remaining_hours, 'hour')}"
    return _quantity(days, "day")


def _quantity(value: int, unit: str) -> str:
    return f"{value} {unit}{'' if value == 1 else 's'}"
