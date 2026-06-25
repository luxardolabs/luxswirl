"""
Pydantic schemas for Alert domain.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field

from app.models.enum_model import AlertTriggerType
from app.schemas.base import BaseSchema, TimestampSchema


class AlertBase(BaseSchema):
    """Base schema for Alert with common fields."""

    name: str = Field(..., min_length=1, max_length=255, description="Name of the alert rule")
    description: str | None = Field(None, description="Detailed description")
    trigger_type: AlertTriggerType = Field(
        ...,
        description="Type of trigger (status_change, threshold, repeated_failure, ssl_cert_expiry)",
    )
    trigger_config: dict[str, Any] = Field(
        ...,
        description="Trigger-specific configuration",
    )
    is_enabled: bool = Field(default=True, description="Whether this alert is active")
    is_global: bool = Field(
        default=False,
        description="Whether this is a global alert (applies to filtered checks)",
    )
    notify_on_recovery: bool = Field(
        default=True,
        description="Send notification when service recovers",
    )
    resend_interval_minutes: int | None = Field(
        default=None,
        ge=1,
        description="Resend notification after X minutes if still down",
    )
    max_resends: int | None = Field(
        default=None,
        ge=1,
        description="Maximum number of resends",
    )
    custom_subject: str | None = Field(None, max_length=500, description="Custom subject template")
    custom_message: str | None = Field(None, description="Custom message body template")


class AlertCreate(AlertBase):
    """Schema for creating a new alert."""

    notification_provider_ids: list[UUID] = Field(
        default_factory=list,
        description="List of notification provider UUIDs to use for this alert",
    )
    check_ids: list[UUID] = Field(
        default_factory=list,
        description="List of check UUIDs to apply this alert to (if not global)",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "name": "Critical Services Down",
                "description": "Alert when critical production services fail",
                "trigger_type": "status_change",
                "trigger_config": {
                    "on_status": ["error"],
                    "consecutive_failures": 3,
                    "check_filters": {
                        "check_types": ["http"],
                        "tags": ["production", "critical"],
                    },
                },
                "is_enabled": True,
                "is_global": True,
                "notify_on_recovery": True,
                "resend_interval_minutes": 30,
                "max_resends": 5,
                "notification_provider_ids": [
                    "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                    "9c858901-8a57-4791-81fe-4c455b099bc9",
                ],
                "check_ids": [],
            }
        }
    }


class AlertUpdate(BaseSchema):
    """Schema for updating an alert."""

    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    trigger_type: AlertTriggerType | None = None
    trigger_config: dict[str, Any] | None = None
    is_enabled: bool | None = None
    is_global: bool | None = None
    notify_on_recovery: bool | None = None
    resend_interval_minutes: int | None = Field(None, ge=1)
    max_resends: int | None = Field(None, ge=1)
    custom_subject: str | None = None
    custom_message: str | None = None

    model_config = {
        "json_schema_extra": {
            "example": {
                "name": "Updated Alert Name",
                "is_enabled": False,
                "resend_interval_minutes": 60,
            }
        }
    }


class AlertInDB(AlertBase, TimestampSchema):
    """Schema for alert in database."""

    id: UUID = Field(..., description="Database UUID")
    deleted_at: datetime | None = Field(None, description="Soft delete timestamp")


class AlertResponse(AlertInDB):
    """Schema for alert API responses."""

    notification_provider_count: int | None = Field(
        default=0,
        description="Number of notification providers attached",
    )
    check_count: int | None = Field(
        default=0,
        description="Number of checks this alert applies to",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": 1,
                "name": "Critical Services Down",
                "description": "Alert when critical production services fail",
                "trigger_type": "status_change",
                "trigger_config": {
                    "on_status": ["error"],
                    "consecutive_failures": 3,
                },
                "is_enabled": True,
                "is_global": True,
                "notify_on_recovery": True,
                "resend_interval_minutes": 30,
                "max_resends": 5,
                "custom_subject": None,
                "custom_message": None,
                "notification_provider_count": 2,
                "check_count": 15,
                "created_at": "2024-10-22T10:00:00Z",
                "updated_at": "2024-10-22T10:00:00Z",
                "deleted_at": None,
            }
        }
    }


class AlertListResponse(BaseSchema):
    """Schema for paginated list of alerts."""

    items: list[AlertResponse]
    total: int
    page: int
    page_size: int
    pages: int


class AlertNotificationMappingCreate(BaseSchema):
    """Schema for adding a notification provider to an alert."""

    notification_provider_id: UUID = Field(..., description="Notification provider UUID")
    is_enabled: bool = Field(default=True, description="Whether this mapping is enabled")


class AlertNotificationMappingUpdate(BaseSchema):
    """Schema for updating an alert-notification mapping."""

    is_enabled: bool


class AlertCheckMappingCreate(BaseSchema):
    """Schema for adding a check to an alert."""

    check_id: UUID = Field(..., description="Check UUID")
    is_enabled: bool = Field(default=True, description="Whether this mapping is enabled")
    override_resend_interval: int | None = Field(
        None,
        ge=1,
        description="Override resend interval for this specific check",
    )
    override_max_resends: int | None = Field(
        None,
        ge=1,
        description="Override max resends for this specific check",
    )


class AlertCheckMappingUpdate(BaseSchema):
    """Schema for updating an alert-check mapping."""

    is_enabled: bool | None = None
    override_resend_interval: int | None = Field(None, ge=1)
    override_max_resends: int | None = Field(None, ge=1)


class AlertStatsResponse(BaseSchema):
    """Schema for alert statistics."""

    total_alerts: int
    enabled_alerts: int
    global_alerts: int
    total_notifications_sent: int
    notifications_last_24h: int
    failed_notifications_last_24h: int

    model_config = {
        "json_schema_extra": {
            "example": {
                "total_alerts": 10,
                "enabled_alerts": 8,
                "global_alerts": 3,
                "total_notifications_sent": 1234,
                "notifications_last_24h": 45,
                "failed_notifications_last_24h": 2,
            }
        }
    }
