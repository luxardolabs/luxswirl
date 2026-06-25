"""
Pydantic schemas for StatusPage domain.
"""

from typing import Any, Literal
from uuid import UUID

from pydantic import Field, field_validator

from app.schemas.base import BaseSchema, TimestampSchema


class StatusPageCheckItem(BaseSchema):
    """Schema for a check item in a status page."""

    type: Literal["check"] = Field("check", description="Item type")
    check_id: UUID = Field(
        ..., description="UUID of the check to display (stored as string in JSONB)"
    )
    order: int = Field(..., ge=0, description="Display order")

    model_config = {
        "json_schema_extra": {
            "example": {
                "type": "check",
                "check_id": "550e8400-e29b-41d4-a716-446655440000",
                "order": 0,
            }
        }
    }


class StatusPageGroupItem(BaseSchema):
    """Schema for a group item in a status page."""

    type: Literal["group"] = Field("group", description="Item type")
    name: str = Field(..., min_length=1, max_length=255, description="Group display name")
    order: int = Field(..., ge=0, description="Display order")
    filter: dict[str, Any] = Field(..., description="Filter configuration for checks in this group")
    collapsed: bool = Field(False, description="Whether group is collapsed by default")

    model_config = {
        "json_schema_extra": {
            "example": {
                "type": "group",
                "name": "Production API Services",
                "order": 1,
                "filter": {"agent_id": "prod-api-01", "tags": ["production", "api"]},
                "collapsed": False,
            }
        }
    }


class StatusPageBase(BaseSchema):
    """Base schema for StatusPage with common fields."""

    name: str = Field(
        ..., min_length=1, max_length=255, description="Display name of the status page"
    )
    slug: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="URL-friendly slug (e.g., /status/production)",
    )
    description: str | None = Field(None, max_length=1000, description="Optional description")
    is_public: bool = Field(True, description="Whether the status page is publicly accessible")

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, v: str) -> str:
        """Validate slug is URL-safe."""
        if not v.replace("-", "").replace("_", "").isalnum():
            raise ValueError(
                "Slug must contain only alphanumeric characters, hyphens, and underscores"
            )
        return v.lower()


class StatusPageCreate(StatusPageBase):
    """Schema for creating a new status page."""

    config: dict[str, Any] | None = Field(
        None, description="Display configuration (theme, show_uptime, etc.)"
    )
    items: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Ordered array of items to display (checks and groups)",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "name": "Production Status",
                "slug": "production",
                "description": "Status page for all production services",
                "is_public": True,
                "config": {
                    "theme": "dark",
                    "show_uptime": True,
                    "refresh_interval": 10,
                },
                "items": [
                    {
                        "type": "check",
                        "check_id": "550e8400-e29b-41d4-a716-446655440000",
                        "order": 0,
                    },
                    {
                        "type": "group",
                        "name": "API Services",
                        "order": 1,
                        "filter": {"tags": ["api", "production"]},
                        "collapsed": False,
                    },
                ],
            }
        }
    }


class StatusPageUpdate(BaseSchema):
    """Schema for updating a status page."""

    name: str | None = Field(None, min_length=1, max_length=255)
    slug: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = Field(None, max_length=1000)
    is_public: bool | None = None
    config: dict[str, Any] | None = None
    items: list[dict[str, Any]] | None = None

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, v: str | None) -> str | None:
        """Validate slug is URL-safe if provided."""
        if v is None:
            return v
        if not v.replace("-", "").replace("_", "").isalnum():
            raise ValueError(
                "Slug must contain only alphanumeric characters, hyphens, and underscores"
            )
        return v.lower()

    model_config = {
        "json_schema_extra": {
            "example": {
                "name": "Production Status (Updated)",
                "description": "Updated description",
                "is_public": False,
            }
        }
    }


class StatusPageInDB(StatusPageBase, TimestampSchema):
    """Schema for status page in database."""

    id: UUID = Field(..., description="Status Page UUID")
    config: dict[str, Any] | None = None
    items: list[dict[str, Any]] = Field(default_factory=list)


class StatusPageResponse(StatusPageInDB):
    """Schema for status page API responses."""

    check_count: int = Field(0, description="Number of direct check items")
    group_count: int = Field(0, description="Number of group items")

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": "550e8400-e29b-41d4-a716-446655440010",
                "name": "Production Status",
                "slug": "production",
                "description": "Status page for all production services",
                "is_public": True,
                "config": {
                    "theme": "dark",
                    "show_uptime": True,
                    "refresh_interval": 10,
                },
                "items": [
                    {
                        "type": "check",
                        "check_id": "550e8400-e29b-41d4-a716-446655440000",
                        "order": 0,
                    },
                    {
                        "type": "group",
                        "name": "API Services",
                        "order": 1,
                        "filter": {"tags": ["api", "production"]},
                        "collapsed": False,
                    },
                ],
                "check_count": 1,
                "group_count": 1,
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T12:00:00Z",
            }
        }
    }


class StatusPageListResponse(BaseSchema):
    """Schema for listing status pages."""

    status_pages: list[StatusPageResponse] = Field(..., description="List of status pages")
    total: int = Field(..., description="Total number of status pages")
    public_count: int = Field(..., description="Number of public status pages")
    private_count: int = Field(..., description="Number of private status pages")


class StatusPageAddCheckRequest(BaseSchema):
    """Schema for adding a check to a status page."""

    check_id: UUID = Field(..., description="UUID of the check to add")
    order: int | None = Field(None, ge=0, description="Display order (defaults to end)")

    model_config = {
        "json_schema_extra": {
            "example": {"check_id": "550e8400-e29b-41d4-a716-446655440000", "order": 0}
        }
    }


class StatusPageAddGroupRequest(BaseSchema):
    """Schema for adding a group to a status page."""

    name: str = Field(..., min_length=1, max_length=255, description="Group display name")
    filter: dict[str, Any] = Field(..., description="Filter configuration for checks")
    order: int | None = Field(None, ge=0, description="Display order (defaults to end)")
    collapsed: bool = Field(False, description="Whether group is collapsed by default")

    model_config = {
        "json_schema_extra": {
            "example": {
                "name": "Production API Services",
                "filter": {"agent_id": "prod-api-01", "tags": ["production", "api"]},
                "order": 1,
                "collapsed": False,
            }
        }
    }


class StatusPageReorderRequest(BaseSchema):
    """Schema for reordering items in a status page."""

    from_index: int = Field(..., ge=0, description="Current position of item")
    to_index: int = Field(..., ge=0, description="New position for item")

    model_config = {"json_schema_extra": {"example": {"from_index": 0, "to_index": 2}}}
