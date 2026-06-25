"""
Pydantic schemas for Agent domain.
"""

from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.models.enum_model import AgentApprovalStatus, AgentStatus
from app.schemas.base import BaseSchema, TimestampSchema


class AgentRegisterRequest(BaseSchema):
    """Schema for agent registration (before approval)."""

    hostname: str | None = Field(None, max_length=255, description="Agent hostname")
    ip_address: str | None = Field(None, max_length=45, description="Agent IP address")
    version: str | None = Field(None, max_length=50, description="Agent version")
    tags: list[str] | None = Field(None, description="Agent tags")

    model_config = {
        "json_schema_extra": {
            "example": {
                "hostname": "web01.example.com",
                "ip_address": "10.0.1.100",
                "version": "1.0.0",
                "tags": ["production", "web"],
            }
        }
    }


class AgentRegisterResponse(BaseSchema):
    """Schema for agent registration response."""

    agent_id: UUID = Field(..., description="Assigned agent UUID")
    status: str = Field(..., description="Registration status (pending/approved)")
    message: str = Field(..., description="Human-readable message")

    model_config = {
        "json_schema_extra": {
            "example": {
                "agent_id": "550e8400-e29b-41d4-a716-446655440000",
                "status": "pending",
                "message": "Agent registered. Awaiting approval.",
            }
        }
    }


class AgentBase(BaseSchema):
    """Base schema for Agent with common fields."""

    agent_name: str | None = Field(
        None, max_length=255, description="Agent name (None until approved)"
    )
    hostname: str | None = Field(None, max_length=255, description="Agent hostname")
    ip_address: str | None = Field(None, max_length=45, description="Agent IP address")
    version: str | None = Field(None, max_length=50, description="Agent version")
    tags: list[str] | None = Field(None, description="Tags for grouping/filtering")


class AgentCreate(AgentBase):
    """Schema for creating a new agent."""

    agent_run_id: str | None = Field(None, max_length=255, description="Current run ID (UUID)")

    model_config = {
        "json_schema_extra": {
            "example": {
                "agent_name": "prod-web-01",
                "hostname": "web01.example.com",
                "ip_address": "10.0.1.100",
                "version": "1.0.0",
                "tags": ["production", "web", "us-east-1"],
            }
        }
    }


class AgentUpdate(BaseSchema):
    """Schema for updating an agent."""

    agent_name: str | None = Field(None, min_length=1, max_length=255)
    agent_run_id: str | None = None
    hostname: str | None = None
    ip_address: str | None = None
    version: str | None = None
    tags: list[str] | None = None
    heartbeat_interval: int | None = Field(
        None, ge=1, le=600, description="Heartbeat interval in seconds"
    )
    check_sync_interval: int | None = Field(
        None, ge=1, le=600, description="Check sync interval in seconds"
    )

    # Reporter configuration
    report_interval: int | None = Field(
        None, ge=1, le=300, description="Reporter batch send interval in seconds"
    )
    report_batch_size: int | None = Field(
        None, ge=10, le=5000, description="Reporter batch size in results"
    )
    report_max_files_per_batch: int | None = Field(
        None, ge=1, le=100, description="Max stored reports to process per batch"
    )
    report_process_interval: int | None = Field(
        None,
        ge=1,
        le=300,
        description="Interval for processing stored reports in seconds",
    )
    report_max_queue_size: int | None = Field(
        None, ge=100, le=50000, description="Maximum results queue size"
    )
    report_backpressure_threshold: float | None = Field(
        None, ge=0.1, le=1.0, description="Backpressure threshold 0.0-1.0"
    )

    # Performance tuning
    max_concurrent_checks: int | None = Field(
        None, ge=1, le=1000, description="Maximum concurrent checks"
    )
    watchdog_interval: int | None = Field(
        None, ge=5, le=300, description="Watchdog check interval in seconds"
    )
    watchdog_stall_threshold: int | None = Field(
        None, ge=1, le=10, description="Watchdog stall threshold count"
    )

    # Logging configuration
    log_level: str | None = Field(
        None,
        pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$",
        description="Logging level",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "version": "1.0.1",
                "tags": ["production", "web", "us-east-1", "updated"],
                "heartbeat_interval": 60,
                "check_sync_interval": 60,
                "report_interval": 10,
                "report_batch_size": 500,
                "max_concurrent_checks": 200,
                "log_level": "INFO",
            }
        }
    }


