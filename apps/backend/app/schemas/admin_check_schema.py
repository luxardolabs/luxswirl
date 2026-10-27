"""
Pydantic schemas for checks on the admin API.

A check is addressed by its own id there, so the agent it runs on is a field
of the body, not a segment of the path. A body field the API does not take is
refused (422), never silently dropped, and a field every check must have
cannot be set to null.
"""

from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enum_model import ImportMode
from app.schemas.check_schema import (
    BulkCheckCreateRequest,
    CheckCreate,
    CheckResponse,
    CheckUpdate,
)
from app.schemas.import_export_schema import CheckExport
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


# The most checks one bulk request names.
BULK_MAX = 10_000


class AdminBulkChecks(BaseModel):
    """The checks a bulk action applies to, by id."""

    model_config = ConfigDict(extra="forbid")

    check_ids: list[UUID] = Field(..., min_length=1, max_length=BULK_MAX)

    @property
    def ids(self) -> list[UUID]:
        """`check_ids` once each, in the order sent."""
        return list(dict.fromkeys(self.check_ids))


class AdminBulkModify(AdminBulkChecks):
    """What a bulk modify changes on every check it names: at least one field."""

    interval_seconds: int | None = Field(None, gt=0, le=86400)
    timeout_seconds: int | None = Field(None, gt=0, le=300)
    retry_attempts: int | None = Field(None, ge=0, le=10)
    agent_id: UUID | None = Field(None, description="Move the checks to this agent")
    alert_id: UUID | None = Field(None, description="Assign this alert to the checks")

    @model_validator(mode="after")
    def _changes_something(self) -> Self:
        if not self.model_fields_set - {"check_ids"}:
            raise ValueError("A bulk modify needs at least one field to change")
        cleared = [
            f for f in self.model_fields_set - {"check_ids"} if getattr(self, f) is None
        ]
        if cleared:
            raise ValueError(f"These fields cannot be null: {', '.join(cleared)}")
        return self

    def update_fields(self) -> CheckUpdate:
        """The per-check field changes, as the bulk modify job applies them."""
        return CheckUpdate(
            **self.model_dump(
                include={"interval_seconds", "timeout_seconds", "retry_attempts"},
                exclude_unset=True,
            )
        )


class BulkCheckResult(BaseModel):
    """What a bulk action did: every check named is a success or a failure."""

    success_count: int
    failure_count: int
    errors: list[str]


class AdminCheckDependents(BaseModel):
    """The whole set of checks that depend on a check; any other is detached."""

    model_config = ConfigDict(extra="forbid")

    dependent_ids: list[UUID] = Field(default_factory=list, max_length=BULK_MAX)


class DependentsChange(BaseModel):
    """How many dependents a replacement attached and detached."""

    added: int
    removed: int


class AdminBulkCreateItem(BulkCheckCreateRequest):
    """One URL to make a check from; a field it does not take is refused."""

    model_config = ConfigDict(extra="forbid")


class AdminBulkCreate(BaseModel):
    """One check per URL on an agent, each given the alerts named."""

    model_config = ConfigDict(extra="forbid")

    agent_id: UUID = Field(..., description="The agent the new checks run on")
    items: list[AdminBulkCreateItem] = Field(..., min_length=1, max_length=BULK_MAX)
    alert_ids: list[UUID] = Field(default_factory=list, max_length=100)


class AdminCheckImport(BaseModel):
    """Checks to import onto an agent, in the export's form.

    Every entry is held to that form here, so one the import cannot take is a
    422 naming it and nothing is queued. A check the database then refuses
    when the job runs is that entry's error, never the rest's.
    """

    model_config = ConfigDict(extra="forbid")

    mode: ImportMode = Field(
        ImportMode.MERGE,
        description="`merge` skips a check whose name the agent has; `replace` overwrites it",
    )
    checks: list[CheckExport] = Field(..., min_length=1, max_length=BULK_MAX)
