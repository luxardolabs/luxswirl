"""
Pydantic schemas for checks on the admin API.

A check is addressed by its own id there, so the agent it runs on is a field
of the body, not a segment of the path.
"""

from uuid import UUID

from pydantic import Field

from app.schemas.check_schema import CheckCreate, CheckResponse, CheckUpdate
from app.schemas.paginated_response_schema import PaginatedResponse


class AdminCheckCreate(CheckCreate):
    """A new check, and the agent it belongs to."""

    agent_id: UUID = Field(..., description="The agent the check belongs to")

    def to_check_create(self) -> CheckCreate:
        return CheckCreate(**self.model_dump(exclude={"agent_id"}, exclude_unset=True))


class AdminCheckClone(CheckUpdate):
    """What a clone changes from its source, and the agent it goes to."""

    agent_id: UUID | None = Field(
        None, description="The agent the clone belongs to; the source's when omitted"
    )

    def overrides(self) -> CheckUpdate:
        return CheckUpdate(**self.model_dump(exclude={"agent_id"}, exclude_unset=True))


class CheckPage(PaginatedResponse[CheckResponse]):
    """One page of checks."""
