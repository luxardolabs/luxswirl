"""
Import/Export schemas — extracted from import_export_router.

Pydantic models for the bulk import/export endpoints. Lives in schemas/ per
the layered architecture standard (no BaseModel subclasses inside routers).
"""

from pydantic import BaseModel, Field

from app.models.enum_model import CheckType
from app.schemas.base_schema import TagList
from app.schemas.check_schema import HttpMethodField


class CheckExport(BaseModel):
    """Check export format."""

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
