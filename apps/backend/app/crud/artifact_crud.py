"""
CheckArtifact CRUD - database queries for check artifacts (screenshots, logs, traces).
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import ColumnElement, and_, delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

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
