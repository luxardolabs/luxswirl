"""
Artifacts view service — context building for the artifacts web UI.

Mediates between web routers and `ArtifactCoreService` core, plus assembles the
template context for the trace viewer (filename, size formatting).
"""

from typing import Any
from uuid import UUID

from fastapi import Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.content_disposition import content_disposition
from app.schemas.auth_schema import AuthedUser
from app.services.core.artifact_core_service import ArtifactCoreService
from app.shared.logger import get_logger

logger = get_logger("luxswirl.web.services.artifacts")


class ArtifactsViewService:
    """View-layer wrapper for artifact endpoints."""

    @staticmethod
    async def build_artifact_download_response(
        db: AsyncSession, artifact_id: UUID
    ) -> Response:
        """Serve an artifact's bytes inline, or a 404 body if it is gone."""
        artifact = await ArtifactCoreService.get_artifact_content(db, artifact_id)
        if artifact is None:
            return Response(
                content=b"Artifact not found",
                status_code=404,
                media_type="text/plain",
            )
        return Response(
            content=artifact.data,
            media_type=artifact.content_type,
            headers={
                "Content-Disposition": content_disposition("inline", artifact.filename),
                "Content-Length": str(artifact.size_bytes),
            },
        )

    @staticmethod
    async def build_trace_viewer_context(
        db: AsyncSession,
        artifact_id: UUID,
        request: Request,
        current_user: AuthedUser,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """
        Build template context for the Playwright trace viewer.

        Returns (context, error). On error, context is None and error is a
        human-readable string the router can surface as a 4xx response body.
        """
        artifact = await ArtifactCoreService.get_artifact(db, artifact_id)
        if artifact is None:
            return None, "Artifact not found"
        if artifact.artifact_type != "trace":
            return None, "This viewer only supports Playwright trace files"

        context = {
            "request": request,
            "current_user": current_user,
            "artifact_id": str(artifact_id),
            "filename": artifact.filename,
            "size_kb": round(artifact.size_bytes / 1024, 1),
            # .path, not str(): url_for returns an absolute url from the
            # request's scheme/host, which behind TLS-terminating nginx can emit
            # http:// into an https:// page.
            "download_url": request.url_for(
                "artifacts.show", artifact_id=artifact_id
            ).path,
        }
        return context, None
