"""
Pydantic schemas for Check domain.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field

from app.models.enum_model import AssignmentMode, CheckType
from app.schemas.base_schema import BaseSchema, TimestampSchema


class CheckBase(BaseSchema):
    """Base schema for Check with common fields."""

    display_name: str = Field(
        ..., min_length=1, max_length=255, description="Display name of the check"
    )
    # CheckType IS the validation — bad value is a 422, no hand-maintained list.
    check_type: CheckType = Field(..., description="Type of check")
    target: str = Field(..., min_length=1, max_length=512, description="Check target")
    description: str | None = Field(
        None, max_length=1000, description="Check description"
    )


class CheckCreate(CheckBase):
    """Schema for creating a new check."""

    interval_seconds: int | None = Field(
        None, gt=0, le=86400, description="Check interval in seconds"
    )
    timeout_seconds: int = Field(
        10, gt=0, le=300, description="Check timeout in seconds"
    )
    enabled: bool = Field(True, description="Whether check is enabled")

    # Retry configuration
    retry_attempts: int | None = Field(
        None,
        ge=0,
        le=10,
        description="Number of retry attempts for a single check execution before marking as failed",
    )
    retry_interval_seconds: int = Field(
        30,
        ge=1,
        le=300,
        description="Retry interval in seconds (Heartbeat Retry Interval)",
    )
    resend_notification_after: int | None = Field(
        None,
        ge=1,
        le=100,
        description="Resend notification if down X times consecutively (NULL = disabled)",
    )
    tags: list[str] | None = Field(
        None, description="Tags for organizing/filtering checks"
    )

    # Agent assignment (Phase 2)
    assignment_mode: AssignmentMode = Field(
        AssignmentMode.MANUAL,
        description="Assignment mode: manual, replicate, distribute",
    )
    agent_selector: dict | None = Field(
        None, description="Agent selector for replicate/distribute modes"
    )

    # Synthetic check script
    script_code: str | None = Field(
        None, description="Python script code for synthetic checks (Playwright async)"
    )

    # Check-type-specific configuration (JSONB - accepts any fields)
    # These fields come from UI forms and get packed into check_config
    http_method: str | None = Field(None, max_length=10, description="HTTP method")
    verify_ssl: bool | None = Field(None, description="Verify SSL certificates")
    expected_status: int | None = Field(
        None, ge=100, le=599, description="Expected HTTP status"
    )
    json_path: str | None = Field(None, max_length=500, description="JSON path")
    expected_value: str | None = Field(
        None, max_length=500, description="Expected value"
    )
    record_type: str | None = Field(None, max_length=10, description="DNS record type")
    nameserver: str | None = Field(None, max_length=255, description="DNS nameserver")
    port: int | None = Field(None, ge=1, le=65535, description="Port number")
    expect_value: str | None = Field(
        None, max_length=500, description="Expected DNS value"
    )
    connection_string: str | None = Field(
        None, max_length=1000, description="Database connection string"
    )
    query: str | None = Field(None, max_length=5000, description="SQL query")

    depends_on_check_id: UUID | None = Field(
        None,
        description="Parent check this one depends on; notifications suppressed when parent is down",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "display_name": "api_health",
                "check_type": "http",
                "target": "https://api.example.com/health",
                "description": "Check API health endpoint",
                "interval_seconds": 60,
                "timeout_seconds": 10,
                "expected_status": 200,
                "enabled": True,
                "http_method": "GET",
                "tags": ["production", "api"],
            }
        }
    }


class CheckUpdate(BaseSchema):
    """Schema for updating a check."""

    display_name: str | None = Field(None, min_length=1, max_length=255)
    check_type: CheckType | None = None
    target: str | None = None
    description: str | None = None
    interval_seconds: int | None = Field(None, gt=0, le=86400)
    timeout_seconds: int | None = Field(None, gt=0, le=300)
    enabled: bool | None = None
    retry_attempts: int | None = Field(None, ge=0, le=10)
    retry_interval_seconds: int | None = Field(None, ge=1, le=300)
    resend_notification_after: int | None = Field(None, ge=1, le=100)
    tags: list[str] | None = None

    # Agent assignment (Phase 2)
    assignment_mode: AssignmentMode | None = None
    agent_selector: dict | None = None

    # Synthetic check script
    script_code: str | None = None

    # Check-type-specific configuration (JSONB - accepts any fields)
    http_method: str | None = Field(None, max_length=10)
    verify_ssl: bool | None = None
    expected_status: int | None = Field(None, ge=100, le=599)
    json_path: str | None = Field(None, max_length=500)
    expected_value: str | None = Field(None, max_length=500)
    record_type: str | None = Field(None, max_length=10)
    nameserver: str | None = Field(None, max_length=255)
    port: int | None = Field(None, ge=1, le=65535)
    expect_value: str | None = Field(None, max_length=500)
    connection_string: str | None = Field(None, max_length=1000)
    query: str | None = Field(None, max_length=5000)
    depends_on_check_id: UUID | None = Field(
        None,
        description="Parent check this one depends on; notifications suppressed when parent is down",
    )


class CheckInDB(CheckBase, TimestampSchema):
    """Schema for check in database."""

    id: UUID = Field(..., description="Check UUID")
    agent_id: UUID = Field(..., description="Foreign key to agent (UUID)")
    interval_seconds: int | None = None
    timeout_seconds: int | None = None
    expected_status: int | None = None
    enabled: bool = True
    # HTTP/JSON check fields
    http_method: str | None = None
    verify_ssl: bool | None = None
    json_path: str | None = None
    expected_value: str | None = None
    # DNS check fields
    record_type: str | None = None
    nameserver: str | None = None
    port: int | None = None
    expect_value: str | None = None
    # MySQL/Postgres check fields
    connection_string: str | None = None
    query: str | None = None
    # Common fields
    retry_attempts: int | None = None
    retry_interval_seconds: int | None = None
    resend_notification_after: int | None = None
    tags: list[str] | None = None
    script_code: str | None = None
    depends_on_check_id: UUID | None = None


class CheckResponse(CheckInDB):
    """Schema for check API responses."""

    fully_qualified_name: str = Field(
        ..., description="Fully qualified check name (agent:check)"
    )
    latest_status: str | None = Field(None, description="Latest check status")
    latest_latency_ms: float | None = Field(
        None, description="Latest latency in milliseconds"
    )
    success_rate_24h: float | None = Field(
        None, description="24-hour success rate percentage"
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": "550e8400-e29b-41d4-a716-446655440000",
                "agent_id": "550e8400-e29b-41d4-a716-446655440001",
                "display_name": "api_health",
                "check_type": "http",
                "target": "https://api.example.com/health",
                "description": "Check API health endpoint",
                "interval_seconds": 60,
                "timeout_seconds": 10,
                "expected_status": 200,
                "enabled": True,
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T12:00:00Z",
                "fully_qualified_name": "prod-web-01:api_health",
                "latest_status": "success",
                "latest_latency_ms": 45.2,
                "success_rate_24h": 99.5,
            }
        }
    }


class CheckListResponse(BaseSchema):
    """Schema for listing checks."""

    checks: list[CheckResponse] = Field(..., description="List of checks")
    total: int = Field(..., description="Total number of checks")
    enabled_count: int = Field(..., description="Number of enabled checks")
    disabled_count: int = Field(..., description="Number of disabled checks")


class BulkCheckCreateRequest(BaseSchema):
    """Schema for bulk check creation request (single item)."""

    url: str = Field(
        ..., min_length=1, max_length=512, description="URL or target to check"
    )
    display_name: str | None = Field(
        None,
        max_length=255,
        description="Display name (auto-generated if not provided)",
    )
    interval_seconds: int | None = Field(
        None, gt=0, le=86400, description="Check interval in seconds"
    )
    timeout_seconds: int | None = Field(
        10, gt=0, le=300, description="Check timeout in seconds"
    )
    enabled: bool = Field(True, description="Whether check is enabled")
    tags: list[str] | None = Field(
        None, description="Tags for organizing/filtering checks"
    )
    expected_status: int | None = Field(
        None, ge=100, le=599, description="Expected HTTP status code"
    )
    http_method: str | None = Field(
        None, max_length=10, description="HTTP method (GET, POST, etc.)"
    )
    verify_ssl: bool | None = Field(
        True, description="Verify SSL certificates for HTTPS"
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "url": "https://api.example.com/health",
                "display_name": "api-health",
                "interval_seconds": 60,
                "enabled": True,
                "tags": ["production", "api"],
            }
        }
    }


class BulkCheckResult(BaseSchema):
    """Schema for individual bulk check result."""

    url: str = Field(..., description="The input URL")
    status: str = Field(..., description="Result status: 'success' or 'failed'")
    check_id: UUID | None = Field(None, description="Check UUID if successful")
    display_name: str | None = Field(None, description="Name assigned to the check")
    check_type: str | None = Field(
        None, description="Detected check type (http, https, tcp, etc.)"
    )
    error: str | None = Field(None, description="Error message if failed")

    model_config = {
        "json_schema_extra": {
            "example": {
                "url": "https://api.example.com/health",
                "status": "success",
                "check_id": "550e8400-e29b-41d4-a716-446655440000",
                "display_name": "api-health",
                "check_type": "https",
                "error": None,
            }
        }
    }


class BulkCheckCreateResponse(BaseSchema):
    """Schema for bulk check creation response."""

    total: int = Field(..., description="Total number of checks requested")
    succeeded: int = Field(..., description="Number of successfully created checks")
    failed: int = Field(..., description="Number of failed checks")
    results: list[BulkCheckResult] = Field(
        ..., description="Detailed results for each check"
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "total": 3,
                "succeeded": 2,
                "failed": 1,
                "results": [
                    {
                        "url": "https://api.example.com/health",
                        "status": "success",
                        "check_id": "550e8400-e29b-41d4-a716-446655440000",
                        "display_name": "api-health",
                        "check_type": "https",
                        "error": None,
                    },
                    {
                        "url": "invalid-url",
                        "status": "failed",
                        "check_id": None,
                        "display_name": None,
                        "check_type": None,
                        "error": "Invalid URL format",
                    },
                ],
            }
        }
    }


@dataclass(frozen=True)
class DashboardCheckAgent:
    """The agent as a dashboard row names it.

    Flat values, not the ORM relationship: reading `check.agent.tags` off a
    detached Check lazy-loads outside the session and raises MissingGreenlet.
    """

    id: UUID
    agent_name: str | None
    tags: list[str]


@dataclass(frozen=True)
class DashboardCheck:
    """One check on a dashboard or public status page, status included.

    Field names match what the templates already read, because this replaces
    ORM Check objects that had `latest_status`, `latest_success` and
    `latency_ms` setattr'd onto them — display data written onto a database
    row. The status was always available on the StatusRow beside it, so this is
    projected from that and the checks are no longer re-fetched at all.
    """

    id: UUID
    display_name: str
    check_type: str
    target: str
    tags: list[str]
    enabled: bool
    agent: DashboardCheckAgent
    # Status, computed from results rather than columns on the check.
    latest_status: str
    latest_success: bool | None
    latency_ms: float | None
    uptime_24h: float

    # Filled in by a later pass over the same rows (the bulk uptime/bars
    # queries), via dataclasses.replace rather than mutation — the whole point
    # of the frozen dataclass is that a display value cannot be written onto
    # something that looks like a database row.
    uptime_7d: float = 100.0
    uptime_30d: float = 100.0
    uptime_90d: float = 100.0
    last_failure_at: datetime | None = None
    minute_bars: list[dict[str, Any]] = field(default_factory=list)
    sparkline_path: str = ""


@dataclass(frozen=True)
class CheckFormUrls:
    """Finished hrefs for the check form / dependents panels.

    A dataclass, not a dict: Jinja resolves `x.update` to dict.update — the bound
    METHOD — before it ever tries the key, so a dict silently renders
    "<built-in method update of dict object …>" into the form's hx-post.
    """

    clone: str
    update: str
    dependents: str
    update_dependents: str


@dataclass
class BulkCheckCreateSummary:
    """How many checks a bulk-create made and how many it passed over.

    Distinct from BulkCheckResult above, which is ONE row of a bulk create —
    a single URL and what became of it. This is the tally for the whole run.
    """

    created_count: int
    skipped_count: int
