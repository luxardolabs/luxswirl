"""
Pydantic schemas for checks on the admin API.

A check is addressed by its own id there, so the agent it runs on is a field
of the body, not a segment of the path. A body field the API does not take is
refused (422), never silently dropped, and a field every check must have
cannot be set to null.
"""

from typing import Self
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from app.schemas.check_schema import CheckCreate, CheckResponse, CheckUpdate
from app.schemas.paginated_response_schema import PaginatedResponse

# Fields every check has: a change may set them, never clear them.
_REQUIRED = ("display_name", "check_type", "target", "enabled", "timeout_seconds")


class AdminCheckCreate(CheckCreate):
    """A new check, and the agent it belongs to."""

    model_config = ConfigDict(extra="forbid")

    agent_id: UUID = Field(..., description="The agent the check belongs to")

    def to_check_create(self) -> CheckCreate:
        return CheckCreate(**self.model_dump(exclude={"agent_id"}, exclude_unset=True))


class AdminCheckUpdate(CheckUpdate):
    """A change to a check: only the fields sent, none of them cleared if required."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _required_not_cleared(self) -> Self:
        cleared = [
            f
            for f in _REQUIRED
            if f in self.model_fields_set and getattr(self, f) is None
        ]
        if cleared:
            raise ValueError(f"These fields cannot be null: {', '.join(cleared)}")
        return self


class AdminCheckClone(AdminCheckUpdate):
    """What a clone changes from its source, and the agent it goes to."""

    agent_id: UUID | None = Field(
        None, description="The agent the clone belongs to; the source's when omitted"
    )

    def overrides(self) -> CheckUpdate:
        return CheckUpdate(**self.model_dump(exclude={"agent_id"}, exclude_unset=True))


class CheckPage(PaginatedResponse[CheckResponse]):
    """One page of checks."""
