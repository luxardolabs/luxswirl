"""
Import/Export web router - web UI for bulk import/export of checks.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Path, Request, UploadFile
from fastapi.responses import HTMLResponse
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AgentNotFoundException
from app.db import get_db
from app.services.views.import_export_view_service import ImportExportViewService
from app.web.deps import AdminUserWeb
from app.web.templates import templates

logger = get_logger("luxswirl.web.import_export")

router = APIRouter(tags=["Web UI - Import/Export"])


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.get(
    "/agents/{agent_id}/import-export-form",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def import_export_form(
    request: Request,
    agent_id: Annotated[UUID, Path(description="Agent ID")],
    current_user: AdminUserWeb,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Get the import/export form for an agent.
    """
    try:
        return templates.TemplateResponse(
            request,
            "partials/import_export_panel.html",
            await ImportExportViewService.build_panel_context(
                db, request, current_user, agent_id
            ),
        )

    except AgentNotFoundException:
        return templates.TemplateResponse(
            request,
            "partials/error_message.html",
            {
                "current_user": current_user,
                "error": f"Agent '{agent_id}' not found",
            },
            status_code=404,
        )


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.get(
    "/agents/{agent_id}/export", include_in_schema=False, name="web_export_checks"
)
async def export_checks(
    agent_id: Annotated[UUID, Path(description="Agent ID")],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Export all checks for an agent as JSON download.

    AgentNotFoundException (a LuxSwirlException) propagates to the global handler,
    which renders it content-negotiated — the router does not hand-roll an error body.
    """
    return await ImportExportViewService.build_export_download(db, agent_id)


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.post(
    "/agents/{agent_id}/import",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="web_import_checks",
)
async def import_checks(
    request: Request,
    agent_id: Annotated[UUID, Path(description="Agent ID")],
    file: Annotated[UploadFile, File()],
    mode: Annotated[str, Form()],  # "merge" or "replace"
    current_user: AdminUserWeb,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Enqueue a bulk_check_import maintenance job; return the polling partial.

    File parsing + structural validation stay in the request (fast — kilobytes).
    The actual import (which for mode=replace cascades through check_results)
    runs in the worker. See LUXSWIRL-105.
    """
    content = await file.read()
    return await ImportExportViewService.handle_import(
        db, request, current_user, agent_id, content, mode
    )
