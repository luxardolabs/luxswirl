"""
Artifact service - business logic for check artifact operations.
"""

from uuid import UUID

from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthorizationException, CheckNotFoundException
from app.crud.artifact_crud import ArtifactCRUD
from app.models.check_artifact_model import CheckArtifact
from app.schemas.check_artifact_schema import (
    CheckArtifactContent,
    CheckArtifactCreate,
    CheckArtifactResponse,
)
from app.services.core.agent_assignment_core_service import AgentAssignmentCoreService
from app.services.core.check_core_service import CheckCoreService

logger = get_logger("luxswirl.services.artifact")


class ArtifactCoreService:
    """Service for check artifact operations."""

    @staticmethod
    async def create_artifact(
        db: AsyncSession, agent_id: UUID, data: CheckArtifactCreate
    ) -> CheckArtifactResponse:
        """Store an artifact one of `agent_id`'s check executions produced.

        The check must be one the assignment model gives that agent (its manual
        check, or a replicate/distribute check its selector matches), the same
        set the agent is handed by GET /api/v1/checks.

        Args:
            db: Database session
            agent_id: The authenticated uploading agent
            data: Artifact creation data (includes base64-encoded binary data)

        Returns:
            The stored artifact's metadata

        Raises:
            CheckNotFoundException: If the artifact's check does not exist
            AuthorizationException: If the check is not assigned to the agent
        """
        await ArtifactCoreService._require_check(db, data.check_id)
        if not await AgentAssignmentCoreService.is_check_assigned(
            db, agent_id, data.check_id
        ):
            logger.warning(
                "Refused artifact for a check not assigned to the uploading agent",
                extra={"agent_id": str(agent_id), "check_id": str(data.check_id)},
            )
            raise AuthorizationException(
                "This check is not assigned to the uploading agent"
            )
        binary_data = data.get_binary_data()
        artifact = CheckArtifact(
            check_id=data.check_id,
            check_result_id=data.check_result_id,
            check_result_timestamp=data.check_result_timestamp,
            artifact_type=data.artifact_type,
            content_type=data.content_type,
            filename=data.filename,
            size_bytes=len(binary_data),
            data=binary_data,
        )
        artifact = await ArtifactCRUD.add_artifact(db, artifact)
        logger.info(
            "Created artifact",
            extra={
                "artifact_id": str(artifact.id),
                "artifact_type": artifact.artifact_type,
                "size_bytes": artifact.size_bytes,
                "check_id": str(artifact.check_id),
                "agent_id": str(agent_id),
            },
        )
        return ArtifactCoreService._metadata(artifact)

    @staticmethod
    def _metadata(artifact: CheckArtifact) -> CheckArtifactResponse:
        """The artifact's metadata in its API shape (never its bytes)."""
        return CheckArtifactResponse(
            id=artifact.id,
            check_id=artifact.check_id,
            check_result_id=artifact.check_result_id,
            check_result_timestamp=artifact.check_result_timestamp,
            artifact_type=artifact.artifact_type,
            content_type=artifact.content_type,
            filename=artifact.filename,
            size_bytes=artifact.size_bytes,
            created_at=artifact.created_at,
        )

    @staticmethod
    async def get_artifact(
        db: AsyncSession, artifact_id: UUID
    ) -> CheckArtifactResponse | None:
        """An artifact's metadata, or None if there is no such artifact."""
        artifact = await ArtifactCRUD.get_by_id(db, artifact_id)
        return None if artifact is None else ArtifactCoreService._metadata(artifact)

    @staticmethod
    async def get_artifact_content(
        db: AsyncSession, artifact_id: UUID
    ) -> CheckArtifactContent | None:
        """An artifact with its bytes, or None if there is no such artifact."""
        artifact = await ArtifactCRUD.get_by_id(db, artifact_id)
        if artifact is None:
            return None
        return CheckArtifactContent(
            **dict(ArtifactCoreService._metadata(artifact)), data=artifact.data
        )

    @staticmethod
    async def _require_check(db: AsyncSession, check_id: UUID) -> None:
        if await CheckCoreService.get_check_by_id(db, check_id) is None:
            raise CheckNotFoundException("unknown", str(check_id))

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
        deleted_count = await ArtifactCRUD.delete_for_check_excluding(
            db, check_id, keep_ids
        )

        if deleted_count > 0:
            logger.info(
                "Deleted old artifacts for check",
                extra={
                    "deleted_count": deleted_count,
                    "check_id": str(check_id),
                    # post-db-filter: log field: keep_ids is the bounded keep-list the delete excluded
                    "kept_count": len(keep_ids),
                },
            )

        return deleted_count
