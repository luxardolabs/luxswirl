"""Trusted proxy refresh background task.

Looks up `settings.security.trusted_proxy_hosts` at startup and every
PROXY_HOSTS_REFRESH_SECONDS after, so X-Forwarded-For is taken from the
shipped proxy (the compose service `nginx`) at whatever address it has now,
and a request never waits on DNS (`app/core/request_helpers.py`).
"""

import asyncio
import contextlib

from app.core.request_helpers import (
    PROXY_HOSTS_REFRESH_SECONDS,
    refresh_trusted_proxy_hosts,
)
from app.shared.logger import get_logger

logger = get_logger("luxswirl.background.trusted_proxy_refresh")

_refresh_task: asyncio.Task[None] | None = None


async def _refresh_loop() -> None:
    """Look the proxy names up now, then every PROXY_HOSTS_REFRESH_SECONDS.

    The first lookup runs here, not in startup, so a slow resolver never holds
    the server's start: until it answers, only the configured networks count.
    """
    while True:
        try:
            await refresh_trusted_proxy_hosts()
            await asyncio.sleep(PROXY_HOSTS_REFRESH_SECONDS)
        except asyncio.CancelledError:
            break
        # swallowed-exceptions: daemon loop — one failed lookup must not stop the
        # refresh for the life of the process; the last addresses stay trusted.
        except Exception:
            logger.exception("Error refreshing the trusted proxy addresses")
            await asyncio.sleep(PROXY_HOSTS_REFRESH_SECONDS)


def start_trusted_proxy_refresh_task() -> asyncio.Task[None]:
    """Keep the proxy names' addresses current in the background."""
    global _refresh_task

    if _refresh_task is not None:
        return _refresh_task

    _refresh_task = asyncio.create_task(_refresh_loop())
    return _refresh_task


async def stop_trusted_proxy_refresh_task() -> None:
    """Stop the trusted proxy refresh background task."""
    global _refresh_task

    if _refresh_task is None:
        return

    _refresh_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _refresh_task
    _refresh_task = None
