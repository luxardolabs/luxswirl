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
from app.schemas.alert_trigger_config_schema import normalized_trigger_config
from app.schemas.paginated_response_schema import PaginatedResponse

# Fields every alert has: a change may set them, never clear them.
_REQUIRED = (
    "name",
    "trigger_type",
    "trigger_config",
    "is_enabled",
    "notify_on_recovery",
)

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

    @model_validator(mode="after")
    def _config_fits_trigger(self) -> Self:
        self.trigger_config = normalized_trigger_config(
            self.trigger_type, self.trigger_config
        )
        return self


class AdminAlertUpdate(AlertUpdate):
    """A change to an alert: the fields sent; a field left out is unchanged, and
    one sent as null is cleared (a required one cannot be).

    Whether an alert is global is fixed when it is created, as in the web UI:
    flipping it would strand or orphan its check mappings. A new trigger type
    comes with its config, which is validated against it.
    """

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _a_change_the_alert_can_hold(self) -> Self:
        sent = self.model_fields_set
        if "is_global" in sent:
            raise ValueError("Whether an alert is global is fixed when it is created")
        cleared = [f for f in _REQUIRED if f in sent and getattr(self, f) is None]
        if cleared:
            raise ValueError(f"These fields cannot be null: {', '.join(cleared)}")
        if "trigger_type" in sent and "trigger_config" not in sent:
            raise ValueError("A new trigger_type needs its trigger_config")
        if self.trigger_type is not None and self.trigger_config is not None:
            self.trigger_config = normalized_trigger_config(
                self.trigger_type, self.trigger_config
            )
        return self


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
