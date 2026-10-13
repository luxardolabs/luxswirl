"""
CheckArtifact CRUD - database queries for check artifacts (screenshots, logs, traces).
"""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import ColumnElement, and_, delete, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.models.check_artifact_model import CheckArtifact


async def _delete_counted(db: AsyncSession, criteria: ColumnElement[bool]) -> int:
    """Delete the matching artifacts and return how many went.

    Counts the ids DELETE ... RETURNING hands back, so the count is typed rather
    than the driver's untyped `rowcount`. Artifacts are retention-bounded, so the
    id list stays small.
    """
    result = await db.execute(
        delete(CheckArtifact).where(criteria).returning(CheckArtifact.id)
    )
    return len(result.scalars().all())


class ArtifactCRUD:
    """Database queries for check artifacts."""

    # ── Writes (flushed; the session owner commits) ───────────────────────────

    @staticmethod
    async def add_artifact(db: AsyncSession, artifact: CheckArtifact) -> CheckArtifact:
        db.add(artifact)
        await db.flush()
        await db.refresh(artifact)
        return artifact

    @staticmethod
    async def delete_older_than(db: AsyncSession, cutoff: datetime) -> int:
        """Delete artifacts created before cutoff. Returns how many were deleted."""
        return await _delete_counted(db, CheckArtifact.created_at < cutoff)

    @staticmethod
    async def get_by_id(db: AsyncSession, artifact_id: UUID) -> CheckArtifact | None:
        result = await db.execute(
            select(CheckArtifact).where(CheckArtifact.id == artifact_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def list_by_check(
        db: AsyncSession, check_id: UUID, limit: int = 50
    ) -> list[CheckArtifact]:
        """Most recent artifacts for a check, without their bytes (`data` raises)."""
        result = await db.execute(
            select(CheckArtifact)
            .options(defer(CheckArtifact.data, raiseload=True))
            .where(CheckArtifact.check_id == check_id)
            .order_by(desc(CheckArtifact.created_at))
            .limit(limit)
        )
        return list(result.scalars().all())

    @staticmethod
    async def list_by_check_result(
        db: AsyncSession,
        check_id: UUID,
        check_result_id: UUID,
        check_result_timestamp: datetime,
    ) -> list[CheckArtifact]:
        """Artifacts for a specific check execution.

        check_artifacts is a hypertable partitioned by created_at, so the scan is
        bounded to a ±1-day window around the result's timestamp for chunk
        exclusion (artifacts are written within seconds of the result, so the
        window is result-identical).
        """
        window = timedelta(days=1)
        result = await db.execute(
            select(CheckArtifact)
            .options(defer(CheckArtifact.data, raiseload=True))
            .where(
                and_(
                    CheckArtifact.check_id == check_id,
                    CheckArtifact.check_result_id == check_result_id,
                    CheckArtifact.created_at >= check_result_timestamp - window,
                    CheckArtifact.created_at <= check_result_timestamp + window,
                )
            )
            .order_by(desc(CheckArtifact.created_at))
        )
        return list(result.scalars().all())

    @staticmethod
    async def delete_by_id(db: AsyncSession, artifact_id: UUID) -> int:
        return await _delete_counted(db, CheckArtifact.id == artifact_id)

    @staticmethod
    async def get_recent_ids_for_check(
        db: AsyncSession, check_id: UUID, keep_count: int
    ) -> list[UUID]:
        """IDs of the most recent N artifacts for a check."""
        result = await db.execute(
            select(CheckArtifact.id)
            .where(CheckArtifact.check_id == check_id)
            .order_by(desc(CheckArtifact.created_at))
            .limit(keep_count)
        )
        return [row[0] for row in result.fetchall()]

    @staticmethod
    async def delete_for_check_excluding(
        db: AsyncSession, check_id: UUID, keep_ids: list[UUID]
    ) -> int:
        """Delete all artifacts for a check except those in keep_ids."""
        criteria = CheckArtifact.check_id == check_id
        if keep_ids:
            criteria = and_(criteria, CheckArtifact.id.not_in(keep_ids))
        return await _delete_counted(db, criteria)

    @staticmethod
    async def get_stats_for_check(db: AsyncSession, check_id: UUID) -> tuple[int, int]:
        """(artifact_count, total_size_bytes) for a check; 0 bytes when it has none."""
        result = await db.execute(
            select(
                func.count(CheckArtifact.id),
                func.coalesce(func.sum(CheckArtifact.size_bytes), 0),
            ).where(CheckArtifact.check_id == check_id)
        )
        count, total_size = result.one()
        return count, total_size
