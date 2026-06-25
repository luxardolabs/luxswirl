"""
Alert CRUD - database queries for alerts, alert mappings, and notification logs.
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.alert_check_mapping_model import AlertCheckMapping
from app.models.alert_model import Alert
from app.models.alert_notification_mapping_model import AlertNotificationMapping
from app.models.check_result_model import CheckResult
from app.models.enum_model import NotificationStatus
from app.models.notification_log_model import NotificationLog

# Statuses that represent "alert evaluation already produced an audit row for this state."
# Used by `get_last_notification_for_dedup` to recover dedup memory across provider-disabled,
# rate-limited, suppressed, and failed-delivery conditions. `retrying` is excluded because it
# represents a pending decision, not a recorded state.
DEDUP_RELEVANT_STATUSES = (
    NotificationStatus.SENT.value,
    NotificationStatus.FAILED.value,
    NotificationStatus.RATE_LIMITED.value,
    NotificationStatus.SUPPRESSED.value,
    NotificationStatus.DEDUPLICATED.value,
)


class AlertCRUD:
    """Database queries for alerts and their relationships."""

    @staticmethod
    async def get_by_id(
        db: AsyncSession, alert_id: UUID, *, include_deleted: bool = False
    ) -> Alert | None:
        query = select(Alert).where(Alert.id == alert_id)
        if not include_deleted:
            query = query.where(Alert.deleted_at.is_(None))
        result = await db.execute(query)
        return result.scalar_one_or_none()

    @staticmethod
    async def count_enabled(db: AsyncSession) -> int:
        """Count alert rules where is_enabled is True (excluding soft-deleted)."""
        result = await db.execute(
            select(func.count(Alert.id)).where(
                Alert.is_enabled.is_(True),
                Alert.deleted_at.is_(None),
            )
        )
        return result.scalar() or 0

    @staticmethod
    async def list_paginated(
        db: AsyncSession,
        *,
        skip: int = 0,
        limit: int = 100,
        is_enabled: bool | None = None,
        is_global: bool | None = None,
        include_deleted: bool = False,
    ) -> tuple[Sequence[Alert], int]:
        """Paginated list of alerts with notification_mappings loaded."""
        query = select(Alert)
        if not include_deleted:
            query = query.where(Alert.deleted_at.is_(None))
        if is_enabled is not None:
            query = query.where(Alert.is_enabled == is_enabled)
        if is_global is not None:
            query = query.where(Alert.is_global == is_global)

        total = (await db.scalar(select(func.count()).select_from(query.subquery()))) or 0

        result = await db.execute(
            query.options(selectinload(Alert.notification_mappings))
            .order_by(Alert.id)
            .offset(skip)
            .limit(limit)
        )
        return result.scalars().all(), total

    @staticmethod
    async def get_notif_mapping(
        db: AsyncSession, alert_id: UUID, provider_id: UUID
    ) -> AlertNotificationMapping | None:
        result = await db.execute(
            select(AlertNotificationMapping).where(
                and_(
                    AlertNotificationMapping.alert_id == alert_id,
                    AlertNotificationMapping.notification_provider_id == provider_id,
                )
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def delete_notif_mapping(db: AsyncSession, alert_id: UUID, provider_id: UUID) -> int:
        result = await db.execute(
            delete(AlertNotificationMapping).where(
                and_(
                    AlertNotificationMapping.alert_id == alert_id,
                    AlertNotificationMapping.notification_provider_id == provider_id,
                )
            )
        )
        return result.rowcount or 0  # type: ignore[attr-defined]

    @staticmethod
    async def get_check_mapping(
        db: AsyncSession, alert_id: UUID, check_id: UUID
    ) -> AlertCheckMapping | None:
        result = await db.execute(
            select(AlertCheckMapping).where(
                and_(
                    AlertCheckMapping.alert_id == alert_id,
                    AlertCheckMapping.check_id == check_id,
                )
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def delete_check_mapping(db: AsyncSession, alert_id: UUID, check_id: UUID) -> int:
        result = await db.execute(
            delete(AlertCheckMapping).where(
                and_(
                    AlertCheckMapping.alert_id == alert_id,
                    AlertCheckMapping.check_id == check_id,
                )
            )
        )
        return result.rowcount or 0  # type: ignore[attr-defined]

    @staticmethod
    async def list_alert_ids_for_check(db: AsyncSession, check_id: UUID) -> list[UUID]:
        """All alert ids assigned to a check (regardless of enabled state)."""
        result = await db.execute(
            select(AlertCheckMapping.alert_id).where(AlertCheckMapping.check_id == check_id)
        )
        return [row[0] for row in result.fetchall()]

    @staticmethod
    async def list_global_enabled_alert_ids(db: AsyncSession) -> list[UUID]:
        """Ids of all enabled, non-deleted, global alerts."""
        result = await db.execute(
            select(Alert.id).where(
                and_(
                    Alert.is_global.is_(True),
                    Alert.is_enabled.is_(True),
                    Alert.deleted_at.is_(None),
                )
            )
        )
        return [row[0] for row in result.fetchall()]

    @staticmethod
    async def list_active_alerts_for_check(db: AsyncSession, check_id: UUID) -> Sequence[Alert]:
        """Enabled, non-deleted alerts mapped to a check, with notification_mappings loaded."""
        result = await db.execute(
            select(Alert)
            .join(AlertCheckMapping)
            .where(
                and_(
                    AlertCheckMapping.check_id == check_id,
                    AlertCheckMapping.is_enabled.is_(True),
                    Alert.is_enabled.is_(True),
                    Alert.deleted_at.is_(None),
                )
            )
            .options(selectinload(Alert.notification_mappings))
        )
        return result.scalars().all()

    @staticmethod
    async def get_recent_results_for_check(
        db: AsyncSession, check_id: UUID, limit: int
    ) -> Sequence[CheckResult]:
        """Most recent N results for a check, newest first."""
        result = await db.execute(
            select(CheckResult)
            .where(CheckResult.check_id == check_id)
            .order_by(CheckResult.timestamp.desc())
            .limit(limit)
        )
        return result.scalars().all()

    @staticmethod
    async def list_active_notif_mappings_for_alert(
        db: AsyncSession, alert_id: UUID
    ) -> Sequence[AlertNotificationMapping]:
        """Enabled notification mappings for an alert, with provider loaded."""
        result = await db.execute(
            select(AlertNotificationMapping)
            .where(
                and_(
                    AlertNotificationMapping.alert_id == alert_id,
                    AlertNotificationMapping.is_enabled.is_(True),
                )
            )
            .options(selectinload(AlertNotificationMapping.notification_provider))
        )
        return result.scalars().all()

    @staticmethod
    async def get_last_notification_for_dedup(
        db: AsyncSession, alert_id: UUID, check_id: UUID
    ) -> NotificationLog | None:
        """Most recent dedup-relevant notification row for an alert+check pairing.

        Used by `_should_send_notification` to decide whether the current alert
        evaluation represents a new state. Includes `sent`, `failed`, `rate_limited`,
        `suppressed`, and `deduplicated` — anything that proves we already audited
        this alert+check. Filtering to `sent`-only here caused the dedup memory to
        reset every time the provider was disabled / rate-limited / failed, producing
        one notification row per check execution. See LUXSWIRL-145.

        Uses the denormalized `notification_logs.check_id` column so the lookup is a
        single-table index scan — earlier versions joined through `check_results`,
        which on large datasets pushed sort buffers past PostgreSQL's shared memory.
        """
        result = await db.execute(
            select(NotificationLog)
            .where(
                and_(
                    NotificationLog.alert_id == alert_id,
                    NotificationLog.check_id == check_id,
                    NotificationLog.status.in_(DEDUP_RELEVANT_STATUSES),
                )
            )
            .order_by(NotificationLog.sent_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_check_result_by_id(db: AsyncSession, check_result_id: UUID) -> CheckResult | None:
        result = await db.execute(
            select(CheckResult).where(CheckResult.id == check_result_id).limit(1)
        )
        return result.scalar_one_or_none()
