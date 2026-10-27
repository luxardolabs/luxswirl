"""
Pydantic schemas for agent jobs on the admin API.

A job's params are held to its type's schema by the core service, so the
request takes them as a mapping. A field the API does not take is refused
(422), never silently dropped.
"""

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enum_model import JobType, ScanCheckKind
from app.schemas.base_schema import TagList
from app.schemas.job_schema import JobResponse
from app.schemas.paginated_response_schema import PaginatedResponse


class JobPage(PaginatedResponse[JobResponse]):
    """One page of jobs."""


class AdminJobCreate(BaseModel):
    """A new job: its type, the agent that runs it, and its params."""

    model_config = ConfigDict(extra="forbid")

    job_type: JobType
    agent_id: UUID | None = Field(
        None, description="The agent that runs the job; every job type needs one"
    )
    params: dict[str, Any] = Field(
        default_factory=dict[str, Any],
        description="The job type's params, as `GET /jobs/types` describes them",
    )
    priority: int = Field(0, ge=0, le=100, description="Higher runs first")
    tags: TagList | None = None


class JobTypeInfo(BaseModel):
    """A job type: what it does, where it runs, and the params it takes."""

    job_type: JobType
    display_name: str
    description: str
    requires_agent: bool
    timeout_seconds: int = Field(description="The longest the whole job may run")
    params_schema: dict[str, Any] = Field(description="JSON Schema of `params`")


class AdminJobChecks(BaseModel):
    """Checks to create from a network scan's hosts, on one agent.

    A setting left out takes the check defaults from Settings.
    """

    model_config = ConfigDict(extra="forbid")

    kind: ScanCheckKind
    agent_id: UUID = Field(description="The agent the new checks run on")
    interval_seconds: int | None = Field(None, gt=0, le=86400)
    timeout_seconds: int | None = Field(None, gt=0, le=300)
    retry_attempts: int | None = Field(None, ge=0, le=10)
    expected_status: int | None = Field(
        None, ge=100, le=599, description="Web checks only"
    )
    verify_ssl: bool | None = Field(None, description="Web checks only")
    tags: TagList = Field(default_factory=list[str])


class JobChecksCreated(BaseModel):
    """What a create-checks request made: a host whose check exists is skipped."""

    created: int
    skipped: int
