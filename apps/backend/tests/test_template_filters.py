"""The display filters the web UI renders durations, counts and ages through.

Each one turns a raw value into the short text a table cell shows, so the edges
are what matter: nothing / zero, a unit boundary, singular vs plural, a naive
datetime from an old row, and a timestamp in the future (agent clock skew).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.web.template_filters import humanize_number, humanize_seconds, timeago

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (None, "-"),
        (0, "-"),
        (45, "45s"),
        (90, "1m 30s"),
        (3600, "1h"),
        # Seconds drop out once the duration reaches an hour.
        (3661, "1h 1m"),
        (86400, "1d"),
        (90061, "1d 1h"),
        (604800, "1w"),
        # Only the two largest units are kept.
        (604800 + 86400 + 3600, "1w 1d"),
    ],
)
def test_humanize_seconds(seconds: int | None, expected: str) -> None:
    assert humanize_seconds(seconds) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "-"),
        (0, "0"),
        (999, "999"),
        (1000, "1.0k"),
        (1234, "1.2k"),
        (1_234_567, "1.2m"),
        (1_234_567_890, "1.2b"),
        (999.9, "999"),
    ],
)
def test_humanize_number(value: int | float | None, expected: str) -> None:
    assert humanize_number(value) == expected


def _ago(**delta: float) -> datetime:
    return datetime.now(UTC) - timedelta(**delta)


def test_timeago_without_a_time_is_a_dash() -> None:
    assert timeago(None) == "--"


def test_timeago_in_the_future_says_so() -> None:
    assert timeago(datetime.now(UTC) + timedelta(minutes=5)) == "in the future"


def test_timeago_under_a_second_is_just_now() -> None:
    assert timeago(_ago(seconds=0.2)) == "just now"


@pytest.mark.parametrize(
    ("delta", "short", "long", "minimal"),
    [
        ({"seconds": 30}, "30s ago", "30 seconds ago", "30s"),
        ({"minutes": 1, "seconds": 5}, "1m ago", "1 minute ago", "1m"),
        ({"hours": 5}, "5h ago", "5 hours ago", "5h"),
        ({"days": 1, "hours": 2}, "1d ago", "1 day ago", "1d"),
        ({"weeks": 3}, "3w ago", "3 weeks ago", "3w"),
    ],
)
def test_timeago_picks_the_largest_unit_in_each_style(
    delta: dict[str, float], short: str, long: str, minimal: str
) -> None:
    dt = _ago(**delta)

    assert timeago(dt) == short
    assert timeago(dt, "long") == long
    assert timeago(dt, "minimal") == minimal


def test_timeago_reads_a_naive_datetime_as_utc() -> None:
    naive = _ago(hours=2).replace(tzinfo=None)

    assert timeago(naive) == "2h ago"
