"""
CheckArtifact CRUD - database queries for check artifacts (screenshots, logs, traces).
"""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, delete, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.check_artifact_model import CheckArtifact


class ArtifactCRUD:
    """Database queries for check artifacts."""

    @staticmethod
    async def delete_older_than(db: AsyncSession, cutoff: datetime) -> int:
        """Delete artifacts created before cutoff. Returns rowcount."""
        result = await db.execute(delete(CheckArtifact).where(CheckArtifact.created_at < cutoff))
        return result.rowcount or 0  # type: ignore[attr-defined]

    @staticmethod
    async def get_by_id(db: AsyncSession, artifact_id: UUID) -> CheckArtifact | None:
        result = await db.execute(select(CheckArtifact).where(CheckArtifact.id == artifact_id))
        return result.scalar_one_or_none()

    @staticmethod
    async def list_by_check(
        db: AsyncSession, check_id: UUID, limit: int = 50
    ) -> list[CheckArtifact]:
        """Most recent artifacts for a check."""
        result = await db.execute(
            select(CheckArtifact)
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
        result = await db.execute(delete(CheckArtifact).where(CheckArtifact.id == artifact_id))
        return result.rowcount or 0  # type: ignore[attr-defined]

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
        if keep_ids:
            stmt = delete(CheckArtifact).where(
                and_(
                    CheckArtifact.check_id == check_id,
                    CheckArtifact.id.not_in(keep_ids),
                )
            )
        else:
            stmt = delete(CheckArtifact).where(CheckArtifact.check_id == check_id)
        result = await db.execute(stmt)
        return result.rowcount or 0  # type: ignore[attr-defined]

    @staticmethod
    async def get_stats_for_check(db: AsyncSession, check_id: UUID):
        """Aggregate (artifact_count, total_size) for a check."""
        result = await db.execute(
            select(
                func.count(CheckArtifact.id).label("artifact_count"),
                func.sum(CheckArtifact.size_bytes).label("total_size"),
            ).where(CheckArtifact.check_id == check_id)
        )
        return result.one()
