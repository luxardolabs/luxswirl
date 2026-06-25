"""
Pydantic schemas for NotificationProvider domain.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field, field_validator

from app.models.enum_model import NotificationProviderType
from app.schemas.base import BaseSchema, TimestampSchema


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


class NotificationProviderResponse(NotificationProviderInDB):
    """Schema for notification provider API responses."""

    # Mask sensitive fields in config (password, api_key, etc.)
    @field_validator("config", mode="before")
    @classmethod
    def mask_sensitive_fields(cls, v: dict[str, Any]) -> dict[str, Any]:
        """Mask sensitive configuration fields."""
        if not isinstance(v, dict):
            return v

        masked = v.copy()
        sensitive_fields = ["password", "api_key", "token", "secret", "api_token"]

        for field in sensitive_fields:
            if field in masked and masked[field]:
                masked[field] = "***MASKED***"

        # Webhook providers carry secrets in additional_headers (e.g. an Authorization
        # bearer). Mask the header VALUES (header names stay visible) so they are never
        # echoed back in plaintext on a read.
        headers = masked.get("additional_headers")
        if isinstance(headers, dict) and headers:
            masked["additional_headers"] = dict.fromkeys(headers, "***MASKED***")

        return masked

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": 1,
                "provider_type": "email",
                "friendly_name": "Google SMTP",
                "config": {
                    "hostname": "smtp.gmail.com",
                    "port": 587,
                    "security": "starttls",
                    "username": "notifications@example.com",
                    "password": "***MASKED***",
                    "from_email": "notifications@example.com",
                    "to_email": "admin@example.com",
                },
                "is_default_enabled": True,
                "rate_limit_count": 100,
                "rate_limit_window_minutes": 60,
                "created_at": "2024-10-22T10:00:00Z",
                "updated_at": "2024-10-22T10:00:00Z",
                "deleted_at": None,
            }
        }
    }


class NotificationProviderListResponse(BaseSchema):
    """Schema for paginated list of notification providers."""

    items: list[NotificationProviderResponse]
    total: int
    page: int
    page_size: int
    pages: int


class NotificationProviderTestRequest(BaseSchema):
    """Schema for testing a notification provider."""

    test_message: str | None = Field(
        default="This is a test notification from LuxSwirl",
        description="Custom test message",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "test_message": "Testing email notifications",
            }
        }
    }


class NotificationProviderTestResponse(BaseSchema):
    """Schema for test notification response."""

    success: bool
    message: str
    error: str | None = None

    model_config = {
        "json_schema_extra": {
            "example": {
                "success": True,
                "message": "Test notification sent successfully",
                "error": None,
            }
        }
    }


class NotificationProviderSchemaResponse(BaseSchema):
    """Schema for provider configuration schema."""

    provider_type: str
    provider_name: str
    provider_description: str
    config_schema: dict[str, Any]

    model_config = {
        "json_schema_extra": {
            "example": {
                "provider_type": "email",
                "provider_name": "Email",
                "provider_description": "Send notifications via email using SMTP",
                "config_schema": {
                    "hostname": {
                        "type": "string",
                        "label": "Hostname",
                        "required": True,
                    }
                },
            }
        }
    }


class NotificationProviderTypesResponse(BaseSchema):
    """Schema for list of available provider types."""

    providers: list[NotificationProviderSchemaResponse]

    model_config = {
        "json_schema_extra": {
            "example": {
                "providers": [
                    {
                        "provider_type": "email",
                        "provider_name": "Email",
                        "provider_description": "Send notifications via email",
                        "config_schema": {},
                    }
                ]
            }
        }
    }
