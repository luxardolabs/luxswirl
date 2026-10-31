"""
Jobs router — web UI for job management.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.query_params import JobStatusFilter, JobTypeFilter, PerPageFilter
from app.db import get_db
from app.services.views.job_bulk_check_view_service import JobBulkCheckViewService
from app.services.views.jobs_view_service import JobsViewService
from app.shared.logger import get_logger
from app.web.deps import CurrentUserWeb, EditorUserWeb
from app.web.hx_responses import hx_empty_with_toast
from app.web.render import bulk_check_response
from app.web.templates import templates

logger = get_logger("luxswirl.web.routers.jobs")

router = APIRouter(tags=["Web UI - Jobs"])


# ---- Page / partials ----------------------------------------------------


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.get(
    "/jobs", response_class=HTMLResponse, include_in_schema=False, name="jobs.index"
)
async def jobs_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
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
    per_page: PerPageFilter = None,
) -> HTMLResponse:
    """Main jobs page — paginated list with filters."""
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


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.get(
    "/jobs/partials/table",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="jobs.table",
)
async def jobs_table_partial(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: CurrentUserWeb,
    status: JobStatusFilter = None,
    job_type: JobTypeFilter = None,
    agent_filter: Annotated[str | None, Query(alias="agent_id")] = None,
    priority: Annotated[str | None, Query()] = None,
    created: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=10, le=200)] = 25,
) -> HTMLResponse:
    """HTMX partial — jobs table for live updates (10s polling)."""
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


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.get(
    "/jobs/{job_id}/detail",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="jobs.show",
)
async def job_detail_panel(
    job_id: UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: CurrentUserWeb,
) -> HTMLResponse:
    """Job detail panel for the side panel."""
    try:
        context = await JobsViewService.build_job_detail_context(
            db, request, current_user, job_id
        )
        return templates.TemplateResponse(
            request, "partials/job_detail_panel.html", context
        )
    except HTTPException:
        raise


@router.get(
    "/jobs/create-form",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="jobs.new",
)
async def job_create_form(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: CurrentUserWeb,
    job_type: JobTypeFilter = None,
    agent_filter: Annotated[
        str | None, Query(alias="agent_id", description="Pre-select agent")
    ] = None,
    priority: Annotated[int, Query(description="Pre-fill priority")] = 0,
    prefill_params: Annotated[
        str | None, Query(description="JSON-encoded params to prefill")
    ] = None,
) -> HTMLResponse:
    """Job creation form for the side panel."""
    context = await JobsViewService.build_job_create_form_context(
        db, request, current_user, job_type, agent_filter, priority, prefill_params
    )
    return templates.TemplateResponse(request, "partials/job_create_form.html", context)


# ---- Mutations ----------------------------------------------------------


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.post(
    "/jobs/create",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="jobs.create",
)
async def create_job_web(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: EditorUserWeb,
) -> HTMLResponse:
    """Create a new job via web form (toast + close panel + refresh on success)."""
    job_type = await JobsViewService.handle_create_job_form(db, request)
    return hx_empty_with_toast(
        f"Job created successfully: {job_type}",
        kind="success",
        extra_events={"closeSidePanel": {}, "refreshPage": {}},
    )


@router.delete("/jobs/{job_id}", include_in_schema=False, name="jobs.delete")
async def delete_job_web(
    job_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: EditorUserWeb,
) -> HTMLResponse:
    """Delete job via web UI."""
    await JobsViewService.handle_delete_job(db, job_id)
    return hx_empty_with_toast("Job deleted successfully", kind="success")


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.post("/jobs/{job_id}/cancel", include_in_schema=False, name="jobs.cancel")
async def cancel_job_web(
    job_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: EditorUserWeb,
) -> HTMLResponse:
    """Cancel a pending/running job."""
    await JobsViewService.handle_cancel_job(db, job_id)
    return hx_empty_with_toast(
        "Job cancelled successfully", kind="success", extra_events={"refreshPage": {}}
    )


# ---- Bulk-create-checks-from-job (separate concern → JobBulkCheckViewService) ---


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.post(
    "/jobs/{job_id}/create-checks/bulk-ping",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="job_checks.bulk_ping",
)
async def bulk_create_ping_checks(
    job_id: UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: EditorUserWeb,
) -> Response:
    """Create ping checks for all discovered hosts from a network scan job."""
    form_data = await request.form()
    result, error = await JobBulkCheckViewService.create_ping_checks_from_job(
        db, job_id, dict(form_data)
    )
    return bulk_check_response(request, current_user, result, error, "ping check(s)")


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.post(
    "/jobs/{job_id}/create-checks/bulk-web",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="job_checks.bulk_web",
)
async def bulk_create_web_checks(
    job_id: UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: EditorUserWeb,
) -> Response:
    """Create HTTP checks for all web servers discovered in a network scan."""
    form_data = await request.form()
    result, error = await JobBulkCheckViewService.create_web_checks_from_job(
        db, job_id, dict(form_data)
    )
    return bulk_check_response(request, current_user, result, error, "HTTP check(s)")


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.post(
    "/jobs/{job_id}/create-checks/bulk-ssh",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="job_checks.bulk_ssh",
)
async def bulk_create_ssh_checks(
    job_id: UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: EditorUserWeb,
) -> Response:
    """Create TCP checks for all SSH servers discovered in a network scan."""
    form_data = await request.form()
    result, error = await JobBulkCheckViewService.create_ssh_checks_from_job(
        db, job_id, dict(form_data)
    )
    return bulk_check_response(request, current_user, result, error, "SSH check(s)")


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.post(
    "/jobs/{job_id}/create-checks/bulk-database",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="job_checks.bulk_database",
)
async def bulk_create_database_checks(
    job_id: UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: EditorUserWeb,
) -> Response:
    """Create TCP checks for all database servers discovered in a network scan."""
    form_data = await request.form()
    result, error = await JobBulkCheckViewService.create_database_checks_from_job(
        db, job_id, dict(form_data)
    )
    return bulk_check_response(
        request, current_user, result, error, "database check(s)"
    )


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.post(
    "/jobs/{job_id}/quick-create-check",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="job_checks.quick_create",
)
async def quick_create_check_from_host(
    job_id: UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: EditorUserWeb,
) -> HTMLResponse:
    """
    Create a single check from a per-host quick-action button on the network
    scan detail page. Returns a toast and no swap — the page refreshes the
    network-scan detail naturally on next visit so duplicate-detect works.
    """
    form_data = await request.form()
    created, error = await JobBulkCheckViewService.quick_create_check(
        db, dict(form_data)
    )
    if created:
        display_name = form_data.get("display_name", "check")
        return hx_empty_with_toast(f"Check '{display_name}' created")
    return hx_empty_with_toast(error or "Failed to create check", kind="error")
