"""
Import/Export schemas — extracted from import_export_router.

Pydantic models for the bulk import/export endpoints. Lives in schemas/ per
the layered architecture standard (no BaseModel subclasses inside routers).
"""

from uuid import UUID

from pydantic import BaseModel, Field


class CheckExport(BaseModel):
    """Check export format."""

    name: str
    check_type: str
    target: str
    interval: int = Field(default=60, description="Interval in seconds")
    timeout: int = Field(default=5, description="Timeout in seconds")
    retry_attempts: int = Field(default=2, description="Number of retry attempts")
    enabled: bool = Field(default=True, description="Whether check is enabled")
    description: str | None = None
    http_method: str | None = None
    expected_status: int | None = None
    json_path: str | None = None
    expected_value: str | None = None
    tags: list[str] | None = None


class BulkImportRequest(BaseModel):
    """Bulk import request."""

    agent_id: UUID = Field(..., description="Agent ID to assign checks to")
    checks: list[CheckExport] = Field(..., description="List of checks to import")
    overwrite: bool = Field(default=False, description="Overwrite existing checks")


class BulkImportResponse(BaseModel):
    """Bulk import response."""

    total: int
    created: int
    updated: int
    skipped: int
    errors: list[dict[str, str]]


class AgentChecksExport(BaseModel):
    """An agent's checks, exported for backup or transfer to another agent."""

    agent_id: UUID
    agent_hostname: str | None = None
    total_checks: int
    checks: list[CheckExport]


class AgentConfigExport(BaseModel):
    """The bundled default agent config's checks, for seeding a new install.

    Separate from AgentChecksExport because this one describes a config file,
    not a registered agent: its `agent_id` is the config's agent NAME
    ("docker-agent"), which is not a UUID. Declaring it as one made the route
    fail response validation on every call.
    """

    agent_id: str
    total_checks: int
    checks: list[CheckExport]
