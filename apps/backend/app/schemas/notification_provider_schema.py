"""
Pydantic schemas for NotificationProvider domain.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field

from app.models.enum_model import NotificationProviderType
from app.schemas.base_schema import BaseSchema, TimestampSchema


class NotificationProviderBase(BaseSchema):
    """Base schema for NotificationProvider with common fields."""

    # NotificationProviderType IS the validation.
    provider_type: NotificationProviderType = Field(
        ...,
        description="Type of notification provider (email, webhook, homeassistant, etc.)",
    )
    friendly_name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="User-friendly name for this provider instance",
    )
    config: dict[str, Any] = Field(
        ...,
        description="Provider-specific configuration (validated by provider class)",
    )
    is_enabled: bool = Field(
        default=True,
        description="Whether this provider is enabled (can send notifications)",
    )
    is_default_enabled: bool = Field(
        default=False,
        description="Whether this provider is enabled by default for new checks",
    )
    rate_limit_count: int | None = Field(
        default=None,
        ge=1,
        description="Maximum notifications per rate_limit_window_minutes",
    )
    rate_limit_window_minutes: int | None = Field(
        default=60,
        ge=1,
        description="Time window for rate limiting in minutes",
    )


class NotificationProviderCreate(NotificationProviderBase):
    """Schema for creating a new notification provider."""

    model_config = {
        "json_schema_extra": {
            "example": {
                "provider_type": "email",
                "friendly_name": "Google SMTP",
                "config": {
                    "hostname": "smtp.gmail.com",
                    "port": 587,
                    "security": "starttls",
                    "username": "notifications@example.com",
                    "password": "app-specific-password",
                    "from_email": "notifications@example.com",
                    "to_email": "admin@example.com",
                },
                "is_default_enabled": True,
                "rate_limit_count": 100,
                "rate_limit_window_minutes": 60,
            }
        }
    }


class NotificationProviderUpdate(BaseSchema):
    """Schema for updating a notification provider."""

    friendly_name: str | None = Field(None, min_length=1, max_length=255)
    config: dict[str, Any] | None = None
    is_enabled: bool | None = None
    is_default_enabled: bool | None = None
    rate_limit_count: int | None = Field(None, ge=1)
    rate_limit_window_minutes: int | None = Field(None, ge=1)

    model_config = {
        "json_schema_extra": {
            "example": {
                "friendly_name": "Updated Gmail SMTP",
                "is_default_enabled": False,
                "rate_limit_count": 50,
            }
        }
    }


class NotificationProviderInDB(NotificationProviderBase, TimestampSchema):
    """Schema for notification provider in database."""

    id: UUID = Field(..., description="Database UUID")
    deleted_at: datetime | None = Field(None, description="Soft delete timestamp")


@dataclass(frozen=True)
class ProviderUrls:
    """Finished hrefs for one notification provider row (fw.url_assembly_in_view)."""

    toggle: str
    test: str
    edit_form: str
    delete: str


class NotificationProviderRow(NotificationProviderInDB):
    """A provider as the settings page lists it, with its links."""

    urls: ProviderUrls
