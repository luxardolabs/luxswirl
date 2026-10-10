"""The SSL expiry view-model must always be complete.

The check detail panel reads `expired` / `expires_soon` / `expires_critical` /
`days_until_expiration` unconditionally. They used to be set only when the
agent's date string parsed, and an unparseable date was swallowed — so a cert
whose expiry could not be read simply showed nothing, which is the one case an
operator most needs to see.

The thresholds are the operator's, so they are arguments here rather than
constants: the ingest path and the panel must agree on the same windows.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from app.core.ssl_expiry import ssl_expiry_fields

KEYS = {
    "expiration_date",
    "days_until_expiration",
    "expired",
    "expires_soon",
    "expires_critical",
}


def _in(days: int) -> str:
    """An agent-format expiry string `days` from now."""
    return (datetime.now(UTC) + timedelta(days=days, hours=1)).strftime(
        "%b %d %H:%M:%S %Y GMT"
    )


@pytest.mark.parametrize(
    "value",
    [None, "", "not a date", "2025-01-15T23:59:59Z", "Feb 30 00:00:00 2025 GMT"],
)
def test_unreadable_expiry_still_returns_every_field(value: str | None) -> None:
    fields = ssl_expiry_fields(value, warning_days=30, critical_days=7)
    assert set(fields) == KEYS
    assert fields["days_until_expiration"] is None
    assert fields["expired"] is False
    assert fields["expires_soon"] is False
    assert fields["expires_critical"] is False


def test_unparseable_date_is_logged(app_logs: list[logging.LogRecord]) -> None:
    """A date that cannot be parsed is warned about, through the real logger."""
    ssl_expiry_fields("not a date", warning_days=30, critical_days=7)
    assert [r.getMessage() for r in app_logs if r.levelno == logging.WARNING] == [
        "Unparseable SSL certificate expiration date — expiry not shown"
    ]


def test_missing_date_is_not_logged(app_logs: list[logging.LogRecord]) -> None:
    """No date at all is a plain absence, not a parse failure worth warning about."""
    ssl_expiry_fields(None, warning_days=30, critical_days=7)
    assert [r for r in app_logs if r.levelno >= logging.WARNING] == []


@pytest.mark.parametrize(
    ("days", "expired", "soon", "critical"),
    [
        (-5, True, False, False),
        (3, False, True, True),
        (7, False, True, True),
        (8, False, True, False),
        (30, False, True, False),
        (31, False, False, False),
        (365, False, False, False),
    ],
)
def test_expiry_windows(days: int, expired: bool, soon: bool, critical: bool) -> None:
    fields = ssl_expiry_fields(_in(days), warning_days=30, critical_days=7)
    assert fields["days_until_expiration"] == days
    assert fields["expired"] is expired
    assert fields["expires_soon"] is soon
    assert fields["expires_critical"] is critical


def test_expiration_date_is_echoed_back() -> None:
    """The panel renders the raw string too, so it must survive the transform."""
    raw = _in(10)
    assert (
        ssl_expiry_fields(raw, warning_days=30, critical_days=7)["expiration_date"]
        == raw
    )


def test_thresholds_come_from_the_caller() -> None:
    """A 20-day-out cert is 'soon' under a 30-day window and fine under a 14-day one."""
    raw = _in(20)
    wide = ssl_expiry_fields(raw, warning_days=30, critical_days=14)
    narrow = ssl_expiry_fields(raw, warning_days=14, critical_days=7)
    assert wide["expires_soon"] is True
    assert narrow["expires_soon"] is False


def test_expiring_today_is_soon_and_critical() -> None:
    """0 days out is the most urgent not-yet-expired state, not a gap."""
    fields = ssl_expiry_fields(_in(0), warning_days=30, critical_days=7)
    assert fields["days_until_expiration"] == 0
    assert fields["expired"] is False
    assert fields["expires_soon"] is True
    assert fields["expires_critical"] is True
