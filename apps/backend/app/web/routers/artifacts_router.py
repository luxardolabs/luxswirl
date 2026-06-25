"""
Artifacts router — web UI for viewing check artifacts.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Request, Response
from fastapi.responses import HTMLResponse
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_deps import CurrentUserWeb
from app.db import get_db
from app.services.views.artifacts_view_service import ArtifactsViewService
from app.web.templates_config import templates

logger = get_logger("luxswirl.web.routers.artifacts")

router = APIRouter(tags=["Web UI - Artifacts"])


@router.get(
    "/artifacts/{artifact_id}/view",
    response_class=Response,
    include_in_schema=False,
)
async def view_artifact(
    artifact_id: Annotated[UUID, Path(description="Artifact UUID")],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """View/download artifact binary data from web UI."""
    artifact = await ArtifactsViewService.get_artifact_for_download(db, artifact_id)
    if not artifact:
        return Response(
            content=b"Artifact not found",
            status_code=404,
            media_type="text/plain",
        )

    return Response(
        content=artifact.data,
        media_type=artifact.content_type,
        headers={
            "Content-Disposition": f'inline; filename="{artifact.filename}"',
            "Content-Length": str(artifact.size_bytes),
        },
    )


@router.get(
    "/artifacts/{artifact_id}/viewer",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def trace_viewer(
    request: Request,
    artifact_id: Annotated[UUID, Path(description="Artifact UUID")],
    current_user: CurrentUserWeb,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Serve the Playwright trace viewer page."""
    context, error = await ArtifactsViewService.build_trace_viewer_context(
        db, artifact_id, request, current_user
    )
    if error or context is None:
        status_code = 404 if error == "Artifact not found" else 400
        return Response(content=error or "Error", status_code=status_code, media_type="text/plain")

    return templates.TemplateResponse(request, "trace_viewer.html", context)
