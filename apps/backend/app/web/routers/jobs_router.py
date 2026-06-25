"""
Jobs router — web UI for job management.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_deps import (
    CurrentUserWeb,
    EditorUserWeb,
)
from app.core.query_params import JobStatusFilter, JobTypeFilter
from app.db import get_db
from app.services.views.job_bulk_check_view_service import JobBulkCheckViewService
from app.services.views.jobs_view_service import JobsViewService
from app.web._hx_responses import hx_empty_with_toast
from app.web.routers._render import bulk_check_response, error_partial
from app.web.templates_config import templates

logger = get_logger("luxswirl.web.routers.jobs")

router = APIRouter(tags=["Web UI - Jobs"])


# ---- Page / partials ----------------------------------------------------


@router.get("/jobs", response_class=HTMLResponse, include_in_schema=False)
async def jobs_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUserWeb,
    status: JobStatusFilter = None,
    job_type: JobTypeFilter = None,
    agent_filter: Annotated[
        str | None, Query(alias="agent_id", description="Filter by agent")
    ] = None,
    priority: Annotated[
        str | None, Query(description="Filter by priority (high/normal/low)")
    ] = None,
    created: Annotated[
        str | None, Query(description="Filter by created time (1h/24h/7d/30d)")
    ] = None,
    page: Annotated[int, Query(ge=1, description="Page number")] = 1,
    per_page: Annotated[
        int | None, Query(ge=10, le=200, description="Items per page (defaults to setting)")
    ] = None,
):
    """Main jobs page — paginated list with filters."""
    try:
        context = await JobsViewService.build_jobs_page_context(
            db,
            request,
            current_user,
            status,
            job_type,
            agent_filter,
            priority,
            created,
            page,
            per_page,
        )
        return templates.TemplateResponse(request, "pages/jobs.html", context)
    except Exception as e:
        logger.error("Error rendering jobs page", exc_info=True)
        return templates.TemplateResponse(
            request,
            "pages/error.html",
            {
                **JobsViewService.build_error_partial_context(request, current_user, str(e)),
                "page_title": "Error",
            },
            status_code=500,
        )


@router.get("/jobs/partials/table", response_class=HTMLResponse, include_in_schema=False)
async def jobs_table_partial(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUserWeb,
    status: JobStatusFilter = None,
    job_type: JobTypeFilter = None,
    agent_filter: Annotated[str | None, Query(alias="agent_id")] = None,
    priority: Annotated[str | None, Query()] = None,
    created: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=10, le=200)] = 25,
):
    """HTMX partial — jobs table for live updates (10s polling)."""
    try:
        context = await JobsViewService.build_jobs_table_partial_context(
            db,
            request,
            current_user,
            status,
            job_type,
            agent_filter,
            priority,
            created,
            page,
            per_page,
        )
        return templates.TemplateResponse(request, "partials/jobs_table.html", context)
    except Exception as e:
        logger.error("Error rendering jobs table partial", exc_info=True)
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            JobsViewService.build_error_partial_context(request, current_user, str(e)),
            status_code=500,
        )


@router.get("/jobs/{job_id}/detail", response_class=HTMLResponse, include_in_schema=False)
async def job_detail_panel(
    job_id: UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUserWeb,
):
    """Job detail panel for the side panel."""
    try:
        context = await JobsViewService.build_job_detail_context(db, request, current_user, job_id)
        return templates.TemplateResponse(request, "partials/job_detail_panel.html", context)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error loading job detail", exc_info=True)
        return error_partial(request, current_user, f"Error loading job details: {e}", 500)


@router.get("/jobs/create-form", response_class=HTMLResponse, include_in_schema=False)
async def job_create_form(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUserWeb,
    job_type: JobTypeFilter = None,
    agent_filter: Annotated[
        str | None, Query(alias="agent_id", description="Pre-select agent")
    ] = None,
    priority: Annotated[int, Query(description="Pre-fill priority")] = 0,
    prefill_params: Annotated[
        str | None, Query(description="JSON-encoded params to prefill")
    ] = None,
):
    """Job creation form for the side panel."""
    try:
        context = await JobsViewService.build_job_create_form_context(
            db, request, current_user, job_type, agent_filter, priority, prefill_params
        )
        return templates.TemplateResponse(request, "partials/job_create_form.html", context)
    except Exception as e:
        logger.error("Error loading job create form", exc_info=True)
        return error_partial(request, current_user, f"Error loading form: {e}", 500)


# ---- Mutations ----------------------------------------------------------


@router.post("/jobs/create", response_class=HTMLResponse, include_in_schema=False)
async def create_job_web(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
):
    """Create a new job via web form (toast + close panel + refresh on success)."""
    job_type = await JobsViewService.handle_create_job_form(db, request)
    return hx_empty_with_toast(
        f"Job created successfully: {job_type}",
        kind="success",
        extra_events={"closeSidePanel": {}, "refreshPage": {}},
    )


@router.delete("/jobs/{job_id}", include_in_schema=False)
async def delete_job_web(
    job_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
):
    """Delete job via web UI."""
    await JobsViewService.handle_delete_job(db, job_id)
    return hx_empty_with_toast("Job deleted successfully", kind="success")


@router.post("/jobs/{job_id}/cancel", include_in_schema=False)
async def cancel_job_web(
    job_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
):
    """Cancel a pending/running job."""
    await JobsViewService.handle_cancel_job(db, job_id)
    return hx_empty_with_toast(
        "Job cancelled successfully", kind="success", extra_events={"refreshPage": {}}
    )


# ---- Bulk-create-checks-from-job (separate concern → JobBulkCheckViewService) ---


@router.post(
    "/jobs/{job_id}/create-checks/bulk-ping",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def bulk_create_ping_checks(
    job_id: UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
):
    """Create ping checks for all discovered hosts from a network scan job."""
    form_data = await request.form()
    result, error = await JobBulkCheckViewService.create_ping_checks_from_job(
        db, job_id, dict(form_data)
    )
    return bulk_check_response(request, current_user, result, error, "ping check(s)")


@router.post(
    "/jobs/{job_id}/create-checks/bulk-web",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def bulk_create_web_checks(
    job_id: UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
):
    """Create HTTP checks for all web servers discovered in a network scan."""
    form_data = await request.form()
    result, error = await JobBulkCheckViewService.create_web_checks_from_job(
        db, job_id, dict(form_data)
    )
    return bulk_check_response(request, current_user, result, error, "HTTP check(s)")


@router.post(
    "/jobs/{job_id}/create-checks/bulk-ssh",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def bulk_create_ssh_checks(
    job_id: UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
):
    """Create TCP checks for all SSH servers discovered in a network scan."""
    form_data = await request.form()
    result, error = await JobBulkCheckViewService.create_ssh_checks_from_job(
        db, job_id, dict(form_data)
    )
    return bulk_check_response(request, current_user, result, error, "SSH check(s)")


@router.post(
    "/jobs/{job_id}/create-checks/bulk-database",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def bulk_create_database_checks(
    job_id: UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
):
    """Create TCP checks for all database servers discovered in a network scan."""
    form_data = await request.form()
    result, error = await JobBulkCheckViewService.create_database_checks_from_job(
        db, job_id, dict(form_data)
    )
    return bulk_check_response(request, current_user, result, error, "database check(s)")


@router.post(
    "/jobs/{job_id}/quick-create-check",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def quick_create_check_from_host(
    job_id: UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
):
    """
    Create a single check from a per-host quick-action button on the network
    scan detail page. Returns a toast and no swap — the page refreshes the
    network-scan detail naturally on next visit so duplicate-detect works.
    """
    form_data = await request.form()
    created, error = await JobBulkCheckViewService.quick_create_check(db, dict(form_data))
    if created:
        display_name = form_data.get("display_name", "check")
        return hx_empty_with_toast(f"Check '{display_name}' created")
    return hx_empty_with_toast(error or "Failed to create check", kind="error")
