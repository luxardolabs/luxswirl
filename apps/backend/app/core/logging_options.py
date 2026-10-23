"""The server's arguments to `configure_logging()`, built in one place.

`app/main.py` installs logging with these; so does `alembic/env.py`, which runs
inside the server process when the test harness (or a startup upgrade) migrates
in-process. Both must install the same thing, or whichever ran last would win.
"""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.shared.log_redaction import scrub
from app.shared.logger import THIRD_PARTY_LEVELS


def logging_options() -> dict[str, Any]:
    """`configure_logging(**logging_options())` for the server process."""
    return {
        "service": "luxswirl",
        "version": settings.app_version,
        "level": settings.logging.level,
        # Code defaults, then the operator's LOG__MODULE_LEVELS on top
        # (LOG_MODULE_LEVELS, read by the module itself, wins over both).
        "levels": {**THIRD_PARTY_LEVELS, **settings.logging.module_levels},
        "environment": settings.environment,
        "redact": scrub,
    }