class AgentInDB(AgentBase, TimestampSchema):
    """Schema for agent in database (includes all fields)."""

    id: UUID = Field(..., description="Agent UUID")
    agent_run_id: str | None = Field(None, description="Current run ID")
    first_seen: datetime = Field(..., description="When agent was first seen")
    last_seen: datetime = Field(..., description="When agent last reported")


class AgentResponse(AgentInDB):
    """Schema for agent API responses."""

    is_online: bool = Field(..., description="Whether agent is currently online")
    uptime_seconds: float | None = Field(None, description="Agent uptime in seconds")
    check_count: int | None = Field(None, description="Number of checks configured")

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": "550e8400-e29b-41d4-a716-446655440000",
                "agent_name": "prod-web-01",
                "agent_run_id": "550e8400-e29b-41d4-a716-446655440001",
                "hostname": "web01.example.com",
                "ip_address": "10.0.1.100",
                "version": "1.0.0",
                "tags": ["production", "web"],
                "first_seen": "2024-01-01T00:00:00Z",
                "last_seen": "2024-01-01T12:00:00Z",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T12:00:00Z",
                "is_online": True,
                "uptime_seconds": 43200.0,
                "check_count": 5,
            }
        }
    }


class AgentListResponse(BaseSchema):
    """Schema for listing agents."""

    agents: list[AgentResponse] = Field(..., description="List of agents")
    total: int = Field(..., description="Total number of agents")
    online_count: int = Field(..., description="Number of online agents")
    offline_count: int = Field(..., description="Number of offline agents")


class AgentStatsResponse(BaseSchema):
    """Schema for agent statistics."""

    agent_name: str | None = Field(None, description="Agent name (None until approved)")
    total_checks: int = Field(..., description="Total number of checks executed")
    successful_checks: int = Field(..., description="Number of successful checks")
    failed_checks: int = Field(..., description="Number of failed checks")
    success_rate: float = Field(..., ge=0, le=100, description="Success rate percentage")
    avg_latency_ms: float | None = Field(None, description="Average latency in milliseconds")
    uptime_seconds: float = Field(..., description="Agent uptime in seconds")
    last_check_time: datetime | None = Field(None, description="When last check was executed")

    model_config = {
        "json_schema_extra": {
            "example": {
                "agent_name": "prod-web-01",
                "total_checks": 1000,
                "successful_checks": 995,
                "failed_checks": 5,
                "success_rate": 99.5,
                "avg_latency_ms": 45.2,
                "uptime_seconds": 86400.0,
                "last_check_time": "2024-01-01T12:00:00Z",
            }
        }
    }


