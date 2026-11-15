"""What the notification delivery worker carries between its steps: a claimed
notification row, the batch one claim took, and how an attempt ended.

Built by the server from its own rows and its own delivery attempts, never
from a request, so they are plain dataclasses rather than validated models.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.models.enum_model import NotificationProviderType, NotificationStatus
from app.notifications.providers.base import NotificationContext
from app.schemas.setting_schema import NetworkProtection


@dataclass(frozen=True)
class DeliveryOutcome:
    """How one delivery attempt ended: `sent` or `failed`, the error stored and
    shown with it (`NotificationCoreService._delivery_error`), and whether
    another attempt may succeed."""

    status: NotificationStatus
    error: str | None
    retryable: bool


@dataclass(frozen=True)
class ClaimedDelivery:
    """A notification row claimed for one attempt, with what the attempt needs,
    read while the claim's transaction was open."""

    log_id: UUID
    attempt: int
    provider_id: UUID
    provider_type: NotificationProviderType
    provider_config: dict[str, Any]
    context: NotificationContext


@dataclass(frozen=True)
class ClaimedBatch:
    """The rows one claim took for an attempt, how many more it ended without
    one (failed, suppressed or rate limited at the claim), and the SSRF
    settings the attempts run under."""

    deliveries: list[ClaimedDelivery]
    network: NetworkProtection
    ended: int = 0
