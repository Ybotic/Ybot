"""Strict, dependency-free parser for supported reminder time expressions."""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


class ReminderParseError(ValueError):
    """Raised when a reminder time or description needs clarification."""


@dataclass(frozen=True, slots=True)
class ParsedReminder:
    due_at: datetime
    description: str


_RELATIVE_RE = re.compile(
    r"^in\s+(?P<count>\d+)\s+(?P<unit>minutes?|mins?|hours?|hrs?)$",
    re.IGNORECASE,
)
_RELATIVE_WITH_DESCRIPTION_RE = re.compile(
    r"^in\s+(?P<count>\d+)\s+(?P<unit>minutes?|mins?|hours?|hrs?)\s+(?P<description>.+)$",
    re.IGNORECASE,
)
_TIME_RE = re.compile(
    r"^(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<period>am|pm)?$",
    re.IGNORECASE,
)
_DATE_AT_TIME_RE = re.compile(
    r"^(?P<date>.+?)\s+at\s+(?P<time>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)$",
    re.IGNORECASE,
)
_TIME_AND_DESCRIPTION_RE = re.compile(
    r"^(?P<time>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)(?:\s+)(?P<description>.+)$",
    re.IGNORECASE,
)
_WEEKDAY_RE = re.compile(r"^(?:(next)\s+)?([a-z]+)$", re.IGNORECASE)
_MONTH_DATE_RE = re.compile(
    r"^(?P<month>[a-z]+)\s+(?P<day>\d{1,2})(?:,?\s+(?P<year>\d{4}))?$",
    re.IGNORECASE,
)


def parse_time_expression(
    expression: str,
    timezone_name: str = "America/New_York",
    *,
    now: datetime | None = None,
    allow_past: bool = False,
) -> datetime:
    """Parse a complete time expression and return its due time in UTC."""
    local_now, now_utc, zone = _current_times(timezone_name, now)
    text = expression.strip()

    relative = _RELATIVE_RE.fullmatch(text)
    if relative:
        due_utc = now_utc + _relative_delta(
            relative.group("count"), relative.group("unit")
        )
        return _require_future(due_utc, now_utc, allow_past=allow_past)

    match = _DATE_AT_TIME_RE.fullmatch(text)
    if match is None:
        raise ReminderParseError(
            "Use a time such as `in 30 minutes` or `Friday at 5 PM`. "
            "Include AM/PM or a 24-hour time."
        )

    target_time = _parse_clock_time(match.group("time"))
    target_date = _parse_date_phrase(
        match.group("date"), local_now, target_time, allow_past=allow_past
    )
    due_utc = _to_utc(target_date, target_time, zone)
    return _require_future(due_utc, now_utc, allow_past=allow_past)


def parse_prefix_reminder(
    request: str,
    timezone_name: str = "America/New_York",
    *,
    now: datetime | None = None,
) -> ParsedReminder:
    """Split a prefix reminder into a strictly parsed time and description."""
    text = request.strip()
    if not text:
        raise ReminderParseError("Provide a reminder time and description.")

    relative = _RELATIVE_WITH_DESCRIPTION_RE.fullmatch(text)
    if relative:
        description = _normalize_description(relative.group("description"))
        due_at = parse_time_expression(
            f"in {relative.group('count')} {relative.group('unit')}",
            timezone_name,
            now=now,
        )
        return ParsedReminder(due_at, description)

    at_match = re.search(r"\s+at\s+", text, flags=re.IGNORECASE)
    if at_match is None:
        raise ReminderParseError(
            "I couldn't identify the time. Try `Friday at 5 PM submit lab`."
        )
    date_part = text[: at_match.start()]
    remainder = text[at_match.end() :]
    time_and_description = _TIME_AND_DESCRIPTION_RE.fullmatch(remainder.strip())
    if time_and_description is None:
        raise ReminderParseError(
            "Add a description after the time and include AM/PM or a 24-hour time."
        )

    description = _normalize_description(time_and_description.group("description"))
    due_at = parse_time_expression(
        f"{date_part.strip()} at {time_and_description.group('time').strip()}",
        timezone_name,
        now=now,
    )
    return ParsedReminder(due_at, description)


def validate_description(description: str, *, maximum_length: int = 1000) -> str:
    """Normalize and validate text supplied independently (e.g. slash input)."""
    return _normalize_description(description, maximum_length=maximum_length)


def _normalize_description(description: str, *, maximum_length: int = 1000) -> str:
    value = " ".join(description.split()).strip()
    if not value:
        raise ReminderParseError("Reminder description cannot be empty.")
    if len(value) > maximum_length:
        raise ReminderParseError(
            f"Reminder description must be {maximum_length} characters or fewer."
        )
    return value[0].upper() + value[1:]


def _current_times(
    timezone_name: str, now: datetime | None
) -> tuple[datetime, datetime, ZoneInfo]:
    zone = ZoneInfo(timezone_name)
    if now is None:
        now_utc = datetime.now(timezone.utc)
    elif now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("The reference time must be timezone-aware")
    else:
        now_utc = now.astimezone(timezone.utc)
    return now_utc.astimezone(zone), now_utc, zone


