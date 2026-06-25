"""MaintenanceJobCoreService — business logic for backend-internal maintenance jobs.

Web routes call `enqueue()` and `get_by_id()`. The worker calls `claim_next()`,
`mark_running/done/failed()`. Cleanup task calls `mark_interrupted_on_restart()`
and `delete_old_terminal()`.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundException
from app.crud.maintenance_job_crud import MaintenanceJobCRUD
from app.models.enum_model import MaintenanceJobKind
from app.models.maintenance_job_model import MaintenanceJob

logger = get_logger("luxswirl.services.maintenance_job")


class MaintenanceJobCoreService:
    @staticmethod
    async def get_by_id(db: AsyncSession, job_id: UUID) -> MaintenanceJob:
        job = await MaintenanceJobCRUD.get_by_id(db, job_id)
        if job is None:
            raise NotFoundException(f"Maintenance job {job_id} not found")
        return job

    @staticmethod
    async def enqueue(
        db: AsyncSession,
        *,
        kind: MaintenanceJobKind | str,
        target_id: UUID | None,
        params: dict[str, Any] | None = None,
        owner_id: UUID | None = None,
    ) -> MaintenanceJob:
        """Insert an intent row, idempotently.

        The partial unique index ``(kind, target_id) WHERE status IN (queued,
        running)`` lets the INSERT use ON CONFLICT DO NOTHING, so a double-click
        or a race both collapse to the single inflight row — the DB enforces it,
        no catch and no rollback here.
        """
        kind_value = kind.value if isinstance(kind, MaintenanceJobKind) else kind

        job = await MaintenanceJobCRUD.insert(
            db,
            kind=kind_value,
            target_id=target_id,
            params=params,
            owner_id=owner_id,
        )
        if job is not None:
            logger.info(
                "Maintenance job enqueued",
                extra={
                    "job_id": str(job.id),
                    "kind": kind_value,
                    "target_id": str(target_id) if target_id else None,
                    "owner_id": str(owner_id) if owner_id else None,
                },
            )
            return job

        # No row inserted → an inflight job already exists for this target.
        # (A NULL target_id never conflicts, so target_id is non-None here.)
        assert target_id is not None
        existing = await MaintenanceJobCRUD.get_inflight_for_target(db, kind_value, target_id)
        if existing is None:
            raise RuntimeError(
                f"enqueue hit ON CONFLICT but found no inflight job for {kind_value}/{target_id}"
            )
        logger.info(
            "Maintenance job already inflight — returning existing",
            extra={"job_id": str(existing.id), "kind": kind_value, "target_id": str(target_id)},
        )
        return existing

    @staticmethod
    async def claim_next(db: AsyncSession) -> MaintenanceJob | None:
        return await MaintenanceJobCRUD.claim_next(db)

    @staticmethod
    async def mark_running(db: AsyncSession, job_id: UUID) -> None:
        await MaintenanceJobCRUD.mark_running(db, job_id)

    @staticmethod
    async def mark_done(db: AsyncSession, job_id: UUID) -> None:
        await MaintenanceJobCRUD.mark_done(db, job_id)

    @staticmethod
    async def mark_failed(db: AsyncSession, job_id: UUID, error: str) -> None:
        await MaintenanceJobCRUD.mark_failed(db, job_id, error)

    @staticmethod
    async def update_progress(db: AsyncSession, job_id: UUID, progress: dict[str, Any]) -> None:
        await MaintenanceJobCRUD.update_progress(db, job_id, progress)

    @staticmethod
    async def mark_interrupted_on_restart(db: AsyncSession) -> int:
        count = await MaintenanceJobCRUD.mark_interrupted_on_restart(db)
        if count:
            logger.warning(
                "Marked maintenance jobs as interrupted",
                extra={"count": count},
            )
        return count

    @staticmethod
    async def delete_old_terminal(db: AsyncSession, cutoff: datetime) -> int:
        return await MaintenanceJobCRUD.delete_old_terminal(db, cutoff)
