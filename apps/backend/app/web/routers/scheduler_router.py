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

from app.core.auth_deps import AdminUserWeb
from app.services.views.scheduler_view_service import SchedulerViewService
from app.web._hx_responses import hx_empty_with_toast, hx_toast_trigger
from app.web.templates_config import templates

logger = get_logger("luxswirl.web.routers.scheduler")

router = APIRouter(tags=["Web UI - Scheduler"], include_in_schema=False)


@router.get("/scheduler", response_class=HTMLResponse)
async def scheduler_page(
    request: Request,
    current_user: AdminUserWeb,
):
    """
    Scheduler admin page - lists all job configurations with controls.
    """
    try:
        context = await SchedulerViewService.get_list_context()

        return templates.TemplateResponse(
            request,
            "pages/scheduler.html",
            {
                "current_user": current_user,
                "page_title": "Scheduler",
                **context,
            },
        )
    except Exception as e:
        logger.error("Error rendering scheduler page", exc_info=True)
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
        row = await SchedulerViewService.toggle_job(job_key)

        response = templates.TemplateResponse(
            request,
            "partials/scheduler_job_row.html",
            {
                "current_user": current_user,
                **row,
            },
        )

        status = "enabled" if row["job"].enabled else "disabled"
        response.headers["HX-Trigger"] = hx_toast_trigger(f"Job {row['job'].display_name} {status}")
        return response

    except ValueError as e:
        return hx_empty_with_toast(str(e), kind="error", status_code=404)
    except Exception as e:
        logger.error(
            "Error toggling job",
            extra={"job_key": job_key},
            exc_info=True,
        )
        return hx_empty_with_toast(f"Error: {str(e)}", kind="error", status_code=500)


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
        data = await SchedulerViewService.run_job(job_key)
        result = data["result"]
        row = data["row"]

        response = templates.TemplateResponse(
            request,
            "partials/scheduler_job_row.html",
            {
                "current_user": current_user,
                **row,
            },
        )

        duration = result.get("duration_seconds", 0)
        response.headers["HX-Trigger"] = hx_toast_trigger(
            f"Job completed ({duration:.1f}s) - {result['status']}",
            kind="success" if result["status"] == "success" else "warning",
        )
        return response

    except ValueError as e:
        return hx_empty_with_toast(str(e), kind="error", status_code=404)
    except Exception as e:
        logger.error(
            "Error running job",
            extra={"job_key": job_key},
            exc_info=True,
        )
        return hx_empty_with_toast(f"Job failed: {str(e)}", kind="error", status_code=500)


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
        row = await SchedulerViewService.reset_job(job_key)

        response = templates.TemplateResponse(
            request,
            "partials/scheduler_job_row.html",
            {
                "current_user": current_user,
                **row,
            },
        )

        response.headers["HX-Trigger"] = hx_toast_trigger(f"Job {row['job'].display_name} reset")
        return response

    except ValueError as e:
        return hx_empty_with_toast(str(e), kind="error", status_code=404)
    except Exception as e:
        logger.error(
            "Error resetting job",
            extra={"job_key": job_key},
            exc_info=True,
        )
        return hx_empty_with_toast(f"Error: {str(e)}", kind="error", status_code=500)


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
            "partials/scheduler_history_panel.html",
            {
                "current_user": current_user,
                **context,
            },
        )

    except ValueError as e:
        return HTMLResponse(
            content=f'<div class="p-6 text-red-400">{str(e)}</div>',
            status_code=404,
        )
    except Exception as e:
        logger.error(
            "Error loading job history",
            extra={"job_key": job_key},
            exc_info=True,
        )
        return HTMLResponse(
            content=f'<div class="p-6 text-red-400">Error: {str(e)}</div>',
            status_code=500,
        )
