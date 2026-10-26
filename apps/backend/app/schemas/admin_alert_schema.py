"""
Pydantic schemas for alert rules on the admin API.

An alert is read with the providers it notifies and the checks it is mapped
to, by id; both sets are replaced whole. A field the API does not take is
refused (422), never silently dropped.
"""

from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.alert_schema import AlertCreate, AlertResponse, AlertUpdate
from app.schemas.paginated_response_schema import PaginatedResponse

# The most providers, and checks, one alert is given in one request.
MAX_PROVIDERS = 100
MAX_CHECKS = 10_000


class AdminAlertCreate(AlertCreate):
    """A new alert, with the providers it notifies and, unless global, its checks."""

    model_config = ConfigDict(extra="forbid")

    notification_provider_ids: list[UUID] = Field(
        default_factory=list, max_length=MAX_PROVIDERS
    )
    check_ids: list[UUID] = Field(default_factory=list, max_length=MAX_CHECKS)

    @model_validator(mode="after")
    def _global_takes_no_checks(self) -> Self:
        if self.is_global and self.check_ids:
            raise ValueError(
                "A global alert reaches checks by its filters; it takes no check_ids"
            )
        return self


class AdminAlertUpdate(AlertUpdate):
    """A change to an alert: the fields sent; a field left out is unchanged."""

    model_config = ConfigDict(extra="forbid")


class AdminAlertDetail(AlertResponse):
    """An alert, and the providers and checks it is mapped to."""

    notification_provider_ids: list[UUID] = Field(default_factory=list)
    check_ids: list[UUID] = Field(default_factory=list)


class AlertPage(PaginatedResponse[AlertResponse]):
    """One page of alerts."""


class AdminAlertProviders(BaseModel):
    """The whole set of providers an alert notifies; any other is detached."""

    model_config = ConfigDict(extra="forbid")

    notification_provider_ids: list[UUID] = Field(..., max_length=MAX_PROVIDERS)


class AdminAlertChecks(BaseModel):
    """The whole set of checks a non-global alert watches; any other is detached."""

    model_config = ConfigDict(extra="forbid")

    check_ids: list[UUID] = Field(..., max_length=MAX_CHECKS)


class AlertMappingChange(BaseModel):
    """How many mappings a replacement attached and detached."""

    added: int
    removed: int


class AdminAlertSnooze(BaseModel):
    """How long to hold back the alert's notifications for one check."""

    model_config = ConfigDict(extra="forbid")

    minutes: int = Field(15, ge=1, le=1440, description="Added to a running snooze")
