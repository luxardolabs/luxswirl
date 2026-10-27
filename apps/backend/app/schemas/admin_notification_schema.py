"""
Pydantic schemas for notification providers and notification logs on the
admin API.

A provider's config holds its credentials (an SMTP password, a webhook URL
whose path can itself be the credential, request headers), so a viewer's
token reads every provider with `config` null; an editor's reads it whole.
"""

from datetime import datetime
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enum_model import (
    CheckType,
    NotificationProviderType,
    NotificationStatus,
)
from app.schemas.notification_provider_schema import (
    NotificationProviderCreate,
    NotificationProviderUpdate,
)
from app.schemas.paginated_response_schema import PaginatedResponse


class AdminProvider(BaseModel):
    """A notification provider; `config` is null for a viewer."""

    id: UUID
    provider_type: NotificationProviderType
    friendly_name: str
    is_enabled: bool
    is_default_enabled: bool
    rate_limit_count: int | None = None
    rate_limit_window_minutes: int | None = None
    config: dict[str, Any] | None = Field(
        None, description="The provider's settings, credentials included; editors only"
    )
    created_at: datetime
    updated_at: datetime


class ProviderPage(PaginatedResponse[AdminProvider]):
    """One page of notification providers."""


class ProviderTypeInfo(BaseModel):
    """A provider type and the config fields it takes."""

    type: str
    name: str
    description: str
    config_schema: dict[str, Any]


class AdminProviderCreate(NotificationProviderCreate):
    """A new provider: its type, name and config, validated by the type."""

    model_config = ConfigDict(extra="forbid")


# What a change may clear with null: a provider's rate limit.
_CLEARABLE = frozenset({"rate_limit_count", "rate_limit_window_minutes"})


class AdminProviderUpdate(NotificationProviderUpdate):
    """A change to a provider: the fields sent; a config is replaced whole."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _none_cleared(self) -> Self:
        cleared = [
            f for f in self.model_fields_set - _CLEARABLE if getattr(self, f) is None
        ]
        if cleared:
            raise ValueError(
                f"These fields cannot be null: {', '.join(sorted(cleared))}"
            )
        return self


class ProviderTestResult(BaseModel):
    """Whether a test notification went through, and why not."""

    success: bool
    error: str | None = None


class AdminNotificationLog(BaseModel):
    """One notification attempt: which alert, provider and check, and its outcome."""

    id: UUID
    sent_at: datetime
    status: NotificationStatus
    error_message: str | None = Field(
        None, description="Why it was not sent; URLs cut to their host"
    )
    alert_id: UUID
    alert_name: str
    provider_name: str
    check_id: UUID | None = None
    check_name: str | None = None
    check_type: CheckType | None = None
    check_target: str | None = Field(None, description="Credentials masked")
    snoozed_until: datetime | None = None


class NotificationLogPage(PaginatedResponse[AdminNotificationLog]):
    """One page of notification attempts, newest first."""
