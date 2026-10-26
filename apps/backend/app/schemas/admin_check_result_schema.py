"""
Pydantic schemas for a check's results on the admin API.

A result is read in three shapes: a page of raw results, one result whole
(with the response body and its artifacts' metadata), and the time-bucketed
series and summary the check detail panel draws.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.enum_model import CheckErrorType
from app.schemas.check_artifact_schema import CheckArtifactResponse
from app.schemas.paginated_response_schema import PaginatedResponse


class AdminCheckResult(BaseModel):
    """One execution of a check, as the agent reported it."""

    id: UUID
    check_id: UUID
    agent_id: UUID
    timestamp: datetime
    success: bool
    latency_ms: float | None = None
    error: str | None = None
    error_type: CheckErrorType | None = None
    http_status_code: int | None = None
    http_response_time_ms: float | None = None
    metrics: dict[str, Any] | None = Field(
        None,
        description="The check type's own measurements (DNS records, row counts, ...)",
    )


class AdminCheckResultDetail(AdminCheckResult):
    """One result whole: its response body (not for a viewer) and its artifacts."""

    response_data: str | None = None
    artifacts: list[CheckArtifactResponse] = Field(default_factory=list)


class CheckResultPage(PaginatedResponse[AdminCheckResult]):
    """One page of a check's results, newest first."""


class CheckHistoryPoint(BaseModel):
    """One time bucket: whether every result in it passed, their mean latency."""

    bucket_time: datetime
    success: bool | None = None
    latency_ms: float | None = None
    error: str | None = None


class CheckHistory(BaseModel):
    """A check's results over the last `hours`, in time buckets (at most 500)."""

    check_id: UUID
    hours: int
    points: list[CheckHistoryPoint]


class CheckSummary(BaseModel):
    """A check's results over the last `hours`, counted, and its latest result."""

    check_id: UUID
    hours: int
    total: int
    successful: int
    uptime_percent: float | None = Field(
        None, description="successful / total x 100; null with no results"
    )
    avg_latency_ms: float | None = None
    min_latency_ms: float | None = None
    max_latency_ms: float | None = None
    latest: AdminCheckResult | None = None
