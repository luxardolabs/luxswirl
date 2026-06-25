"""CRUD for maintenance_jobs — backend-internal intent rows for cascading mutations."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, delete, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.datetime_utils import utc_now
from app.models.enum_model import MaintenanceJobStatus
from app.models.maintenance_job_model import MaintenanceJob


class MaintenanceJobCRUD:
    @staticmethod
    async def get_by_id(db: AsyncSession, job_id: UUID) -> MaintenanceJob | None:
        result = await db.execute(select(MaintenanceJob).where(MaintenanceJob.id == job_id))
        return result.scalar_one_or_none()

    @staticmethod
    async def get_inflight_for_target(
        db: AsyncSession, kind: str, target_id: UUID
    ) -> MaintenanceJob | None:
        """Return the queued/running job for (kind, target_id), if any."""
        result = await db.execute(
            select(MaintenanceJob).where(
                and_(
                    MaintenanceJob.kind == kind,
                    MaintenanceJob.target_id == target_id,
                    MaintenanceJob.status.in_(
                        [MaintenanceJobStatus.QUEUED.value, MaintenanceJobStatus.RUNNING.value]
                    ),
                )
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def insert(
        db: AsyncSession,
        *,
        kind: str,
        target_id: UUID | None,
        params: dict[str, Any] | None,
        owner_id: UUID | None,
    ) -> MaintenanceJob | None:
        """Insert a queued job idempotently.

        ON CONFLICT DO NOTHING on the partial unique index
        ``(kind, target_id) WHERE status IN ('queued', 'running')`` — the DB
        enforces "at most one inflight job per target", so a double-click or a
        race is a no-op. Returns the inserted row, or None if one already existed.
        """
        stmt = (
            pg_insert(MaintenanceJob)
            .values(
                kind=kind,
                target_id=target_id,
                params=params or {},
                owner_id=owner_id,
                status=MaintenanceJobStatus.QUEUED.value,
            )
            .on_conflict_do_nothing(
                index_elements=["kind", "target_id"],
                index_where=text("status IN ('queued', 'running')"),
            )
            .returning(MaintenanceJob)
        )
        result = await db.execute(stmt)
        return result.scalars().one_or_none()

    @staticmethod
    async def claim_next(db: AsyncSession) -> MaintenanceJob | None:
        """Atomically claim the oldest queued row via FOR UPDATE SKIP LOCKED.

        Caller is responsible for marking running + committing in the same
        transaction so no other worker can claim the row in parallel.
        """
        result = await db.execute(
            select(MaintenanceJob)
            .where(MaintenanceJob.status == MaintenanceJobStatus.QUEUED.value)
            .order_by(MaintenanceJob.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def mark_running(db: AsyncSession, job_id: UUID) -> None:
        await db.execute(
            update(MaintenanceJob)
            .where(MaintenanceJob.id == job_id)
            .values(
                status=MaintenanceJobStatus.RUNNING.value,
                started_at=utc_now(),
            )
        )

    @staticmethod
    async def mark_done(db: AsyncSession, job_id: UUID) -> None:
        await db.execute(
            update(MaintenanceJob)
            .where(MaintenanceJob.id == job_id)
            .values(
                status=MaintenanceJobStatus.DONE.value,
                completed_at=utc_now(),
            )
        )

    @staticmethod
    async def mark_failed(db: AsyncSession, job_id: UUID, error: str) -> None:
        await db.execute(
            update(MaintenanceJob)
            .where(MaintenanceJob.id == job_id)
            .values(
                status=MaintenanceJobStatus.FAILED.value,
                error=error[:2000],
                completed_at=utc_now(),
            )
        )

    @staticmethod
    async def update_progress(db: AsyncSession, job_id: UUID, progress: dict[str, Any]) -> None:
        await db.execute(
            update(MaintenanceJob).where(MaintenanceJob.id == job_id).values(progress=progress)
        )

    @staticmethod
    async def mark_interrupted_on_restart(db: AsyncSession) -> int:
        """Mark any rows left in 'running' state as failed.

        Called at worker startup. The previous process was killed mid-cascade;
        we can't recover partial state safely, so the user has to re-run.
        """
        result = await db.execute(
            update(MaintenanceJob)
            .where(MaintenanceJob.status == MaintenanceJobStatus.RUNNING.value)
            .values(
                status=MaintenanceJobStatus.FAILED.value,
                error="Interrupted by process restart",
                completed_at=utc_now(),
            )
        )
        return result.rowcount or 0  # type: ignore[attr-defined]

    @staticmethod
    async def delete_old_terminal(db: AsyncSession, cutoff: datetime) -> int:
        """Periodic cleanup hook — delete terminal rows older than cutoff."""
        result = await db.execute(
            delete(MaintenanceJob).where(
                and_(
                    or_(
                        MaintenanceJob.status == MaintenanceJobStatus.DONE.value,
                        MaintenanceJob.status == MaintenanceJobStatus.FAILED.value,
                    ),
                    MaintenanceJob.completed_at < cutoff,
                )
            )
        )
        return result.rowcount or 0  # type: ignore[attr-defined]
