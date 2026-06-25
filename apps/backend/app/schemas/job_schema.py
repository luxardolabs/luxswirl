"""
Pydantic schemas for Job domain.

Jobs are tasks dispatched to agents (or executed on server) for discovery,
diagnostics, and maintenance operations.
"""

from datetime import datetime
from uuid import UUID

from pydantic import Field, field_validator

from app.models.enum_model import JobStatus, JobType
from app.schemas.base import BaseSchema, TimestampSchema


class JobBase(BaseSchema):
    """Base schema for Job with common fields."""

    # JobType IS the validation — bad value is a 422.
    job_type: JobType = Field(..., description="Type of job to execute")
    agent_id: UUID | None = Field(None, description="Agent to run job on (null = server)")
    params: dict = Field(default_factory=dict, description="Job-specific parameters")
    priority: int = Field(0, ge=0, le=100, description="Job priority (higher = runs first)")
    tags: list[str] | None = Field(None, description="Tags for organizing/filtering jobs")


class JobCreate(JobBase):
    """Schema for creating a new job."""

    # Future fields (design now, implement in Phase 2)
    schedule: dict | None = Field(None, description="Scheduling config (null = one-time job)")
    automation_config: dict | None = Field(None, description="Automation rules (null = manual)")

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


class JobUpdate(BaseSchema):
    """Schema for updating a job (primarily for status changes)."""

    # JobStatus IS the validation.
    status: JobStatus | None = Field(None, description="Job status")
    priority: int | None = Field(None, ge=0, le=100, description="Job priority")
    tags: list[str] | None = None


class JobResultSubmit(BaseSchema):
    """Schema for agent submitting job results."""

    status: str = Field(..., description="Final job status (completed or failed)")
    result: dict | None = Field(None, description="Job result data")
    error: str | None = Field(None, max_length=5000, description="Error message if failed")

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        """Validate status is a terminal state."""
        if v.lower() not in {"completed", "failed"}:
            raise ValueError("Result status must be 'completed' or 'failed'")
        return v.lower()

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
    status: str = Field("pending", description="Job status")
    created_by: str | None = Field(None, description="User who created the job")

    # Timestamps
    assigned_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    expires_at: datetime | None = None

    # Results
    result: dict | None = None
    error: str | None = None

    # Future fields
    schedule: dict | None = None
    automation_config: dict | None = None
    parent_job_id: UUID | None = None


class JobResponse(JobInDB):
    """Schema for job API responses."""

    duration_seconds: float | None = Field(None, description="Job execution duration")
    agent_hostname: str | None = Field(None, description="Hostname of agent that ran the job")

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


class JobListResponse(BaseSchema):
    """Schema for listing jobs."""

    jobs: list[JobResponse] = Field(..., description="List of jobs")
    total: int = Field(..., description="Total number of jobs")
    pending_count: int = Field(0, description="Number of pending jobs")
    running_count: int = Field(0, description="Number of running jobs")
    completed_count: int = Field(0, description="Number of completed jobs")
    failed_count: int = Field(0, description="Number of failed jobs")


class JobQueueStats(BaseSchema):
    """Agent job queue statistics (included in heartbeat)."""

    jobs_pending: int = Field(0, description="Jobs in queue waiting to run")
    jobs_running: int = Field(0, description="Jobs currently executing")
    jobs_completed_since_last: int = Field(0, description="Jobs completed since last heartbeat")
    jobs_failed_since_last: int = Field(0, description="Jobs failed since last heartbeat")
    queue_capacity: int = Field(5, description="Max concurrent jobs")


class JobDispatch(BaseSchema):
    """Job dispatch info sent to agent in heartbeat response."""

    job_id: UUID = Field(..., description="Job UUID")
    job_type: str = Field(..., description="Job type")
    params: dict = Field(default_factory=dict, description="Job parameters")
    priority: int = Field(0, description="Job priority")
    timeout_seconds: int = Field(300, ge=1, le=3600, description="Job execution timeout")

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
