"""Scheduler schemas."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from app.models.enum_model import (
    SchedulerExecutionStatus,
    SchedulerJobCategory,
    SchedulerTriggerType,
)
from app.schemas.base_schema import BaseSchema


@dataclass(frozen=True)
class SchedulerJobUrls:
    """Finished hrefs for one scheduler job row (fw.url_assembly_in_view)."""

    history: str
    run: str
    reset: str
    toggle: str


class SchedulerJobResponse(BaseSchema):
    """A scheduled job's configuration and run statistics, as the admin UI shows it."""

    id: UUID
    job_key: str
    function_name: str
    display_name: str
    description: str | None
    category: SchedulerJobCategory
    enabled: bool
    parameters: dict[str, Any] | None
    trigger_type: SchedulerTriggerType
    interval_seconds: int | None
    cron_expression: str | None
    timezone: str
    next_run_at: datetime | None
    max_runtime_seconds: int
    lease_token: UUID | None
    lease_expires_at: datetime | None
    retry_limit: int
    retry_count: int
    last_run_at: datetime | None
    last_status: SchedulerExecutionStatus | None
    total_runs: int
    failed_runs: int
    average_duration: float | None


class SchedulerExecutionResponse(BaseSchema):
    """One run of a scheduled job."""

    id: UUID
    job_key: str
    started_at: datetime
    completed_at: datetime | None
    status: SchedulerExecutionStatus
    error_message: str | None
    output: dict[str, Any] | None
    records_processed: int | None
    duration_seconds: float | None


class SchedulerRunResult(BaseSchema):
    """The outcome of running a scheduled job on demand."""

    status: SchedulerExecutionStatus
    execution_id: UUID
    duration_seconds: float | None
    error: str | None = None
    job: SchedulerJobResponse  # the job, with this run counted

    @property
    def toast(self) -> tuple[str, str]:
        """The (message, kind) the Run button reports."""
        took = f"{self.duration_seconds or 0:.1f}s"
        if self.status == SchedulerExecutionStatus.FAILED:
            return f"Job failed ({took}) - {self.error}", "error"
        if self.status == SchedulerExecutionStatus.SUCCESS:
            return f"Job completed ({took})", "success"
        return f"Job completed with errors ({took})", "warning"
