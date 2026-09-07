"""The SSL expiry view-model must always be complete.

The check detail panel reads `expired` / `expires_soon` / `expires_critical` /
`days_until_expiration` unconditionally. They used to be set only when the
agent's date string parsed, and an unparseable date was swallowed — so a cert
whose expiry could not be read simply showed nothing, which is the one case an
operator most needs to see.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.services.views import check_detail_view_service
from app.services.views.check_detail_view_service import ssl_expiry_fields

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
    fields = ssl_expiry_fields(value)
    assert set(fields) == KEYS
    assert fields["days_until_expiration"] is None
    assert fields["expired"] is False
    assert fields["expires_soon"] is False
    assert fields["expires_critical"] is False


def test_unparseable_date_is_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    """The app logger does not propagate to root, so assert on it directly."""
    warnings: list[str] = []
    monkeypatch.setattr(
        check_detail_view_service.logger,
        "warning",
        lambda msg, **kw: warnings.append(msg),
    )
    ssl_expiry_fields("not a date")
    assert warnings == [
        "Unparseable SSL certificate expiration date — expiry not shown"
    ]


def test_missing_date_is_not_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    """No date at all is a plain absence, not a parse failure worth warning about."""
    warnings: list[str] = []
    monkeypatch.setattr(
        check_detail_view_service.logger,
        "warning",
        lambda msg, **kw: warnings.append(msg),
    )
    ssl_expiry_fields(None)
    assert warnings == []


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
    fields = ssl_expiry_fields(_in(days))
    assert fields["days_until_expiration"] == days
    assert fields["expired"] is expired
    assert fields["expires_soon"] is soon
    assert fields["expires_critical"] is critical


def test_expiration_date_is_echoed_back() -> None:
    """The panel renders the raw string too, so it must survive the transform."""
    raw = _in(10)
    assert ssl_expiry_fields(raw)["expiration_date"] == raw
