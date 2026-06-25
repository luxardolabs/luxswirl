"""
Agents router — web UI for agent management.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi import status as http_status
from fastapi.responses import HTMLResponse, JSONResponse
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_deps import (
    CurrentUserWeb,
    EditorUserWeb,
)
from app.core.exceptions import AgentNotFoundException
from app.db import get_db
from app.services.views.agents_view_service import AgentsViewService
from app.web._hx_responses import hx_empty_with_toast
from app.web.routers._render import status_message
from app.web.templates_config import templates

logger = get_logger("luxswirl.web.routers.agents")

router = APIRouter(tags=["Web UI - Agents"])


# ---- Page / partials ----------------------------------------------------


@router.get("/agents", response_class=HTMLResponse, include_in_schema=False)
async def agents_list_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUserWeb,
    active_only: Annotated[bool, Query(description="Show only active agents")] = False,
    search: Annotated[str, Query(description="Search agents by name, hostname, or IP")] = "",
    hours: Annotated[int, Query(ge=1, le=24, description="Hours of metrics history")] = 4,
    page: Annotated[int, Query(ge=1, description="Page number")] = 1,
    per_page: Annotated[
        int | None, Query(ge=10, le=200, description="Items per page (defaults to setting)")
    ] = None,
):
    """Agents list page — paginated."""
    try:
        context = await AgentsViewService.build_agents_list_context(
            db, request, current_user, active_only, search, hours, page, per_page
        )
        return templates.TemplateResponse(request, "pages/agents.html", context)
    except Exception as e:
        logger.error("Error rendering agents page", exc_info=True)
        return templates.TemplateResponse(
            request,
            "pages/error.html",
            {
                "current_user": current_user,
                "error": str(e),
                "page_title": "Error",
            },
            status_code=500,
        )


@router.get("/agents/{agent_id}/edit-form", response_class=HTMLResponse, include_in_schema=False)
async def agent_edit_form(
    agent_id: UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUserWeb,
):
    """Get agent edit form for the side panel."""
    try:
        context = await AgentsViewService.build_edit_form_context(
            db, request, current_user, agent_id
        )
        if context is None:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=f"Agent not found: {agent_id}",
            )
        return templates.TemplateResponse(request, "panels/agents/agent_edit_panel.html", context)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error loading agent edit form", exc_info=True)
        return HTMLResponse(
            content=f'<div class="alert alert-error">Error loading edit form: {e}</div>',
            status_code=500,
        )


# ---- Mutations ----------------------------------------------------------


@router.patch("/agents/{agent_id}", response_class=HTMLResponse, include_in_schema=False)
async def update_agent_web(
    agent_id: UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
    agent_name: Annotated[str | None, Form()] = None,
    hostname: Annotated[str | None, Form()] = None,
    tags: Annotated[str | None, Form()] = None,
    heartbeat_interval: Annotated[str | None, Form()] = None,
    check_sync_interval: Annotated[str | None, Form()] = None,
    report_interval: Annotated[str | None, Form()] = None,
    report_batch_size: Annotated[str | None, Form()] = None,
    report_max_files_per_batch: Annotated[str | None, Form()] = None,
    report_process_interval: Annotated[str | None, Form()] = None,
    report_max_queue_size: Annotated[str | None, Form()] = None,
    report_backpressure_threshold: Annotated[str | None, Form()] = None,
    max_concurrent_checks: Annotated[str | None, Form()] = None,
    watchdog_interval: Annotated[str | None, Form()] = None,
    watchdog_stall_threshold: Annotated[str | None, Form()] = None,
    log_level: Annotated[str | None, Form()] = None,
):
    """Update agent via web form."""
    kind, message, status = await AgentsViewService.handle_update_agent_form(
        db,
        agent_id,
        {
            "agent_name": agent_name,
            "hostname": hostname,
            "tags": tags,
            "heartbeat_interval": heartbeat_interval,
            "check_sync_interval": check_sync_interval,
            "report_interval": report_interval,
            "report_batch_size": report_batch_size,
            "report_max_files_per_batch": report_max_files_per_batch,
            "report_process_interval": report_process_interval,
            "report_max_queue_size": report_max_queue_size,
            "report_backpressure_threshold": report_backpressure_threshold,
            "max_concurrent_checks": max_concurrent_checks,
            "watchdog_interval": watchdog_interval,
            "watchdog_stall_threshold": watchdog_stall_threshold,
            "log_level": log_level,
        },
    )
    return status_message(request, "partials/agents/status_message.html", kind, message, status)


@router.delete("/agents/{agent_id}", response_class=HTMLResponse, include_in_schema=False)
async def delete_agent_web(
    request: Request,
    agent_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
):
    """Enqueue an agent_delete maintenance job and return the polling partial.

    See LUXSWIRL-105. The actual cascade (checks → check_results across
    compressed Timescale chunks) runs in the maintenance worker on its own
    session so this web request never holds a transaction longer than the
    insert. UI polls /maintenance/{job_id}/status until terminal.
    """
    # No explicit commit/rollback: get_db() commits on clean return (before the
    # polling partial is sent, so the job is visible to the first poll) and rolls
    # back on any exception. Same pattern as LUXSWIRL-164.
    try:
        job = await AgentsViewService.enqueue_delete(db, agent_id, owner_id=current_user.id)
    except AgentNotFoundException:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {agent_id}",
        ) from None
    except Exception as e:
        logger.error("Error enqueuing agent delete", exc_info=True)
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to enqueue agent delete: {e}",
        ) from e

    logger.info(
        "Enqueued agent_delete maintenance job",
        extra={"agent_id": agent_id, "job_id": str(job.id)},
    )
    return templates.TemplateResponse(
        request,
        "partials/maintenance/job_status.html",
        {"job": job, "request": request, "current_user": current_user},
    )


@router.post("/agents/{agent_id}/force-reload", include_in_schema=False)
async def force_reload_agent(
    agent_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
):
    """Force agent to reload checks by updating checks_updated_at timestamp."""
    try:
        await AgentsViewService.force_reload(db, agent_id)
        logger.info(
            "Forced config reload for agent",
            extra={"agent_id": str(agent_id)},
        )
        return JSONResponse({"success": True, "message": "Config reload triggered"})
    except Exception as e:
        logger.error("Error forcing agent reload", exc_info=True)
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to force reload: {e}",
        ) from e


# ---- Approval workflow (HTMX) -------------------------------------------


@router.post("/agents/{agent_id}/approve-web", include_in_schema=False)
async def approve_agent_web(
    agent_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
):
    """Approve agent via web UI (auto-generates API key)."""
    try:
        agent, _api_key = await AgentsViewService.approve_agent(db, agent_id)
        display_name = agent.agent_name or agent.hostname or str(agent.id)
        logger.info(
            "Agent approved via web (API key auto-generated)",
            extra={"agent_id": str(agent_id)},
        )
        return hx_empty_with_toast(
            f"Agent '{display_name}' approved successfully",
            extra_events={"refreshPage": {}},
        )
    except Exception:
        logger.error("Error approving agent", exc_info=True)
        raise


@router.post("/agents/{agent_id}/reject-web", include_in_schema=False)
async def reject_agent_web(
    agent_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
    reason: Annotated[str | None, Query()] = None,
):
    """Reject agent via web UI."""
    agent = await AgentsViewService.reject_agent(db, agent_id, reason)
    logger.info(
        "Agent rejected via web",
        extra={"agent_id": str(agent_id)},
    )
    pending_count = await AgentsViewService.get_pending_count(db)
    return hx_empty_with_toast(
        f"Agent '{agent.agent_name or agent.hostname}' rejected",
        extra_events={"updatePendingCount": {"count": pending_count}},
    )


@router.post("/agents/{agent_id}/pause-web", include_in_schema=False)
async def pause_agent_web(
    agent_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
    reason: Annotated[str | None, Query()] = None,
):
    await AgentsViewService.pause_agent(db, agent_id, reason)
    logger.info(
        "Agent paused successfully",
        extra={"agent_id": str(agent_id)},
    )
    return JSONResponse({"success": True})


@router.post("/agents/{agent_id}/resume-web", include_in_schema=False)
async def resume_agent_web(
    agent_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
):
    await AgentsViewService.resume_agent(db, agent_id)
    return JSONResponse({"success": True})


@router.post("/agents/{agent_id}/disable-web", include_in_schema=False)
async def disable_agent_web(
    agent_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
    reason: Annotated[str | None, Query()] = None,
):
    await AgentsViewService.disable_agent(db, agent_id, reason)
    return JSONResponse({"success": True})


@router.post("/agents/{agent_id}/enable-web", include_in_schema=False)
async def enable_agent_web(
    agent_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
):
    await AgentsViewService.enable_agent(db, agent_id)
    return JSONResponse({"success": True})


# ---- Key management (HTMX) ----------------------------------------------


@router.get(
    "/agents/{agent_id}/key-management-panel",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def get_key_management_panel(
    agent_id: UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUserWeb,
):
    """Get agent key management panel."""
    try:
        context = await AgentsViewService.build_key_management_panel_context(
            db, request, current_user, agent_id
        )
        if context is None:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=f"Agent not found: {agent_id}",
            )
        return templates.TemplateResponse(
            request, "panels/registration_keys/agent_key_management_panel.html", context
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error loading key management panel", exc_info=True)
        return HTMLResponse(
            content=f'<div class="alert alert-error">Error loading panel: {e}</div>',
            status_code=500,
        )


@router.post(
    "/agents/{agent_id}/generate-key-web",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def generate_key_web(
    agent_id: UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
):
    """Generate initial API key for agent (if none exists)."""
    try:
        agent, plaintext_key = await AgentsViewService.generate_agent_key(db, agent_id)
        return templates.TemplateResponse(
            request,
            "panels/registration_keys/agent_key_generated_panel.html",
            AgentsViewService.build_key_generated_panel_context(
                request, current_user, agent, plaintext_key
            ),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except AgentNotFoundException:
        raise HTTPException(status_code=404, detail="Agent not found") from None
    except Exception as e:
        logger.error("Error generating key", exc_info=True)
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate key: {e}",
        ) from e


@router.post(
    "/agents/{agent_id}/regenerate-key-web",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def regenerate_key_web(
    agent_id: UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
):
    """Regenerate API key (revokes old)."""
    try:
        agent, plaintext_key = await AgentsViewService.regenerate_agent_key(db, agent_id)
        return templates.TemplateResponse(
            request,
            "panels/registration_keys/agent_key_generated_panel.html",
            AgentsViewService.build_key_generated_panel_context(
                request, current_user, agent, plaintext_key
            ),
        )
    except AgentNotFoundException:
        raise HTTPException(status_code=404, detail="Agent not found") from None
    except Exception as e:
        logger.error("Error regenerating key", exc_info=True)
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to regenerate key: {e}",
        ) from e


@router.delete("/agents/{agent_id}/revoke-key-web", include_in_schema=False)
async def revoke_key_web(
    agent_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
):
    """Revoke agent API key."""
    try:
        await AgentsViewService.revoke_agent_key(db, agent_id)
        return JSONResponse({"success": True, "message": "API key revoked successfully"})
    except AgentNotFoundException:
        raise HTTPException(status_code=404, detail="Agent not found") from None
    except Exception as e:
        logger.error("Error revoking key", exc_info=True)
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to revoke key: {e}",
        ) from e
