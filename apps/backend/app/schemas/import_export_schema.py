"""
Import/Export schemas — extracted from import_export_router.

Pydantic models for the bulk import/export endpoints. Lives in schemas/ per
the layered architecture standard (no BaseModel subclasses inside routers).
"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enum_model import CheckType
from app.schemas.base_schema import TagList
from app.schemas.check_schema import HttpMethodField


class CheckExport(BaseModel):
    """Check export format.

    A field it does not name is refused, so an entry carrying a setting the
    import cannot take is that entry's error, never a check made without it.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    check_type: CheckType
    target: str
    interval: int = Field(default=60, description="Interval in seconds")
    timeout: int = Field(default=5, description="Timeout in seconds")
    retry_attempts: int = Field(default=2, description="Number of retry attempts")
    enabled: bool = Field(default=True, description="Whether check is enabled")
    description: str | None = None
    http_method: HttpMethodField = None
    expected_status: int | None = None
    json_path: str | None = None
    expected_value: str | None = None
    tags: TagList | None = None


class CheckExportDocument(BaseModel):
    """An agent's checks, exported: the document an import takes back."""

    agent_id: UUID
    agent_name: str | None
    agent_hostname: str | None
    total_checks: int
    checks: list[CheckExport]
