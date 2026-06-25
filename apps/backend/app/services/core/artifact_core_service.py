"""
Artifact service - business logic for check artifact operations.
"""

from uuid import UUID

from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.artifact_crud import ArtifactCRUD
from app.models.check_artifact_model import CheckArtifact
from app.schemas.check_artifact_schema import CheckArtifactCreate
from app.services.core.check_result_core_service import CheckResultCoreService

logger = get_logger("luxswirl.services.artifact")


class ArtifactCoreService:
    """Service for check artifact operations."""

    @staticmethod
    async def create_artifact(db: AsyncSession, data: CheckArtifactCreate) -> CheckArtifact:
        """Create a new check artifact.

        Args:
            db: Database session
            data: Artifact creation data (includes base64-encoded binary data)

        Returns:
            Created CheckArtifact instance

        Raises:
            None - artifacts are best-effort, failures should not block check execution
        """
        try:
            # Decode base64 data to bytes
            binary_data = data.get_binary_data()

            # Calculate size
            size_bytes = len(binary_data)

            # Create artifact
            artifact = CheckArtifact(
                check_id=data.check_id,
                check_result_id=data.check_result_id,
                check_result_timestamp=data.check_result_timestamp,
                artifact_type=data.artifact_type,
                content_type=data.content_type,
                filename=data.filename,
                size_bytes=size_bytes,
                data=binary_data,
            )

            db.add(artifact)
            await db.flush()
            await db.refresh(artifact)

            logger.info(
                "Created artifact",
                extra={
                    "artifact_id": str(artifact.id),
                    "artifact_type": artifact.artifact_type,
                    "size_bytes": artifact.size_bytes,
                    "check_id": str(artifact.check_id),
                },
            )

            return artifact

        except Exception:
            logger.error("Failed to create artifact", exc_info=True)
            # Re-raise so caller can handle
            raise

    @staticmethod
    async def get_artifact_by_id(db: AsyncSession, artifact_id: UUID) -> CheckArtifact | None:
        """Get artifact by ID.

        Args:
            db: Database session
            artifact_id: Artifact UUID

        Returns:
            CheckArtifact instance or None if not found
        """
        return await ArtifactCRUD.get_by_id(db, artifact_id)

    @staticmethod
    async def list_artifacts_by_check(
        db: AsyncSession,
        check_id: UUID,
        limit: int = 100,
        include_data: bool = False,
    ) -> list[CheckArtifact]:
        """List artifacts for a specific check.

        Args:
            db: Database session
            check_id: Check UUID
            limit: Maximum number of artifacts to return
            include_data: Whether to load binary data (default False for performance)

        Returns:
            List of CheckArtifact instances
        """
        artifacts = await ArtifactCRUD.list_by_check(db, check_id, limit=limit)

        # If caller doesn't want binary data, clear it to save memory
        if not include_data:
            for artifact in artifacts:
                artifact.data = b""

        return list(artifacts)

    @staticmethod
    async def list_artifacts_by_check_result(
        db: AsyncSession,
        check_id: UUID,
        check_result_id: UUID,
        include_data: bool = False,
    ) -> list[CheckArtifact]:
        """List artifacts for a specific check result (execution).

        Args:
            db: Database session
            check_id: Check UUID
            check_result_id: Check result UUID
            include_data: Whether to load binary data

        Returns:
            List of CheckArtifact instances
        """
        # check_artifacts is a hypertable partitioned by created_at, so the
        # listing query is bounded on it — source the result's timestamp first.
        check_result = await CheckResultCoreService.get_check_result_by_id(db, check_result_id)
        if check_result is None:
            return []
        artifacts = await ArtifactCRUD.list_by_check_result(
            db, check_id, check_result_id, check_result.timestamp
        )

        # If caller doesn't want binary data, clear it
        if not include_data:
            for artifact in artifacts:
                artifact.data = b""

        return list(artifacts)

    @staticmethod
    async def delete_artifact(db: AsyncSession, artifact_id: UUID) -> bool:
        """Delete an artifact.

        Args:
            db: Database session
            artifact_id: Artifact UUID

        Returns:
            True if deleted, False if not found
        """
        deleted = (await ArtifactCRUD.delete_by_id(db, artifact_id)) > 0

        if deleted:
            logger.info(
                "Deleted artifact",
                extra={"artifact_id": str(artifact_id)},
            )
        else:
            logger.warning(
                "Artifact not found for deletion",
                extra={"artifact_id": str(artifact_id)},
            )

        return deleted

    @staticmethod
    async def delete_old_artifacts(
        db: AsyncSession,
        check_id: UUID,
        keep_count: int = 10,
    ) -> int:
        """Delete old artifacts, keeping only the most recent N.

        Args:
            db: Database session
            check_id: Check UUID
            keep_count: Number of most recent artifacts to keep

        Returns:
            Number of artifacts deleted
        """
        keep_ids = await ArtifactCRUD.get_recent_ids_for_check(db, check_id, keep_count)
        deleted_count = await ArtifactCRUD.delete_for_check_excluding(db, check_id, keep_ids)

        if deleted_count > 0:
            logger.info(
                "Deleted old artifacts for check",
                extra={
                    "deleted_count": deleted_count,
                    "check_id": str(check_id),
                    "kept_count": len(keep_ids),
                },
            )

        return deleted_count

    @staticmethod
    async def get_artifact_stats(db: AsyncSession, check_id: UUID) -> dict:
        """Get statistics about artifacts for a check.

        Args:
            db: Database session
            check_id: Check UUID

        Returns:
            Dictionary with count and total size
        """

        row = await ArtifactCRUD.get_stats_for_check(db, check_id)
        return {
            "count": row.artifact_count or 0,
            "total_size_bytes": row.total_size or 0,
        }
