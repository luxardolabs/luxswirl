"""Typed query-parameter filters — the one standard for enum-ish filters.

A closed-set filter (status / type / …) is typed as its **enum**, so FastAPI
validates it at the boundary (a bad value is a 422, the allowed values land in
OpenAPI) and nothing downstream ever sees a loose string. "No filter" is the
absence of the param or an empty string (the dropdown's "All" option), which a
single ``BeforeValidator`` normalizes to ``None`` before enum validation.

The enum is the single source of truth: add a member and the write-validator,
the dropdown, and the filter here all pick it up. Reference filters (an id, not
a closed set) are typed ``UUID | None`` so a malformed id is a 422 at the edge
instead of a 500 from a downstream ``::uuid`` cast.

A router then declares e.g. ``status: JobStatusFilter = None`` and does nothing
else — the value arrives already validated and normalized. The ``empty_to_none``
validator is exported for the rare param that also needs a custom ``alias=``.
"""

from typing import Annotated
from uuid import UUID

from fastapi import Query
from pydantic import BeforeValidator

from app.models.enum_model import (
    CheckHealthStatus,
    CheckType,
    JobStatus,
    JobType,
    NotificationProviderType,
    NotificationStatus,
)


def empty_to_none(v: object) -> object:
    """Normalize the dropdown's "All" option ('') to None before enum validation."""
    return None if isinstance(v, str) and v.strip() == "" else v


def split_csv(value: str | None) -> list[str] | None:
    """Split a comma-separated filter value into a clean list (empty -> None).

    Used for multi-value filters whose wire form is a CSV string (e.g. ``?tags=a,b``).
    """
    if not value:
        return None
    items = [v.strip() for v in value.split(",") if v.strip()]
    return items or None


# ── Closed-set filters: typed by the enum (single source of truth) ──────────
JobStatusFilter = Annotated[
    JobStatus | None, BeforeValidator(empty_to_none), Query(description="Filter by job status")
]
JobTypeFilter = Annotated[
    JobType | None, BeforeValidator(empty_to_none), Query(description="Filter by job type")
]
CheckTypeFilter = Annotated[
    CheckType | None, BeforeValidator(empty_to_none), Query(description="Filter by check type")
]
HealthStatusFilter = Annotated[
    CheckHealthStatus | None,
    BeforeValidator(empty_to_none),
    Query(description="Filter by health (up/down/unknown)"),
]
NotifStatusFilter = Annotated[
    NotificationStatus | None,
    BeforeValidator(empty_to_none),
    Query(description="Filter by notification status"),
]
ProviderTypeFilter = Annotated[
    NotificationProviderType | None,
    BeforeValidator(empty_to_none),
    Query(description="Filter by provider type"),
]

# ── Reference filters: a specific instance id (UUID), not a closed set ──────
ProviderIdFilter = Annotated[
    UUID | None, BeforeValidator(empty_to_none), Query(description="Filter by provider id")
]
AlertIdFilter = Annotated[
    UUID | None, BeforeValidator(empty_to_none), Query(description="Filter by alert id")
]
AgentIdFilter = Annotated[
    UUID | None, BeforeValidator(empty_to_none), Query(description="Filter by agent id")
]
