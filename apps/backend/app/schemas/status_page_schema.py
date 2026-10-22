"""
Pydantic schemas for StatusPage domain.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import Field, field_validator

from app.schemas.base_schema import BaseSchema, TimestampSchema


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
    description: str | None = Field(
        None, max_length=1000, description="Optional description"
    )
    is_public: bool = Field(
        True, description="Whether the status page is publicly accessible"
    )

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

    # Derived from `items`, which this schema already carries — no extra query,
    # and no relationship to lazy-load. The list page reads both; they were ORM
    # properties, so the seam conversion left the template calling for something
    # the schema did not have and /status-pages raised UndefinedError.
    @property
    def check_count(self) -> int:
        """Count of direct check items (not including checks inside groups)."""
        return sum(1 for item in self.items if item.get("type") == "check")

    @property
    def group_count(self) -> int:
        """Count of group items."""
        return sum(1 for item in self.items if item.get("type") == "group")


@dataclass(frozen=True)
class StatusPageUrls:
    """Finished hrefs for one status page. Built by route NAME so a router path
    change propagates on its own (fw.url_assembly_in_view)."""

    edit_form: str
    manage: str
    delete: str
    update: str
    available_checks: str
    public: str
    reorder: str
    add_check: str
    add_group: str


@dataclass(frozen=True)
class DashboardItemUrls:
    """Finished hrefs for one dashboard item. The item routes address an item by
    its position, so each href is built for the index the item renders at."""

    remove: str
    rename_group: str
    update_group_filters: str
    sort: str


class StatusPageRow(StatusPageInDB):
    """A status page as the list page shows it, with its links."""

    urls: StatusPageUrls
