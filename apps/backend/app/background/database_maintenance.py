"""
Database maintenance background task.

Runs `DatabaseMaintenanceCoreService.run_cycle()` every
`server.database_maintenance_interval_hours`: bloat-driven VACUUM, retention for
artifacts and notification logs, and the continuous-aggregate refresh. This
module only schedules it; the cycle and its SQL live in the core service and
`DatabaseMaintenanceCRUD`.
"""

import asyncio
import contextlib

from shared.logger import get_logger

from app.core.config import settings
from app.services.core.database_maintenance_core_service import (
    DatabaseMaintenanceCoreService,
)

logger = get_logger("luxswirl.background.db_maintenance")

# Task handle
_maintenance_task: asyncio.Task[None] | None = None


async def _database_maintenance_loop() -> None:
    """Background task loop for database maintenance."""
    interval_hours = settings.server.database_maintenance_interval_hours
    logger.info(
        "Database maintenance task started",
        extra={"interval_hours": interval_hours},
    )

    while True:
        try:
            await asyncio.sleep(interval_hours * 3600)
            await DatabaseMaintenanceCoreService.run_cycle()
        except asyncio.CancelledError:
            logger.info("Database maintenance task cancelled")
            break
        # swallowed-exceptions: daemon loop — one transient DB error must not
        # kill the maintenance task for the life of the process.
        except Exception:
            logger.exception("Error in database maintenance task")


def start_database_maintenance_task() -> asyncio.Task[None]:
    """
    Start the database maintenance background task.

    Returns:
        Task handle for the background task
    """
    global _maintenance_task

    if _maintenance_task is not None:
        logger.warning("Database maintenance task already running")
        return _maintenance_task

    _maintenance_task = asyncio.create_task(_database_maintenance_loop())
    logger.info("Database maintenance background task started")

    return _maintenance_task


async def stop_database_maintenance_task() -> None:
    """Stop the database maintenance background task."""
    global _maintenance_task

    if _maintenance_task is None:
        return

    logger.info("Stopping database maintenance task")
    _maintenance_task.cancel()

    with contextlib.suppress(asyncio.CancelledError):
        await _maintenance_task

    _maintenance_task = None
    logger.info("Database maintenance task stopped")
