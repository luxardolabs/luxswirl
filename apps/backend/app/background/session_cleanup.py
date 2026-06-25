"""
Session cleanup background task.

Periodically removes expired sessions from the database to prevent table bloat.
"""

import asyncio

from shared.logger import get_logger

from app.db import worker_session
from app.services.core.auth_core_service import AuthCoreService

logger = get_logger("luxswirl.background.session_cleanup")

# Task handle
_cleanup_task: asyncio.Task | None = None

# Cleanup interval in hours (run every hour by default)
CLEANUP_INTERVAL_HOURS = 1


async def _session_cleanup_loop():
    """Background task loop to cleanup expired sessions."""
    logger.info(
        "Session cleanup task started",
        extra={"interval_hours": CLEANUP_INTERVAL_HOURS},
    )

    auth_service = AuthCoreService()

    while True:
        try:
            # Wait for interval
            await asyncio.sleep(CLEANUP_INTERVAL_HOURS * 3600)  # Convert hours to seconds

            # Cleanup expired sessions
            async with worker_session() as db:
                deleted_count = await auth_service.cleanup_expired_sessions(db)
                if deleted_count > 0:
                    logger.info(
                        "Cleaned up expired sessions",
                        extra={"deleted_count": deleted_count},
                    )

        except asyncio.CancelledError:
            logger.info("Session cleanup task cancelled")
            break
        except Exception:
            logger.error("Error in session cleanup task", exc_info=True)
            # Continue running despite errors


def start_session_cleanup_task() -> asyncio.Task:
    """
    Start the session cleanup background task.

    Returns:
        Task handle for the background task
    """
    global _cleanup_task

    if _cleanup_task is not None:
        logger.warning("Session cleanup task already running")
        return _cleanup_task

    _cleanup_task = asyncio.create_task(_session_cleanup_loop())
    logger.info("Session cleanup background task started")

    return _cleanup_task


async def stop_session_cleanup_task():
    """Stop the session cleanup background task."""
    global _cleanup_task

    if _cleanup_task is None:
        return

    logger.info("Stopping session cleanup task")
    _cleanup_task.cancel()

    try:
        await _cleanup_task
    except asyncio.CancelledError:
        pass

    _cleanup_task = None
    logger.info("Session cleanup task stopped")
