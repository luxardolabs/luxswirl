"""
Pydantic schemas for status pages on the admin API.

A page's items are an ordered list of checks and groups, replaced whole. A
group holds either the checks it lists or a filter that picks them when the
page renders, never both. A field the API does not take is refused (422),
never silently dropped.
"""

from datetime import datetime
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from app.models.enum_model import CheckType
from app.schemas.base_schema import NoteText, PlainText, Tag
from app.schemas.paginated_response_schema import PaginatedResponse
from app.schemas.status_page_schema import StatusBarMinutes

# The most items one page holds, and checks one group lists.
MAX_ITEMS = 500
MAX_GROUP_CHECKS = 1000

# A slug is a URL path segment: letters, digits, hyphens and underscores.
SLUG_PATTERN = r"^[A-Za-z0-9_-]+$"

SortBy = Literal["manual", "name", "status", "latency", "uptime"]
SortDirection = Literal["asc", "desc"]

# Stored lower-case, as the web UI stores it.
Slug = Annotated[
    str,
    Field(min_length=1, max_length=255, pattern=SLUG_PATTERN),
    AfterValidator(str.lower),
]

# Fields every page has: a change may set them, never clear them.
_REQUIRED = ("name", "slug", "is_public", "status_bar_minutes")


class GroupFilter(BaseModel):
    """The checks a filter group shows: those matching every field set."""

    model_config = ConfigDict(extra="forbid")

    agent_id: UUID | None = None
    check_type: CheckType | None = None
    tags: list[Tag] = Field(default_factory=list, max_length=50)
    search: PlainText | None = Field(None, max_length=255)


class CheckItem(BaseModel):
    """One check, shown on its own."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["check"]
    check_id: UUID


class GroupItem(BaseModel):
    """A named group: the checks it lists, or those its filter picks."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["group"]
    name: PlainText = Field(..., min_length=1, max_length=255)
    collapsed: bool = False
    sort_by: SortBy = "manual"
    sort_direction: SortDirection = "asc"
    checks: list[UUID] | None = Field(None, max_length=MAX_GROUP_CHECKS)
    filter: GroupFilter | None = None

    @model_validator(mode="after")
    def _checks_or_filter(self) -> Self:
        if (self.checks is None) == (self.filter is None):
            raise ValueError("A group takes either checks or a filter, not both")
        return self


StatusPageItem = Annotated[CheckItem | GroupItem, Field(discriminator="type")]


class AdminStatusPageItems(BaseModel):
    """A page's whole item list, in display order."""

    model_config = ConfigDict(extra="forbid")

    items: list[StatusPageItem] = Field(..., max_length=MAX_ITEMS)


class AdminStatusPageCreate(BaseModel):
    """A new status page. Private unless `is_public`: a public page is open to
    anyone, signed in or not."""

    model_config = ConfigDict(extra="forbid")

    name: PlainText = Field(..., min_length=1, max_length=255)
    slug: Slug
    description: NoteText | None = Field(None, max_length=1000)
    is_public: bool = False
    status_bar_minutes: StatusBarMinutes = 30
    items: list[StatusPageItem] = Field(default_factory=list, max_length=MAX_ITEMS)


class AdminStatusPageUpdate(BaseModel):
    """A change to a page: the fields sent; a field left out is unchanged, and
    `description` sent as null is cleared. Items are replaced on their own
    route."""

    model_config = ConfigDict(extra="forbid")

    name: PlainText | None = Field(None, min_length=1, max_length=255)
    slug: Slug | None = None
    description: NoteText | None = Field(None, max_length=1000)
    is_public: bool | None = None
    status_bar_minutes: StatusBarMinutes | None = None

    @model_validator(mode="after")
    def _required_stay_set(self) -> Self:
        cleared = [
            f
            for f in _REQUIRED
            if f in self.model_fields_set and getattr(self, f) is None
        ]
        if cleared:
            raise ValueError(f"These fields cannot be null: {', '.join(cleared)}")
        return self


class AdminStatusPage(BaseModel):
    """A status page, with its items as stored, in display order."""

    id: UUID
    name: str
    slug: str
    description: str | None
    is_public: bool
    status_bar_minutes: int
    items: list[dict[str, Any]]
    created_at: datetime
    updated_at: datetime


class StatusPagePage(PaginatedResponse[AdminStatusPage]):
    """One page of status pages."""
