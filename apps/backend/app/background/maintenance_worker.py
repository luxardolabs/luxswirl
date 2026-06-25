"""Maintenance worker — runs queued cascading mutations off the web request path.

Single asyncio task launched in main.py lifespan. Polls `maintenance_jobs` for
queued rows, claims them with `FOR UPDATE SKIP LOCKED`, runs the registered
handler in its own DB session (with statement_timeout lifted), marks done or
failed.

Handlers are registered in `HANDLERS` keyed by `MaintenanceJobKind` value. A
job whose `kind` has no handler is failed immediately with a clear error so it
doesn't sit queued forever.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta

from shared.logger import get_logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.datetime_utils import utc_now
from app.db import worker_session
from app.models.maintenance_job_model import MaintenanceJob
from app.services.core.maintenance_job_core_service import MaintenanceJobCoreService

logger = get_logger("luxswirl.background.maintenance_worker")

# Handler signature: (db, job) -> None. Handlers receive a session with
# statement_timeout already lifted to 0 and can run their cascade freely.
MaintenanceJobHandler = Callable[[AsyncSession, MaintenanceJob], Awaitable[None]]

HANDLERS: dict[str, MaintenanceJobHandler] = {}


def register_handler(kind: str, handler: MaintenanceJobHandler) -> None:
    HANDLERS[kind] = handler


_worker_task: asyncio.Task | None = None
_POLL_IDLE_SECONDS = 2.0
_POLL_ERROR_BACKOFF_SECONDS = 5.0
_TERMINAL_RETENTION = timedelta(hours=6)


async def _claim_one() -> MaintenanceJob | None:
    """Claim the next queued row + mark running. Returns None when queue empty.

    The FOR UPDATE SKIP LOCKED + mark_running happens in a single transaction
    (committed by worker_session on exit) so the row is irrevocably ours by the
    time we hand back control.
    """
    async with worker_session() as db:
        job = await MaintenanceJobCoreService.claim_next(db)
        if job is None:
            return None
        await MaintenanceJobCoreService.mark_running(db, job.id)
        await db.refresh(job)
        return job


async def _run_handler(job: MaintenanceJob) -> None:
    """Dispatch to the registered handler in a fresh session.

    `SET LOCAL statement_timeout = 0` lifts the web-pool default for this
    transaction only — the cascade is allowed to run as long as it needs.
    """
    handler = HANDLERS.get(job.kind)
    if handler is None:
        raise RuntimeError(f"No handler registered for maintenance job kind {job.kind!r}")

    async with worker_session() as db:
        await db.execute(text("SET LOCAL statement_timeout = 0"))
        await db.execute(text("SET LOCAL idle_in_transaction_session_timeout = 0"))
        await handler(db, job)


async def _finalize(job_id, success: bool, error: str | None = None) -> None:
    async with worker_session() as db:
        if success:
            await MaintenanceJobCoreService.mark_done(db, job_id)
        else:
            await MaintenanceJobCoreService.mark_failed(db, job_id, error or "Unknown error")


async def _periodic_cleanup_if_due(last_cleanup_at: list) -> None:
    """Drop terminal rows older than _TERMINAL_RETENTION once per hour."""
    now = utc_now()
    if last_cleanup_at and (now - last_cleanup_at[0]) < timedelta(hours=1):
        return
    last_cleanup_at[:] = [now]
    async with worker_session() as db:
        deleted = await MaintenanceJobCoreService.delete_old_terminal(db, now - _TERMINAL_RETENTION)
        if deleted:
            logger.info(
                "Pruned terminal maintenance jobs",
                extra={"deleted": deleted},
            )


async def _maintenance_worker_loop() -> None:
    """Main worker loop. Runs until cancelled."""
    logger.info("Maintenance worker started")
    last_cleanup_at: list = []

    while True:
        try:
            job = await _claim_one()
            if job is None:
                await _periodic_cleanup_if_due(last_cleanup_at)
                await asyncio.sleep(_POLL_IDLE_SECONDS)
                continue

            logger.info(
                "Maintenance worker claimed job",
                extra={
                    "job_id": str(job.id),
                    "kind": job.kind,
                    "target_id": str(job.target_id) if job.target_id else None,
                },
            )

            try:
                await _run_handler(job)
            except Exception as e:
                logger.exception(
                    "Maintenance job handler failed",
                    extra={"job_id": str(job.id), "kind": job.kind},
                )
                await _finalize(job.id, success=False, error=str(e))
            else:
                await _finalize(job.id, success=True)
                logger.info(
                    "Maintenance job completed",
                    extra={"job_id": str(job.id), "kind": job.kind},
                )

        except asyncio.CancelledError:
            logger.info("Maintenance worker cancelled")
            break
        except Exception:
            logger.exception("Maintenance worker loop iteration crashed")
            await asyncio.sleep(_POLL_ERROR_BACKOFF_SECONDS)


async def start_maintenance_worker() -> None:
    """Reap any pre-restart `running` rows, then start the loop."""
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        logger.warning("Maintenance worker already running")
        return

    async with worker_session() as db:
        await MaintenanceJobCoreService.mark_interrupted_on_restart(db)

    _worker_task = asyncio.create_task(_maintenance_worker_loop(), name="maintenance_worker")


async def stop_maintenance_worker() -> None:
    global _worker_task
    if _worker_task is None:
        return
    _worker_task.cancel()
    try:
        await _worker_task
    except asyncio.CancelledError:
        pass
    _worker_task = None
    logger.info("Maintenance worker stopped")
