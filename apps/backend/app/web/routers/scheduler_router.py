"""
Scheduler router - admin UI for managing scheduled jobs.

Provides:
- Scheduler dashboard with all job configurations
- Toggle job enabled/disabled
- Manual job execution (run now)
- Reset job retry state
- View job execution history
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from shared.logger import get_logger

from app.services.views.scheduler_view_service import SchedulerViewService
from app.web.deps import AdminUserWeb
from app.web.hx_responses import hx_empty_with_toast, hx_toast_trigger
from app.web.render import settings_error_partial
from app.web.templates import templates

logger = get_logger("luxswirl.web.routers.scheduler")

router = APIRouter(tags=["Web UI - Scheduler"], include_in_schema=False)


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.get("/scheduler", response_class=HTMLResponse)
async def scheduler_page(
    request: Request,
    current_user: AdminUserWeb,
):
    """
    Scheduler admin page - lists all job configurations with controls.
    """
    context = await SchedulerViewService.get_list_context(request)

    return templates.TemplateResponse(
        request,
        "pages/settings/scheduler.html",
        {
            "current_user": current_user,
            "page_title": "Scheduler",
            **context,
        },
    )


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.post("/scheduler/jobs/{job_key}/toggle", response_class=HTMLResponse)
async def toggle_job(
    job_key: str,
    request: Request,
    current_user: AdminUserWeb,
):
    """
    Toggle job enabled/disabled state. Returns updated job row partial.
    """
    try:
        row = await SchedulerViewService.toggle_job(request, job_key)

        response = templates.TemplateResponse(
            request,
            "partials/settings/scheduler_job_row.html",
            # One shape for both callers: the page loops `for row in job_rows`
            # and includes the partial, so an action re-render passes the same
            # `row` rather than splatting it flat.
            {"current_user": current_user, "row": row},
        )

        status = "enabled" if row["job"].enabled else "disabled"
        response.headers["HX-Trigger"] = hx_toast_trigger(
            f"Job {row['job'].display_name} {status}"
        )
        return response

    except ValueError as e:
        return hx_empty_with_toast(str(e), kind="error", status_code=404)


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.post("/scheduler/jobs/{job_key}/run", response_class=HTMLResponse)
async def run_job(
    job_key: str,
    request: Request,
    current_user: AdminUserWeb,
):
    """
    Execute a job immediately. Returns updated job row partial.
    """
    try:
        data = await SchedulerViewService.run_job(request, job_key)
        row = data["row"]

        response = templates.TemplateResponse(
            request,
            "partials/settings/scheduler_job_row.html",
            # One shape for both callers: the page loops `for row in job_rows`
            # and includes the partial, so an action re-render passes the same
            # `row` rather than splatting it flat.
            {"current_user": current_user, "row": row},
        )

        message, kind = data["toast"]
        response.headers["HX-Trigger"] = hx_toast_trigger(message, kind=kind)
        return response

    except ValueError as e:
        return hx_empty_with_toast(str(e), kind="error", status_code=404)


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.post("/scheduler/jobs/{job_key}/reset", response_class=HTMLResponse)
async def reset_job(
    job_key: str,
    request: Request,
    current_user: AdminUserWeb,
):
    """
    Reset job retry state and re-enable. Returns updated job row partial.
    """
    try:
        row = await SchedulerViewService.reset_job(request, job_key)

        response = templates.TemplateResponse(
            request,
            "partials/settings/scheduler_job_row.html",
            # One shape for both callers: the page loops `for row in job_rows`
            # and includes the partial, so an action re-render passes the same
            # `row` rather than splatting it flat.
            {"current_user": current_user, "row": row},
        )

        response.headers["HX-Trigger"] = hx_toast_trigger(
            f"Job {row['job'].display_name} reset"
        )
        return response

    except ValueError as e:
        return hx_empty_with_toast(str(e), kind="error", status_code=404)


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.get("/scheduler/jobs/{job_key}/history", response_class=HTMLResponse)
async def job_history(
    job_key: str,
    request: Request,
    current_user: AdminUserWeb,
):
    """
    Get job execution history panel. Loaded into the side panel.
    """
    try:
        context = await SchedulerViewService.get_job_history(job_key)

        return templates.TemplateResponse(
            request,
            "partials/settings/scheduler_history_panel.html",
            {
                "current_user": current_user,
                **context,
            },
        )

    except ValueError as e:
        return settings_error_partial(request, current_user, str(e), status_code=404)
