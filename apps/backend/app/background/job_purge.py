"""
Job purge background task.

Periodically removes expired jobs from the database based on retention policy.
"""

import asyncio
import contextlib

from app.core.config import settings
from app.db import worker_session
from app.services.core.job_core_service import JobCoreService
from app.shared.logger import get_logger

logger = get_logger("luxswirl.background.job_purge")

# Task handle
_purge_task: asyncio.Task[None] | None = None


async def _job_purge_loop() -> None:
    """Background task loop to purge expired jobs."""
    interval_hours = settings.server.job_purge_interval_hours
    logger.info(
        "Job purge task started",
        extra={"interval_hours": interval_hours},
    )

    while True:
        try:
            # Wait for interval
            await asyncio.sleep(interval_hours * 3600)  # Convert hours to seconds

            # Purge expired jobs: one transaction, committed by the owner
            async with worker_session() as db:
                deleted_count = await JobCoreService.purge_expired_jobs(db)
                if deleted_count > 0:
                    logger.info(
                        "Purged expired jobs",
                        extra={"deleted_count": deleted_count},
                    )

        except asyncio.CancelledError:
            logger.info("Job purge task cancelled")
            break
        # swallowed-exceptions: daemon loop — one transient DB error must not
        # kill the purge task for the life of the process.
        except Exception:
            logger.exception("Error in job purge task")
            # Continue running despite errors


def start_job_purge_task() -> asyncio.Task[None]:
    """
    Start the job purge background task.

    Returns:
        Task handle for the background task
    """
    global _purge_task

    if _purge_task is not None:
        logger.warning("Job purge task already running")
        return _purge_task

    _purge_task = asyncio.create_task(_job_purge_loop())
    logger.info("Job purge background task started")

    return _purge_task


async def stop_job_purge_task() -> None:
    """Stop the job purge background task."""
    global _purge_task

    if _purge_task is None:
        return

    logger.info("Stopping job purge task")
    _purge_task.cancel()

    with contextlib.suppress(asyncio.CancelledError):
        await _purge_task

    _purge_task = None
    logger.info("Job purge task stopped")
