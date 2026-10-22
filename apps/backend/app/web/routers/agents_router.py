"""
Agents router — web UI for agent management.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi import status as http_status
from fastapi.responses import HTMLResponse, Response
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AgentNotFoundException
from app.core.query_params import PerPageFilter
from app.db import get_db
from app.services.views.agents_view_service import AgentsViewService
from app.services.views.maintenance_job_view_service import MaintenanceJobViewService
from app.web.deps import CurrentUserWeb, EditorUserWeb
from app.web.render import agent_status_message
from app.web.templates import templates

logger = get_logger("luxswirl.web.routers.agents")

router = APIRouter(tags=["Web UI - Agents"])


# ---- Page / partials ----------------------------------------------------


@router.get(
    "/agents",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="agents_list_page",
)
async def agents_list_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: CurrentUserWeb,
    active_only: Annotated[bool, Query(description="Show only active agents")] = False,
    search: Annotated[
        str, Query(description="Search agents by name, hostname, or IP")
    ] = "",
    hours: Annotated[
        int, Query(ge=1, le=24, description="Hours of metrics history")
    ] = 4,
    page: Annotated[int, Query(ge=1, description="Page number")] = 1,
    per_page: PerPageFilter = None,
) -> HTMLResponse:
    """Agents list page — paginated."""
    context = await AgentsViewService.build_agents_list_context(
        db, request, current_user, active_only, search, hours, page, per_page
    )
    return templates.TemplateResponse(request, "pages/agents.html", context)


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.get(
    "/agents/{agent_id}/edit-form",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="agent_edit_form",
)
async def agent_edit_form(
    agent_id: UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: CurrentUserWeb,
) -> HTMLResponse:
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
        return templates.TemplateResponse(
            request, "partials/panels/agents/agent_edit_panel.html", context
        )
    except HTTPException:
        raise


# ---- Mutations ----------------------------------------------------------


@router.patch(
    "/agents/{agent_id}",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="update_agent_web",
)
async def update_agent_web(
    agent_id: UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
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
) -> Response:
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
    return agent_status_message(request, kind, message, status)


@router.delete(
    "/agents/{agent_id}",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="delete_agent_web",
)
async def delete_agent_web(
    request: Request,
    agent_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: EditorUserWeb,
) -> HTMLResponse:
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
        job = await AgentsViewService.enqueue_delete(
            db, agent_id, owner_id=current_user.id
        )
    except AgentNotFoundException:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {agent_id}",
        ) from None
    except Exception as e:
        logger.exception("Error enqueuing agent delete")
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
        MaintenanceJobViewService.build_status_context(request, current_user, job),
    )


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.post(
    "/agents/{agent_id}/force-reload",
    include_in_schema=False,
    name="force_reload_agent",
)
async def force_reload_agent(
    agent_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: EditorUserWeb,
) -> Response:
    """Force agent to reload checks by updating checks_updated_at timestamp."""
    return await AgentsViewService.force_reload(db, agent_id)


# ---- Approval workflow (HTMX) -------------------------------------------


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.post(
    "/agents/{agent_id}/approve-web", include_in_schema=False, name="approve_agent_web"
)
async def approve_agent_web(
    agent_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: EditorUserWeb,
) -> Response:
    """Approve agent via web UI (auto-generates API key)."""
    return await AgentsViewService.approve_agent(db, agent_id)


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.post(
    "/agents/{agent_id}/reject-web", include_in_schema=False, name="reject_agent_web"
)
async def reject_agent_web(
    agent_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: EditorUserWeb,
    reason: Annotated[str | None, Query()] = None,
) -> Response:
    """Reject agent via web UI."""
    return await AgentsViewService.reject_agent(db, agent_id, reason)


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.post(
    "/agents/{agent_id}/pause-web", include_in_schema=False, name="pause_agent_web"
)
async def pause_agent_web(
    agent_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: EditorUserWeb,
    reason: Annotated[str | None, Query()] = None,
) -> Response:
    return await AgentsViewService.pause_agent(db, agent_id, reason)


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.post(
    "/agents/{agent_id}/resume-web", include_in_schema=False, name="resume_agent_web"
)
async def resume_agent_web(
    agent_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: EditorUserWeb,
) -> Response:
    return await AgentsViewService.resume_agent(db, agent_id)


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.post(
    "/agents/{agent_id}/disable-web", include_in_schema=False, name="disable_agent_web"
)
async def disable_agent_web(
    agent_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: EditorUserWeb,
    reason: Annotated[str | None, Query()] = None,
) -> Response:
    return await AgentsViewService.disable_agent(db, agent_id, reason)


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.post(
    "/agents/{agent_id}/enable-web", include_in_schema=False, name="enable_agent_web"
)
async def enable_agent_web(
    agent_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: EditorUserWeb,
) -> Response:
    return await AgentsViewService.enable_agent(db, agent_id)


# ---- Key management (HTMX) ----------------------------------------------


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.get(
    "/agents/{agent_id}/key-management-panel",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="get_key_management_panel",
)
async def get_key_management_panel(
    agent_id: UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: CurrentUserWeb,
) -> HTMLResponse:
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
            request,
            "partials/panels/registration_keys/agent_key_management_panel.html",
            context,
        )
    except HTTPException:
        raise


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.post(
    "/agents/{agent_id}/generate-key-web",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="generate_key_web",
)
async def generate_key_web(
    agent_id: UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: EditorUserWeb,
) -> Response:
    """Generate initial API key for agent (if none exists)."""
    try:
        return await AgentsViewService.generate_agent_key(
            db, request, current_user, agent_id
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except AgentNotFoundException:
        raise HTTPException(status_code=404, detail="Agent not found") from None


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.post(
    "/agents/{agent_id}/regenerate-key-web",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="regenerate_key_web",
)
async def regenerate_key_web(
    agent_id: UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: EditorUserWeb,
) -> Response:
    """Regenerate API key (revokes old)."""
    try:
        return await AgentsViewService.regenerate_agent_key(
            db, request, current_user, agent_id
        )
    except AgentNotFoundException:
        raise HTTPException(status_code=404, detail="Agent not found") from None


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.delete(
    "/agents/{agent_id}/revoke-key-web", include_in_schema=False, name="revoke_key_web"
)
async def revoke_key_web(
    agent_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: EditorUserWeb,
) -> Response:
    """Revoke agent API key."""
    try:
        return await AgentsViewService.revoke_agent_key(db, agent_id)
    except AgentNotFoundException:
        raise HTTPException(status_code=404, detail="Agent not found") from None
