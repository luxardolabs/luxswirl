"""Notification log schemas."""

from datetime import datetime
from uuid import UUID

from app.models.enum_model import CheckType, NotificationStatus
from app.schemas.base_schema import BaseSchema


class NotificationLogEntry(BaseSchema):
    """One notification log with its alert, provider and check.

    The check fields are None when the log's check has since been deleted.
    """

    id: UUID
    sent_at: datetime
    alert_name: str
    alert_id: UUID
    snoozed_until: datetime | None
    provider_name: str
    display_name: str | None
    check_type: CheckType | None
    target: str | None
    status: NotificationStatus
    error_message: str | None
    success: bool | None
    latency_ms: float | None
    check_id: UUID | None
