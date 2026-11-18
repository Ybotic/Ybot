from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from services.reminder_parser import (
    ReminderParseError,
    parse_prefix_reminder,
    parse_time_expression,
)


def new_york_time(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("America/New_York"))


def test_relative_time_is_converted_to_utc() -> None:
    now = datetime(2026, 11, 16, 17, 0, tzinfo=timezone.utc)

    due = parse_time_expression("in 30 minutes", now=now)

    assert due == datetime(2026, 11, 16, 17, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("tomorrow at 5 PM", datetime(2026, 11, 17, 22, 0, tzinfo=timezone.utc)),
        ("Friday at 5 PM", datetime(2026, 11, 20, 22, 0, tzinfo=timezone.utc)),
        ("November 20 at 8 PM", datetime(2026, 11, 21, 1, 0, tzinfo=timezone.utc)),
        ("today at 21:30", datetime(2026, 11, 17, 2, 30, tzinfo=timezone.utc)),
        ("next Monday at 10 AM", datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)),
    ],
)
def test_supported_absolute_expressions(expression, expected) -> None:
    now = new_york_time(2026, 11, 16, 12)

    assert parse_time_expression(expression, now=now) == expected


def test_prefix_parser_separates_time_from_description() -> None:
    parsed = parse_prefix_reminder(
        "Friday at 5pm submit lab",
        now=new_york_time(2026, 11, 16, 12),
    )

    assert parsed.description == "Submit lab"
    assert parsed.due_at == datetime(2026, 11, 20, 22, 0, tzinfo=timezone.utc)


def test_prefix_parser_supports_relative_time_and_description() -> None:
    parsed = parse_prefix_reminder(
        "in 2 hours check laundry",
        now=datetime(2026, 11, 16, 17, 0, tzinfo=timezone.utc),
    )

    assert parsed.description == "Check laundry"
    assert parsed.due_at == datetime(2026, 11, 16, 19, 0, tzinfo=timezone.utc)


def test_timezone_conversion_observes_standard_time_offset() -> None:
    due = parse_time_expression(
        "March 8, 2026 at 1:30 AM",
        now=new_york_time(2026, 3, 1, 12),
    )

    assert due == datetime(2026, 3, 8, 6, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "expression",
    [
        "February 30, 2027 at 5 PM",
        "Friday at 5",
        "today at 25:00",
        "March 8, 2026 at 2:30 AM",
        "November 1, 2026 at 1:30 AM",
    ],
)
def test_invalid_ambiguous_or_nonexistent_times_are_rejected(expression) -> None:
    with pytest.raises(ReminderParseError):
        parse_time_expression(expression, now=new_york_time(2026, 2, 1, 12))


def test_past_time_is_rejected_instead_of_silently_moving_to_next_day() -> None:
    with pytest.raises(ReminderParseError, match="already passed"):
        parse_time_expression(
            "today at 9 AM",
            now=new_york_time(2026, 11, 16, 12),
        )


def test_unreasonably_distant_relative_time_is_rejected() -> None:
    with pytest.raises(ReminderParseError, match="10 years"):
        parse_time_expression(
            "in 100000 hours",
            now=datetime(2026, 11, 16, 17, 0, tzinfo=timezone.utc),
        )
