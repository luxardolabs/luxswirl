"""
Scheduler view service - wraps scheduler_service for web UI consumption.

Builds display-ready data structures for templates.
Does NOT touch the database directly - delegates to scheduler_service.
"""

from datetime import UTC, datetime
from typing import cast

from shared.logger import get_logger

from app.services.core.scheduler_core_service import scheduler_service

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


def _format_schedule(job) -> str:
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


def _is_running(job) -> bool:
    """Check if a job currently holds a lease (is running)."""
    if not job.lease_token or not job.lease_expires_at:
        return False
    return cast(bool, job.lease_expires_at > datetime.now(UTC))


class SchedulerViewService:
    """View service for scheduler admin UI."""

    @staticmethod
    async def get_list_context() -> dict:
        """
        Build template context for the scheduler list page.

        Returns:
            Dict with jobs list and display metadata.
        """
        jobs = await scheduler_service.get_all_jobs()
        now = datetime.now(UTC)

        job_rows = []
        for job in jobs:
            job_rows.append(
                {
                    "job": job,
                    "schedule_display": _format_schedule(job),
                    "category_color": CATEGORY_COLORS.get(job.category, "gray"),
                    "status_color": STATUS_COLORS.get(job.last_status or "", "gray"),
                    "is_running": _is_running(job),
                    "avg_duration": _format_duration(job.average_duration),
                    "now": now,
                }
            )

        return {
            "job_rows": job_rows,
            "total_jobs": len(jobs),
            "enabled_count": sum(1 for j in jobs if j.enabled),
            "disabled_count": sum(1 for j in jobs if not j.enabled),
            "now": now,
        }

    @staticmethod
    async def toggle_job(job_key: str) -> dict:
        """
        Toggle job enabled state and return updated row context.

        Returns:
            Dict with updated job row data for template rendering.
        """
        job = await scheduler_service.toggle_job(job_key)
        now = datetime.now(UTC)
        return {
            "job": job,
            "schedule_display": _format_schedule(job),
            "category_color": CATEGORY_COLORS.get(job.category, "gray"),
            "status_color": STATUS_COLORS.get(job.last_status or "", "gray"),
            "is_running": _is_running(job),
            "avg_duration": _format_duration(job.average_duration),
            "now": now,
        }

    @staticmethod
    async def run_job(job_key: str) -> dict:
        """
        Execute job synchronously and return updated row context.

        Returns:
            Dict with execution result and updated job row data.
        """
        result = await scheduler_service.execute_job_synchronously(job_key)

        # Reload job for updated stats
        jobs = await scheduler_service.get_all_jobs()
        job = next((j for j in jobs if j.job_key == job_key), None)
        now = datetime.now(UTC)

        row = {
            "job": job,
            "schedule_display": _format_schedule(job) if job else "",
            "category_color": CATEGORY_COLORS.get(job.category, "gray") if job else "gray",
            "status_color": STATUS_COLORS.get(job.last_status or "", "gray") if job else "gray",
            "is_running": _is_running(job) if job else False,
            "avg_duration": _format_duration(job.average_duration) if job else "-",
            "now": now,
        }

        return {"result": result, "row": row}

    @staticmethod
    async def reset_job(job_key: str) -> dict:
        """
        Reset job and return updated row context.

        Returns:
            Dict with updated job row data.
        """
        job = await scheduler_service.reset_job(job_key)
        now = datetime.now(UTC)
        return {
            "job": job,
            "schedule_display": _format_schedule(job),
            "category_color": CATEGORY_COLORS.get(job.category, "gray"),
            "status_color": STATUS_COLORS.get(job.last_status or "", "gray"),
            "is_running": _is_running(job),
            "avg_duration": _format_duration(job.average_duration),
            "now": now,
        }

    @staticmethod
    async def get_job_history(job_key: str, limit: int = 50) -> dict:
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
