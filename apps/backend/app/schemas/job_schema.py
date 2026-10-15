"""
Pydantic schemas for Job domain.

Jobs are tasks dispatched to agents (or executed on server) for discovery,
diagnostics, and maintenance operations.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import AliasChoices, Field, field_validator

from app.models.enum_model import (
    SERVER_RUNNER,
    JobStatus,
    JobType,
    MaintenanceJobKind,
    MaintenanceJobStatus,
)
from app.schemas.base_schema import BaseSchema, TimestampSchema


class JobBase(BaseSchema):
    """Base schema for Job with common fields."""

    # JobType IS the validation — bad value is a 422.
    job_type: JobType = Field(..., description="Type of job to execute")
    agent_id: UUID | None = Field(
        None, description="Agent to run job on (null = server)"
    )
    params: dict[str, Any] = Field(
        default_factory=dict[str, Any], description="Job-specific parameters"
    )
    priority: int = Field(
        0, ge=0, le=100, description="Job priority (higher = runs first)"
    )
    tags: list[str] | None = Field(
        None, description="Tags for organizing/filtering jobs"
    )

    @property
    def runner_token(self) -> str:
        """The runner as a UI/URL token: the agent UUID string, or the server.

        A job runs on a runner: a specific agent, or the server itself
        (agent_id IS NULL). Callers use this instead of comparing to NULL.
        """
        return str(self.agent_id) if self.agent_id else SERVER_RUNNER


class JobCreate(JobBase):
    """Schema for creating a new job."""

    # Future fields (design now, implement in Phase 2)
    schedule: dict[str, Any] | None = Field(
        None, description="Scheduling config (null = one-time job)"
    )
    automation_config: dict[str, Any] | None = Field(
        None, description="Automation rules (null = manual)"
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "job_type": "network_scan",
                "agent_id": "london-agent",
                "params": {
                    "subnet": "192.168.1.0/24",
                    "timeout": 5,
                    "ports": [80, 443, 22],
                },
                "priority": 10,
                "tags": ["discovery", "production"],
            }
        }
    }


class JobResultSubmit(BaseSchema):
    """Schema for agent submitting job results."""

    status: Literal[JobStatus.COMPLETED, JobStatus.FAILED] = Field(
        ..., description="Final job status (completed or failed)"
    )
    result: dict[str, Any] | None = Field(None, description="Job result data")
    error: str | None = Field(
        None, max_length=5000, description="Error message if failed"
    )

    @field_validator("status", mode="before")
    @classmethod
    def _lowercase_status(cls, v: object) -> object:
        """Agents may send `COMPLETED`; the terminal states are lowercase."""
        return v.lower() if isinstance(v, str) else v

    model_config = {
        "json_schema_extra": {
            "example": {
                "status": "completed",
                "result": {
                    "discovered_hosts": [
                        {
                            "ip": "192.168.1.10",
                            "hostname": "printer",
                            "ports": [80, 443],
                        },
                        {"ip": "192.168.1.20", "hostname": "nas", "ports": [22, 80]},
                    ],
                    "scan_duration_seconds": 12.5,
                    "hosts_scanned": 254,
                    "hosts_responding": 2,
                },
                "error": None,
            }
        }
    }


class JobInDB(JobBase, TimestampSchema):
    """Schema for job in database."""

    id: UUID = Field(..., description="Job UUID")
    status: JobStatus = Field(JobStatus.PENDING, description="Job status")
    created_by: str | None = Field(None, description="User who created the job")

    # Timestamps
    assigned_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    expires_at: datetime | None = None

    # Results
    result: dict[str, Any] | None = None
    error: str | None = None

    # Future fields
    schedule: dict[str, Any] | None = None
    automation_config: dict[str, Any] | None = None
    parent_job_id: UUID | None = None


class JobDetail(JobInDB):
    """A job as its detail panel shows it: the stored fields plus its duration."""

    duration_seconds: float | None = Field(None, description="Job execution duration")


class JobResponse(JobDetail):
    """Schema for job API responses."""

    agent_hostname: str | None = Field(
        None, description="Hostname of agent that ran the job"
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": "550e8400-e29b-41d4-a716-446655440000",
                "job_type": "network_scan",
                "agent_id": "london-agent",
                "agent_hostname": "london-01.example.com",
                "params": {"subnet": "192.168.1.0/24", "timeout": 5},
                "priority": 10,
                "status": "completed",
                "tags": ["discovery"],
                "created_at": "2024-01-01T12:00:00Z",
                "updated_at": "2024-01-01T12:00:15Z",
                "assigned_at": "2024-01-01T12:00:01Z",
                "started_at": "2024-01-01T12:00:02Z",
                "completed_at": "2024-01-01T12:00:15Z",
                "expires_at": "2024-01-08T12:00:00Z",
                "duration_seconds": 13.0,
                "result": {
                    "discovered_hosts": [
                        {"ip": "192.168.1.10", "hostname": "printer"},
                    ]
                },
                "error": None,
                "created_by": "admin@example.com",
                "schedule": None,
                "automation_config": None,
                "parent_job_id": None,
            }
        }
    }


class JobListRow(BaseSchema):
    """One row of the jobs table, as the web seam receives it.

    The listing joins a job to its agent, so this carries the agent's name
    alongside the job's own columns rather than handing the view a
    (Job, Agent | None) tuple of ORM rows. duration_seconds and the two
    has_* flags are derived here because they are what the table renders — the
    template never sees the result or error payload itself, only whether there
    is one.
    """

    id: UUID
    job_type: JobType
    agent_id: UUID | None = None
    agent_name: str | None = None
    status: JobStatus
    priority: int
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    has_result: bool = False
    has_error: bool = False
    tags: list[str] | None = None

    @property
    def runner_token(self) -> str:
        """The runner as a UI/URL token: the agent UUID string, or the server."""
        return str(self.agent_id) if self.agent_id else SERVER_RUNNER


class JobListResponse(BaseSchema):
    """Schema for listing jobs."""

    jobs: list[JobResponse] = Field(..., description="List of jobs")
    total: int = Field(..., description="Total number of jobs")
    pending_count: int = Field(0, description="Number of pending jobs")
    running_count: int = Field(0, description="Number of running jobs")
    completed_count: int = Field(0, description="Number of completed jobs")
    failed_count: int = Field(0, description="Number of failed jobs")


class JobStatsResponse(BaseSchema):
    """Job counts by status (server-wide, or for one runner)."""

    pending: int = Field(0, description="Waiting to be assigned")
    assigned: int = Field(0, description="Assigned, not yet started")
    running: int = Field(0, description="Executing")
    completed: int = Field(0, description="Finished successfully")
    failed: int = Field(0, description="Finished with an error")
    cancelled: int = Field(0, description="Cancelled before finishing")
    total: int = Field(0, description="All of the above")
    active: int = Field(0, description="pending + assigned + running")
    terminal: int = Field(0, description="completed + failed + cancelled")


class JobDispatch(BaseSchema):
    """Job dispatch info sent to agent in heartbeat response."""

    job_id: UUID = Field(..., description="Job UUID")
    job_type: JobType = Field(..., description="Job type")
    params: dict[str, Any] = Field(
        default_factory=dict[str, Any], description="Job parameters"
    )
    priority: int = Field(0, description="Job priority")
    timeout_seconds: int = Field(
        300, ge=1, le=3600, description="Job execution timeout"
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "job_id": "550e8400-e29b-41d4-a716-446655440000",
                "job_type": "network_scan",
                "params": {"subnet": "192.168.1.0/24", "timeout": 5},
                "priority": 10,
                "timeout_seconds": 300,
            }
        }
    }


class MaintenanceJobEnqueuedResponse(BaseSchema):
    """Acknowledgement that a maintenance job was queued for the worker.

    The work itself (e.g. an agent delete cascading through checks and results)
    runs in the in-process maintenance worker, so the request returns this
    immediately instead of holding a long transaction.
    """

    # Read straight off the queued MaintenanceJob row, whose key is `id`.
    job_id: UUID = Field(
        ...,
        validation_alias=AliasChoices("job_id", "id"),
        description="Id of the queued maintenance job",
    )
    status: MaintenanceJobStatus = Field(..., description="Queued job's status")
    kind: MaintenanceJobKind = Field(..., description="What the job will do")


@dataclass(frozen=True)
class JobUrls:
    """Finished hrefs for one job row (fw.url_assembly_in_view)."""

    detail: str
    cancel: str
    delete: str


@dataclass(frozen=True)
class ScanActionUrls:
    """Finished hrefs for the network-scan quick-action buttons."""

    bulk_ping: str
    bulk_web: str
    bulk_ssh: str
    bulk_database: str
    quick_create: str
