"""Expiry flags for an agent-reported TLS certificate.

One home for the calculation, because it runs twice: once on ingest (stored
alongside the check result) and again when the detail panel renders, where
`days_until_expiration` has to be recomputed against the current clock rather
than the clock at check time. Both callers pass the operator's configured
thresholds, so the panel cannot disagree with the stored value.
"""

from datetime import UTC, datetime
from typing import Any

from app.shared.logger import get_logger

logger = get_logger("luxswirl.ssl_expiry")

# The agent reports OpenSSL's notAfter format, e.g. "Jan 15 23:59:59 2025 GMT".
# ISO 8601 is read too, so a producer that switches formats keeps the alerts.
_AGENT_DATE_FORMAT = "%b %d %H:%M:%S %Y %Z"


def _parse_expiry(expiration_date: str) -> datetime | None:
    """The expiry as an aware UTC datetime, or None when no format reads it."""
    try:
        # OpenSSL's notAfter is always GMT, but %Z carries no offset into the
        # result, so the zone is attached here.
        return datetime.strptime(expiration_date, _AGENT_DATE_FORMAT).replace(
            tzinfo=UTC
        )
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(expiration_date)
    except ValueError:
        return None
    # A certificate's validity dates are UTC; an ISO value without an offset is too.
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def ssl_expiry_fields(
    expiration_date: str | None,
    *,
    warning_days: int,
    critical_days: int,
) -> dict[str, Any]:
    """Expiry flags for one certificate, always fully populated.

    Returned whether or not the date parses, so no caller ever sees a
    half-filled certificate: `days_until_expiration` is None when the expiry is
    unknown, every flag is False and `state` is "unknown".

    A certificate expiring today (0 days) counts as expiring soon and
    critically — it is the most urgent not-yet-expired state, not a gap
    between "fine" and "expired".

    Args:
        expiration_date: The agent's reported expiry, or None if it sent none.
        warning_days: Days out at which a certificate counts as expiring soon.
        critical_days: Days out at which it counts as critical.

    Returns:
        expiration_date (echoed back so a caller can render it), plus
        days_until_expiration, the expired / expires_soon / expires_critical
        flags, and `state`: the one window it is in (expired, critical, soon,
        valid or unknown).
    """
    unknown: dict[str, Any] = {
        "state": "unknown",
        "expiration_date": expiration_date,
        "days_until_expiration": None,
        "expired": False,
        "expires_soon": False,
        "expires_critical": False,
    }
    if not expiration_date:
        return unknown

    expires_at = _parse_expiry(expiration_date)
    if expires_at is None:
        # A certificate whose expiry we cannot read is exactly the one an
        # operator needs to hear about, so this is a line in the log rather
        # than a silently-unset field.
        logger.warning(
            "Unparseable SSL certificate expiration date — expiry not shown",
            extra={"expiration_date": expiration_date},
        )
        return unknown

    days_remaining = (expires_at - datetime.now(UTC)).days
    expired = days_remaining < 0
    expires_soon = 0 <= days_remaining <= warning_days
    expires_critical = 0 <= days_remaining <= critical_days
    if expired:
        state = "expired"
    elif expires_critical:
        state = "critical"
    elif expires_soon:
        state = "soon"
    else:
        state = "valid"
    return {
        "state": state,
        "expiration_date": expiration_date,
        "days_until_expiration": days_remaining,
        "expired": expired,
        "expires_soon": expires_soon,
        "expires_critical": expires_critical,
    }
