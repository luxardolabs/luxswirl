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

from datetime import datetime
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
    SettingCategory,
    UserRole,
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
    JobStatus | None,
    BeforeValidator(empty_to_none),
    Query(description="Filter by job status"),
]
JobTypeFilter = Annotated[
    JobType | None,
    BeforeValidator(empty_to_none),
    Query(description="Filter by job type"),
]
SettingCategoryFilter = Annotated[
    SettingCategory | None,
    BeforeValidator(empty_to_none),
    Query(description="Filter by setting category"),
]
CheckTypeFilter = Annotated[
    CheckType | None,
    BeforeValidator(empty_to_none),
    Query(description="Filter by check type"),
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
    UUID | None,
    BeforeValidator(empty_to_none),
    Query(description="Filter by provider id"),
]
AlertIdFilter = Annotated[
    UUID | None, BeforeValidator(empty_to_none), Query(description="Filter by alert id")
]
AgentIdFilter = Annotated[
    UUID | None, BeforeValidator(empty_to_none), Query(description="Filter by agent id")
]

# ── Scalar filters: optional non-str params a dropdown/form can submit as '' ──
# The same empty→None normalization the enum filters get — an optional int/bool
# query param 422s on the "All" option's '' without it (fw.optional_query_empty_safe).
PerPageFilter = Annotated[
    int | None,
    BeforeValidator(empty_to_none),
    Query(ge=10, le=200, description="Items per page (defaults to setting)"),
]
HistoryHoursFilter = Annotated[
    int | None,
    BeforeValidator(empty_to_none),
    Query(ge=1, le=168, description="Hours of history to show (defaults to setting)"),
]
TimeRangeFilter = Annotated[
    int | None,
    BeforeValidator(empty_to_none),
    Query(description="Status bar time range in minutes"),
]
EnabledFilter = Annotated[
    bool | None,
    BeforeValidator(empty_to_none),
    Query(description="Filter by enabled status"),
]
GlobalFilter = Annotated[
    bool | None,
    BeforeValidator(empty_to_none),
    Query(description="Filter by global status"),
]
PublicFilter = Annotated[
    bool | None,
    BeforeValidator(empty_to_none),
    Query(description="Filter by public status"),
]
UserRoleFilter = Annotated[
    UserRole | None,
    BeforeValidator(empty_to_none),
    Query(description="Filter by role"),
]
ActiveFilter = Annotated[
    bool | None,
    BeforeValidator(empty_to_none),
    Query(description="Filter by active status"),
]
SinceFilter = Annotated[
    datetime | None,
    BeforeValidator(empty_to_none),
    Query(description="From this time (ISO 8601; no zone is UTC)"),
]
UntilFilter = Annotated[
    datetime | None,
    BeforeValidator(empty_to_none),
    Query(description="Up to this time (ISO 8601; no zone is UTC)"),
]
