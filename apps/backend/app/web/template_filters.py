"""
Jinja2 template filters for the web UI.
"""

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.schemas.pagination_schema import paginated_url

# Abbreviation mapping for the timeago filter
_TIME_UNITS = [
    (604800, "w"),
    (86400, "d"),
    (3600, "h"),
    (60, "m"),
    (1, "s"),
]

# Global timezone cache (loaded from database settings at startup)
_cached_timezone: str = "America/Chicago"
_cached_date_format: str = "long"
_cached_time_format: str = "12h"


def update_settings_cache(
    timezone: str | None = None,
    date_format: str | None = None,
    time_format: str | None = None,
) -> None:
    """
    Update the cached display settings.

    Called at startup and when settings are changed via the settings page.

    Args:
        timezone: Timezone name (e.g., "America/Chicago")
        date_format: Date format preference (long/short/iso)
        time_format: Time format preference (12h/24h)
    """
    global _cached_timezone, _cached_date_format, _cached_time_format

    if timezone is not None:
        _cached_timezone = timezone
    if date_format is not None:
        _cached_date_format = date_format
    if time_format is not None:
        _cached_time_format = time_format


def format_datetime(
    dt: datetime | str | None,
    format_str: str | None = None,
    timezone: str | None = None,
    include_date: bool = False,
) -> str:
    """
    Format a datetime with timezone conversion using user preferences.

    All datetimes in the database are UTC. This filter converts them
    to the display timezone and formats using configured date/time preferences.

    Args:
        dt: Datetime object or ISO string to format
        format_str: Optional explicit strftime format (overrides settings)
        timezone: Target timezone name (default: uses cached setting from database)
        include_date: If True, includes date + time; if False, time only

    Returns:
        Formatted datetime string

    Examples:
        {{ check.last_failure_at | format_datetime }}  # Uses settings (time only)
        {{ check.last_failure_at | format_datetime(include_date=True) }}  # Date + time
        {{ check.last_failure_at | format_datetime("%Y-%m-%d %I:%M%p") }}  # Custom format
    """
    if dt is None:
        return "-"

    # Convert ISO string to datetime if needed
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))

    # Use cached timezone from settings if not explicitly provided
    if timezone is None:
        timezone = _cached_timezone

    # Convert UTC to target timezone (with fallback to UTC if invalid)
    try:
        tz = ZoneInfo(timezone)
    except Exception:
        # Fallback to UTC if timezone is invalid
        tz = ZoneInfo("UTC")

    local_dt = dt.astimezone(tz)

    # Build format string from settings if not explicitly provided
    if format_str is None:
        # Time format based on setting
        if _cached_time_format == "24h":
            time_fmt = "%H:%M"
        else:  # 12h
            time_fmt = "%I:%M%p"

        if include_date:
            # Date format based on setting
            if _cached_date_format == "iso":
                date_fmt = "%Y-%m-%d"
            elif _cached_date_format == "short":
                date_fmt = "%-m/%-d/%Y"  # 11/8/2025
            else:  # long
                date_fmt = "%B %-d, %Y"  # November 8, 2025

            format_str = f"{date_fmt} {time_fmt}"
        else:
            format_str = time_fmt

    # Format with proper capitalization (Python's strftime uses proper case)
    formatted = local_dt.strftime(format_str)

    # Only lowercase the AM/PM indicator for 12h format
    # Replace " AM" → " am" and " PM" → " pm", but keep month abbreviations capitalized
    if "AM" in formatted:
        formatted = formatted.replace("AM", "am")
    if "PM" in formatted:
        formatted = formatted.replace("PM", "pm")

    return formatted


