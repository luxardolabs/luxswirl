"""Maintenance jobs router — status polling partial for backend cascading mutations.

NOT to be confused with the user-facing Jobs page (`jobs_router`). These rows
are backend-internal intent rows; the only UI surface is the polling partial
returned by GET /maintenance/{id}/status, which whichever page enqueued the
job has embedded inline.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import HTMLResponse
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_deps import CurrentUserWeb
from app.core.exceptions import NotFoundException
from app.db import get_db
from app.services.views.maintenance_job_view_service import MaintenanceJobViewService
from app.web.templates_config import templates

logger = get_logger("luxswirl.web.routers.maintenance")

router = APIRouter(tags=["Web UI - Maintenance"])


@router.get(
    "/maintenance/{job_id}/status",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def maintenance_job_status(
    request: Request,
    job_id: Annotated[UUID, Path(description="Maintenance job UUID")],
    current_user: CurrentUserWeb,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Render the polling partial. Returns 404 if the row was reaped."""
    try:
        context = await MaintenanceJobViewService.build_status_partial_context(
            db, request, current_user, job_id
        )
    except NotFoundException:
        return HTMLResponse("", status_code=404)

    return templates.TemplateResponse(
        request,
        "partials/maintenance/job_status.html",
        context,
    )
