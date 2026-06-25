"""
Artifacts view service — context building for the artifacts web UI.

Mediates between web routers and `ArtifactCoreService` core, plus assembles the
template context for the trace viewer (filename, size formatting).
"""

from typing import Any
from uuid import UUID

from fastapi import Request
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user_model import User
from app.services.core.artifact_core_service import ArtifactCoreService

logger = get_logger("luxswirl.web.services.artifacts")


class ArtifactsViewService:
    """View-layer wrapper for artifact endpoints."""

    @staticmethod
    async def get_artifact_for_download(db: AsyncSession, artifact_id: UUID):
        """Return the raw artifact or None if missing. Router handles response shape."""
        return await ArtifactCoreService.get_artifact_by_id(db, artifact_id)

    @staticmethod
    async def build_trace_viewer_context(
        db: AsyncSession,
        artifact_id: UUID,
        request: Request,
        current_user: User,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """
        Build template context for the Playwright trace viewer.

        Returns (context, error). On error, context is None and error is a
        human-readable string the router can surface as a 4xx response body.
        """
        artifact = await ArtifactCoreService.get_artifact_by_id(db, artifact_id)
        if not artifact:
            return None, "Artifact not found"
        if artifact.artifact_type != "trace":
            return None, "This viewer only supports Playwright trace files"

        context = {
            "request": request,
            "current_user": current_user,
            "artifact_id": str(artifact_id),
            "filename": artifact.filename,
            "size_kb": round(artifact.size_bytes / 1024, 1),
        }
        return context, None