class AgentHeartbeat(BaseSchema):
    """Agent heartbeat payload."""

    # Agent identifier
    agent_id: UUID = Field(..., description="Agent UUID from registration")

    # Timestamp
    timestamp: datetime = Field(..., description="Heartbeat timestamp")

    # Agent info
    hostname: str | None = Field(None, description="Agent hostname")
    ip_address: str | None = Field(None, description="Agent IP address")
    version: str | None = Field(None, description="Agent version")
    uptime_seconds: int | None = Field(None, description="Agent uptime in seconds")
    status: AgentStatus = Field(
        AgentStatus.ONLINE, description="Agent status (online/degraded/offline)"
    )

    # Tags
    tags: list[str] | None = Field(None, description="Agent tags for grouping")

    # Check stats
    checks_total: int = Field(0, description="Total checks configured")
    checks_active: int = Field(0, description="Checks currently running")
    checks_executed_count: int = Field(0, description="Total checks executed")
    checks_succeeded_count: int = Field(0, description="Total successful checks")
    checks_failed_count: int = Field(0, description="Total failed checks")

    # Performance metrics
    cpu_percent: float | None = Field(None, description="CPU usage percentage")
    memory_mb: int | None = Field(None, description="Memory usage in MB")
    queue_depth: int = Field(0, description="Current result queue depth")
    queue_max_size: int | None = Field(None, description="Max queue size since last heartbeat")

    # Config state
    config_version: str | None = Field(None, description="Last fetched config version")
    baseline_checks_count: int | None = Field(
        None, description="Number of baseline checks from config"
    )
    remote_checks_count: int | None = Field(
        None, description="Number of remote checks from the server"
    )

    # Job queue stats (NEW)
    jobs_pending: int = Field(0, description="Jobs in queue waiting to run")
    jobs_running: int = Field(0, description="Jobs currently executing")
    jobs_completed_since_last: int = Field(0, description="Jobs completed since last heartbeat")
    jobs_failed_since_last: int = Field(0, description="Jobs failed since last heartbeat")

    # Error tracking
    errors_since_last_heartbeat: int = Field(0, description="Errors since last heartbeat")
    warnings_since_last_heartbeat: int = Field(0, description="Warnings since last heartbeat")

    # Shutdown tracking
    is_shutdown: bool = Field(False, description="True if this is a final shutdown heartbeat")
    agent_run_id: UUID | None = Field(None, description="Unique ID for this agent run session")
    heartbeats_total: int | None = Field(
        None, description="Total heartbeats sent during this session"
    )
    last_error_message: str | None = Field(None, description="Last error message")
    server_unreachable_count: int = Field(0, description="Failed server connections")

    # Reporter backlog metrics
    stored_reports_count: int = Field(0, description="Number of stored reports waiting to send")
    stored_reports_oldest_timestamp: float | None = Field(
        None, description="Timestamp of oldest stored report"
    )

    # Resource monitoring (SWIRL-57: detect subprocess/FD leaks)
    open_file_descriptors: int | None = Field(None, description="Number of open file descriptors")
    fd_limit_soft: int | None = Field(None, description="Soft limit for file descriptors (ulimit)")
    fd_usage_percent: float | None = Field(None, description="FD usage percentage vs soft limit")
    subprocess_count: int | None = Field(None, description="Number of child subprocesses")


class AgentHeartbeatResponse(BaseSchema):
    """Response to agent heartbeat."""

    status: str = Field("ok", description="Response status")
    config_version: str | None = Field(None, description="Latest config version")
    heartbeat_interval: int = Field(60, description="Heartbeat interval in seconds")
    check_sync_interval: int = Field(60, description="Check sync interval in seconds")
    message: str | None = Field(None, description="Optional message to agent")
    jobs: list[dict] = Field(default_factory=list, description="Pending jobs to execute")
    approval_status: AgentApprovalStatus | None = Field(
        None,
        description="Agent approval status (pending/active/paused/disabled/rejected)",
    )

    # Reporter configuration
    report_interval: int | None = Field(None, description="Reporter batch send interval in seconds")
    report_batch_size: int | None = Field(
        None, description="Reporter batch size (results per batch)"
    )
    report_max_files_per_batch: int | None = Field(
        None, description="Max stored reports to process per batch"
    )
    report_process_interval: int | None = Field(
        None, description="Interval for processing stored reports"
    )
    report_max_queue_size: int | None = Field(None, description="Maximum results queue size")
    report_backpressure_threshold: float | None = Field(
        None, description="Backpressure threshold (0.0-1.0)"
    )

    # Performance tuning
    max_concurrent_checks: int | None = Field(None, description="Maximum concurrent checks")
    watchdog_interval: int | None = Field(None, description="Watchdog check interval in seconds")
    watchdog_stall_threshold: int | None = Field(None, description="Watchdog stall threshold count")

    # Logging configuration
    log_level: str | None = Field(
        None, description="Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)"
    )
