"""
The display preferences every rendered date and time uses, held in memory.

Loaded from the settings at startup, and replaced by `SettingsCoreService`
whenever a display setting is set or reset, by any path.
"""

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class DisplaySettings:
    """How dates and times are shown."""

    timezone: str = "America/Chicago"
    date_format: str = "long"
    time_format: str = "12h"


_current = DisplaySettings()


def current_display_settings() -> DisplaySettings:
    """The preferences in effect now."""
    return _current


def update_display_settings(
    timezone: str | None = None,
    date_format: str | None = None,
    time_format: str | None = None,
) -> None:
    """Replace the preferences given; the others are kept."""
    global _current
    changes = {
        name: value
        for name, value in (
            ("timezone", timezone),
            ("date_format", date_format),
            ("time_format", time_format),
        )
        if value is not None
    }
    _current = replace(_current, **changes)
