"""
Import/Export web router - web UI for bulk import/export of checks.
"""

import json
from datetime import datetime
from io import BytesIO
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Path, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_deps import AdminUserWeb
from app.core.exceptions import AgentNotFoundException
from app.db import get_db
from app.services.views.import_export_view_service import ImportExportViewService
from app.web.templates_config import templates

logger = get_logger("luxswirl.web.import_export")

router = APIRouter(tags=["Web UI - Import/Export"])


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
        # Get agent info
        agent = await ImportExportViewService.get_agent_by_id(db, agent_id)
        if not agent:
            raise AgentNotFoundException(str(agent_id))

        # Get check count
        checks = await ImportExportViewService.list_checks_for_agent(db, agent_id)
        check_count = len(checks)

        return templates.TemplateResponse(
            request,
            "partials/import_export_panel.html",
            {
                "current_user": current_user,
                "agent": agent,
                "agent_id": agent_id,
                "check_count": check_count,
            },
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
    except Exception as e:
        logger.error("Error loading import/export form", exc_info=True)
        return templates.TemplateResponse(
            request,
            "partials/error_message.html",
            {
                "current_user": current_user,
                "error": str(e),
            },
            status_code=500,
        )


@router.get("/agents/{agent_id}/export", include_in_schema=False)
async def export_checks(
    agent_id: Annotated[UUID, Path(description="Agent ID")],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Export all checks for an agent as JSON download.
    """
    try:
        # Verify agent exists
        agent = await ImportExportViewService.get_agent_by_id(db, agent_id)
        if not agent:
            raise AgentNotFoundException(str(agent_id))

        # Get all checks for agent
        checks = await ImportExportViewService.list_checks_for_agent(db, agent_id)

        # Convert to export format using service
        export_data = ImportExportViewService.export_checks_to_dict(checks, agent)

        # Create JSON file
        json_str = json.dumps(export_data, indent=2)
        json_bytes = BytesIO(json_str.encode())

        # Return as download with timestamp
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"{agent_id}-checks-{timestamp}.json"
        return StreamingResponse(
            json_bytes,
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    except AgentNotFoundException:
        return JSONResponse(
            status_code=404,
            content={"error": f"Agent '{agent_id}' not found"},
        )
    except Exception as e:
        logger.error("Error exporting checks", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": str(e)},
        )


@router.post("/agents/{agent_id}/import", response_class=HTMLResponse, include_in_schema=False)
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
