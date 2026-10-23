"""
Scheduler view service - wraps scheduler_service for web UI consumption.

Builds display-ready data structures for templates.
Does NOT touch the database directly - delegates to scheduler_service.
"""

from datetime import UTC, datetime
from typing import Any

from fastapi import Request

from app.schemas.scheduler_schema import SchedulerJobResponse, SchedulerJobUrls
from app.services.core.scheduler_core_service import scheduler_service
from app.shared.logger import get_logger

logger = get_logger("luxswirl.web.services.scheduler_view")

# Status badge color mapping
STATUS_COLORS = {
    "success": "green",
    "failed": "red",
    "warning": "yellow",
    "running": "blue",
    "timeout": "orange",
}

# Category badge color mapping
CATEGORY_COLORS = {
    "cleanup": "purple",
    "monitoring": "cyan",
    "system": "gray",
}


def _format_duration(seconds: float | None) -> str:
    """Format duration in human-readable form."""
    if seconds is None:
        return "-"
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.1f}m"
    hours = minutes / 60
    return f"{hours:.1f}h"


def _format_schedule(job: SchedulerJobResponse) -> str:
    """Format job schedule for display."""
    if job.trigger_type == "cron":
        return f"cron: {job.cron_expression or '?'}"
    elif job.trigger_type == "interval":
        seconds = job.interval_seconds or 0
        if seconds < 60:
            return f"every {seconds}s"
        elif seconds < 3600:
            return f"every {seconds // 60}m"
        else:
            return f"every {seconds // 3600}h"
    elif job.trigger_type == "manual":
        return "manual"
    return job.trigger_type or "unknown"


def _is_running(job: SchedulerJobResponse) -> bool:
    """Check if a job currently holds a lease (is running)."""
    if not job.lease_token or not job.lease_expires_at:
        return False
    return job.lease_expires_at > datetime.now(UTC)


def build_scheduler_job_urls(request: Request, job_key: str) -> SchedulerJobUrls:
    # .path: url_for returns an absolute url from the request's scheme/host, which
    # behind TLS-terminating nginx can emit http:// into an https:// page.
    def u(name: str) -> str:
        return request.url_for(name, job_key=job_key).path

    return SchedulerJobUrls(
        history=u("job_history"),
        run=u("run_job"),
        reset=u("reset_job"),
        toggle=u("toggle_job"),
    )


def _job_row(request: Request, job: SchedulerJobResponse) -> dict[str, Any]:
    """One row of the scheduler table: the page, and every action's re-render."""
    return {
        "job": job,
        "schedule_display": _format_schedule(job),
        "category_color": CATEGORY_COLORS.get(job.category, "gray"),
        "status_color": STATUS_COLORS.get(job.last_status or "", "gray"),
        "is_running": _is_running(job),
        "avg_duration": _format_duration(job.average_duration),
        "now": datetime.now(UTC),
        "urls": build_scheduler_job_urls(request, job.job_key),
    }


class SchedulerViewService:
    """View service for scheduler admin UI."""

    @staticmethod
    async def get_list_context(request: Request) -> dict[str, Any]:
        """
        Build template context for the scheduler list page.

        Returns:
            Dict with jobs list and display metadata.
        """
        jobs = await scheduler_service.get_all_jobs()
        now = datetime.now(UTC)
        job_rows = [_job_row(request, job) for job in jobs]

        return {
            "job_rows": job_rows,
            # post-db-filter: every job row is rendered on this page; counts are over those rows
            "total_jobs": len(jobs),
            # post-db-filter: every job row is rendered on this page; counts are over those rows
            "enabled_count": sum(1 for j in jobs if j.enabled),
            # post-db-filter: every job row is rendered on this page; counts are over those rows
            "disabled_count": sum(1 for j in jobs if not j.enabled),
            "now": now,
        }

    @staticmethod
    async def toggle_job(request: Request, job_key: str) -> dict[str, Any]:
        """
        Toggle job enabled state and return updated row context.

        Returns:
            Dict with updated job row data for template rendering.
        """
        job = await scheduler_service.toggle_job(job_key)
        return _job_row(request, job)

    @staticmethod
    async def run_job(request: Request, job_key: str) -> dict[str, Any]:
        """
        Execute job synchronously and return updated row context.

        Returns:
            Dict with the updated job row and the run's (message, kind) toast.
        """
        result = await scheduler_service.execute_job_synchronously(job_key)
        return {"row": _job_row(request, result.job), "toast": result.toast}

    @staticmethod
    async def reset_job(request: Request, job_key: str) -> dict[str, Any]:
        """
        Reset job and return updated row context.

        Returns:
            Dict with updated job row data.
        """
        job = await scheduler_service.reset_job(job_key)
        return _job_row(request, job)

    @staticmethod
    async def get_job_history(job_key: str, limit: int = 50) -> dict[str, Any]:
        """
        Get job details and execution history for the history panel.

        Returns:
            Dict with job info, executions, and display metadata.
        """
        job, executions = await scheduler_service.get_job_history(job_key, limit=limit)
        now = datetime.now(UTC)

        execution_rows = []
        for ex in executions:
            execution_rows.append(
                {
                    "execution": ex,
                    "status_color": STATUS_COLORS.get(ex.status or "", "gray"),
                    "duration_display": _format_duration(ex.duration_seconds),
                }
            )

        return {
            "job": job,
            "schedule_display": _format_schedule(job),
            "category_color": CATEGORY_COLORS.get(job.category, "gray"),
            "status_color": STATUS_COLORS.get(job.last_status or "", "gray"),
            "is_running": _is_running(job),
            "execution_rows": execution_rows,
            "now": now,
        }