def humanize_seconds(seconds: int | None) -> str:
    """
    Convert seconds to human-readable duration.

    Examples:
        45 -> "45s"
        90 -> "1m 30s"
        3661 -> "1h 1m"
        86400 -> "1d"
        604800 -> "7d"
    """
    if seconds is None or seconds == 0:
        return "-"

    weeks = seconds // 604800
    days = (seconds % 604800) // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60

    parts = []
    if weeks > 0:
        parts.append(f"{weeks}w")
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if secs > 0 and not (weeks or days or hours):  # Only show seconds for short durations
        parts.append(f"{secs}s")

    # Return first 2 parts for brevity (e.g., "1w 2d" or "3h 45m")
    return " ".join(parts[:2]) if parts else "0s"


def humanize_number(value: int | float | None) -> str:
    """
    Convert large numbers to human-readable format (k, m, b).

    Examples:
        1234 -> "1.2k"
        1234567 -> "1.2m"
        1234567890 -> "1.2b"
        999 -> "999"
    """
    if value is None:
        return "-"

    value = float(value)

    if value < 1000:
        return str(int(value))
    elif value < 1_000_000:
        return f"{value / 1000:.1f}k"
    elif value < 1_000_000_000:
        return f"{value / 1_000_000:.1f}m"
    else:
        return f"{value / 1_000_000_000:.1f}b"


def timeago(dt: datetime | None, style: str = "short") -> str:
    """
    Format a datetime as a relative time string.

    Pure Python implementation — no humanize dependency.

    Args:
        dt: A datetime object (assumed UTC)
        style: Output style - 'short' for '5m ago', 'long' for '5 minutes ago',
               'minimal' for '5m' (no suffix)

    Returns:
        Relative time string

    Usage:
        {{ job.last_run_at | timeago }}          -> "5m ago"
        {{ job.last_run_at | timeago('long') }}  -> "5 minutes ago"
        {{ job.last_run_at | timeago('minimal') }} -> "5m"
    """
    if dt is None:
        return "--"

    now = datetime.now(UTC)

    # Ensure timezone-aware
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)

    # Handle future times
    if dt > now:
        return "in the future"

    delta_seconds = int((now - dt).total_seconds())

    if delta_seconds < 1:
        return "just now"

    # Find the largest unit
    short_suffix = "s"
    value = delta_seconds
    for unit_seconds, suffix in _TIME_UNITS:
        if delta_seconds >= unit_seconds:
            value = delta_seconds // unit_seconds
            short_suffix = suffix
            break

    # Long names for 'long' style
    long_names = {
        "w": ("week", "weeks"),
        "d": ("day", "days"),
        "h": ("hour", "hours"),
        "m": ("minute", "minutes"),
        "s": ("second", "seconds"),
    }

    if style == "long":
        singular, plural = long_names[short_suffix]
        unit_name = singular if value == 1 else plural
        return f"{value} {unit_name} ago"
    elif style == "minimal":
        return f"{value}{short_suffix}"
    else:  # short
        return f"{value}{short_suffix} ago"


def _compute_static_version() -> str:
    """Compute a short cache-busting token from the newest mtime in web/static/.

    Appended as ?v=<token> on <script src> and <link href> tags so browsers
    refetch when any static file is updated. Computed once at app startup;
    an app restart picks up new file changes.
    """
    static_root = Path(__file__).parent / "static"
    try:
        newest_mtime = max(
            (p.stat().st_mtime for p in static_root.rglob("*") if p.is_file()),
            default=0.0,
        )
    except OSError:
        newest_mtime = 0.0
    return hashlib.md5(str(newest_mtime).encode()).hexdigest()[:8]


def register_filters(jinja_env):
    """
    Register all custom filters with a Jinja2 environment.

    Args:
        jinja_env: Jinja2 environment instance
    """
    jinja_env.filters["humanize_seconds"] = humanize_seconds
    jinja_env.filters["format_datetime"] = format_datetime
    jinja_env.filters["humanize_number"] = humanize_number
    jinja_env.filters["timeago"] = timeago
    jinja_env.globals["static_version"] = _compute_static_version()
    # paginated_url is universal (every paginated page uses the same URL
    # construction); registering as a global avoids per-template imports.
    jinja_env.globals["paginated_url"] = paginated_url