def _relative_delta(count_value: str, unit: str) -> timedelta:
    if len(count_value) > 7:
        raise ReminderParseError("That relative reminder time is too far in the future.")
    count = int(count_value)
    if count <= 0:
        raise ReminderParseError("Relative reminder times must be greater than zero.")
    if unit.lower().startswith(("min",)):
        delta = timedelta(minutes=count)
    else:
        delta = timedelta(hours=count)
    if delta > timedelta(days=3650):
        raise ReminderParseError("Reminder times cannot be more than 10 years away.")
    return delta


def _parse_clock_time(value: str) -> time:
    match = _TIME_RE.fullmatch(value.strip())
    if match is None:
        raise ReminderParseError("That time is invalid. Use a time like `5 PM` or `21:30`.")
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or "0")
    period = (match.group("period") or "").lower()

    if minute > 59:
        raise ReminderParseError("Minutes must be between 00 and 59.")
    if period:
        if not 1 <= hour <= 12:
            raise ReminderParseError("For AM/PM times, the hour must be from 1 to 12.")
        hour = hour % 12 + (12 if period == "pm" else 0)
    elif ":" not in value:
        raise ReminderParseError(
            "That time is ambiguous. Include AM/PM or use a 24-hour time like `17:00`."
        )
    elif hour > 23:
        raise ReminderParseError("24-hour times must have an hour from 00 to 23.")

    return time(hour, minute)


def _parse_date_phrase(
    value: str,
    local_now: datetime,
    target_time: time,
    *,
    allow_past: bool = False,
) -> date:
    phrase = " ".join(value.lower().split())
    today = local_now.date()
    if phrase == "today":
        return today
    if phrase == "tomorrow":
        return today + timedelta(days=1)

    weekday_match = _WEEKDAY_RE.fullmatch(phrase)
    if weekday_match:
        next_week = weekday_match.group(1) is not None
        weekday_name = weekday_match.group(2)
        weekday = _weekday_number(weekday_name)
        if weekday is None:
            raise ReminderParseError(f"I don't recognize the weekday `{weekday_name}`.")
        days_ahead = (weekday - today.weekday()) % 7
        if next_week and days_ahead == 0:
            days_ahead = 7
        elif not next_week and days_ahead == 0 and target_time <= local_now.time().replace(tzinfo=None):
            days_ahead = 7
        return today + timedelta(days=days_ahead)

    month_match = _MONTH_DATE_RE.fullmatch(phrase)
    if month_match:
        month_name = month_match.group("month").title()
        month_number = _month_number(month_name)
        if month_number is None:
            raise ReminderParseError(f"I don't recognize the month `{month_name}`.")
        day_number = int(month_match.group("day"))
        year_value = month_match.group("year")
        year = int(year_value) if year_value else today.year
        try:
            target_date = date(year, month_number, day_number)
        except ValueError as exc:
            raise ReminderParseError("That calendar date is invalid.") from exc

        if year_value is None and not allow_past:
            tentative = datetime.combine(target_date, target_time)
            if tentative <= local_now.replace(tzinfo=None):
                raise ReminderParseError(
                    "That date and time have already passed. Include a year or choose a future date."
                )
        return target_date

    raise ReminderParseError(
        "I couldn't identify that date. Try today, tomorrow, a weekday, "
        "or a month and day such as `November 20`."
    )


def _weekday_number(value: str) -> int | None:
    normalized = value[:3].lower()
    for weekday, name in enumerate(calendar.day_name):
        if name[:3].lower() == normalized:
            return weekday
    return None


def _month_number(value: str) -> int | None:
    normalized = value[:3].lower()
    for month_number in range(1, 13):
        if calendar.month_name[month_number][:3].lower() == normalized:
            return month_number
    return None


def _to_utc(target_date: date, target_time: time, zone: ZoneInfo) -> datetime:
    local_naive = datetime.combine(target_date, target_time)
    candidates: dict[timedelta | None, datetime] = {}
    for fold in (0, 1):
        candidate = local_naive.replace(tzinfo=zone, fold=fold)
        round_trip = candidate.astimezone(timezone.utc).astimezone(zone)
        if round_trip.replace(tzinfo=None) == local_naive:
            candidates[candidate.utcoffset()] = candidate

    if not candidates:
        raise ReminderParseError(
            "That local time does not exist because of a daylight-saving time change. "
            "Choose another time."
        )
    if len(candidates) > 1:
        raise ReminderParseError(
            "That local time occurs twice because of a daylight-saving time change. "
            "Choose an unambiguous time."
        )
    return next(iter(candidates.values())).astimezone(timezone.utc)


def _require_future(
    due_at: datetime, now_utc: datetime, *, allow_past: bool = False
) -> datetime:
    if not allow_past and due_at <= now_utc:
        raise ReminderParseError("That reminder time has already passed; choose a future time.")
    return due_at
