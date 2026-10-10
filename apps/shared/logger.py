"""Loggers for LuxSwirl code, and the level defaults both services configure.

Logging itself is set up once per process by the fleet module each service
carries (`app/core/logging_config.py`, `configure_logging(...)`, called from the
entrypoint) — this module sets nothing up. It keeps the `get_logger` every
module already imports, and the third-party level defaults the entrypoints pass
as `configure_logging(levels=...)`.
"""

import logging

# Third-party libraries that are noisy at INFO. Passed as `levels=`; an
# operator's per-module override (LOG__MODULE_LEVELS / LOG_MODULE_LEVELS) wins.
THIRD_PARTY_LEVELS: dict[str, str | int] = {
    "uvicorn.access": logging.WARNING,
    "sqlalchemy": logging.WARNING,
    "sqlalchemy.engine": logging.WARNING,
    "sqlalchemy.engine.Engine": logging.WARNING,
    "sqlalchemy.pool": logging.WARNING,
    "asyncpg": logging.WARNING,
    "aiomysql": logging.WARNING,
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "watchfiles": logging.WARNING,
    "alembic.runtime.migration": logging.WARNING,
}


def get_logger(name: str = "luxswirl") -> logging.Logger:
    """The stdlib logger `name` (conventionally `luxswirl.<area>`)."""
    return logging.getLogger(name)
