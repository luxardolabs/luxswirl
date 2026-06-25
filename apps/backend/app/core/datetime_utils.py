"""
Datetime utilities for timezone-aware operations.

All datetime operations should use timezone-aware datetimes in UTC.
This ensures compatibility with PostgreSQL's 'timestamp with time zone' type.
"""

from datetime import UTC, datetime


def utc_now() -> datetime:
    """
    Get current UTC time as a timezone-aware datetime.

    This replaces datetime.utcnow() which returns timezone-naive datetimes.
    All database timestamps should be timezone-aware for proper comparison.

    Returns:
        Timezone-aware datetime in UTC

    Example:
        >>> now = utc_now()
        >>> now.tzinfo
        datetime.timezone.utc
    """
    return datetime.now(UTC)
