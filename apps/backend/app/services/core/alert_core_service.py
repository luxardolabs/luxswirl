"""
Alert service - business logic for alert configuration: alert CRUD, which checks
an alert watches, and snoozing an alert-check pair.

Evaluating check results against alerts and sending their notifications is
`alert_evaluation_core_service`.
"""

from collections.abc import Sequence
from datetime import timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.datetime_utils import utc_now
from app.core.exceptions import NotFoundException, ValidationException
from app.crud.alert_crud import AlertCRUD
from app.models.alert_model import Alert
from app.schemas.alert_schema import (
    AlertCheckSnooze,
    AlertCreate,
    AlertOption,
    AlertResponse,
    AlertUpdate,
)
from app.shared.logger import get_logger

logger = get_logger("luxswirl.services.alert")


class AlertCoreService:
    """Service for alert configuration operations."""

    @staticmethod
    async def get_alert_by_id(
        db: AsyncSession,
        alert_id: UUID,
        include_deleted: bool = False,
    ) -> Alert:
        """
        Get alert by ID.

        Args:
            db: Database session
            alert_id: Alert database ID
            include_deleted: Whether to include soft-deleted alerts

        Returns:
            Alert instance

        Raises:
            NotFoundException: If alert not found
        """
        alert = await AlertCRUD.get_by_id(db, alert_id, include_deleted=include_deleted)
        if not alert:
            raise NotFoundException(f"Alert {alert_id} not found")
        return alert

    @staticmethod
    async def list_alerts(
        db: AsyncSession,
        skip: int = 0,
        limit: int = 100,
        is_enabled: bool | None = None,
        is_global: bool | None = None,
        include_deleted: bool = False,
    ) -> tuple[Sequence[Alert], int]:
        """
        List alerts with pagination.

        Args:
            db: Database session
            skip: Number of records to skip
            limit: Maximum number of records to return
            is_enabled: Filter by enabled status
            is_global: Filter by global status
            include_deleted: Whether to include soft-deleted alerts

        Returns:
            Tuple of (alerts list, total count)
        """
        return await AlertCRUD.list_paginated(
            db,
            skip=skip,
            limit=limit,
            is_enabled=is_enabled,
            is_global=is_global,
            include_deleted=include_deleted,
        )

    @staticmethod
    def _to_response(alert: Alert) -> AlertResponse:
        """The API shape of an alert loaded with both of its mapping lists."""
        return AlertResponse(
            id=alert.id,
            name=alert.name,
            description=alert.description,
            trigger_type=alert.trigger_type,
            trigger_config=alert.trigger_config,
            is_enabled=alert.is_enabled,
            is_global=alert.is_global,
            notify_on_recovery=alert.notify_on_recovery,
            resend_interval_minutes=alert.resend_interval_minutes,
            max_resends=alert.max_resends,
            custom_subject=alert.custom_subject,
            custom_message=alert.custom_message,
            notification_provider_count=alert.notification_provider_count,
            check_count=alert.check_count,
            created_at=alert.created_at,
            updated_at=alert.updated_at,
            deleted_at=alert.deleted_at,
        )

    @staticmethod
    async def get_alert_response(db: AsyncSession, alert_id: UUID) -> AlertResponse:
        """One alert in its API shape, with its provider and check counts.

        Raises:
            NotFoundException: If alert not found
        """
        alert = await AlertCRUD.get_by_id(db, alert_id, with_mappings=True)
        if alert is None:
            raise NotFoundException(f"Alert {alert_id} not found")
        return AlertCoreService._to_response(alert)

    @staticmethod
    async def update_alert_response(
        db: AsyncSession, alert_id: UUID, data: AlertUpdate
    ) -> AlertResponse:
        """Update an alert and return it in its API shape.

        Raises:
            NotFoundException: If alert not found
            ValidationException: If the update is invalid
        """
        await AlertCoreService.update_alert(db, alert_id, data)
        return await AlertCoreService.get_alert_response(db, alert_id)

    @staticmethod
    async def list_alert_responses(
        db: AsyncSession,
        *,
        skip: int,
        limit: int,
        is_enabled: bool | None = None,
        is_global: bool | None = None,
    ) -> tuple[list[AlertResponse], int]:
        """A window of alerts in their API shape, and the total that match."""
        alerts, total = await AlertCoreService.list_alerts(
            db, skip=skip, limit=limit, is_enabled=is_enabled, is_global=is_global
        )
        return [AlertCoreService._to_response(a) for a in alerts], total

    @staticmethod
    async def create_alert_response(
        db: AsyncSession, data: AlertCreate
    ) -> AlertResponse:
        """Create an alert and return it in its API shape, counts included.

        Raises:
            ValidationException: If the alert is invalid
            DuplicateResourceException: If an alert with that name exists
        """
        alert = await AlertCoreService.create_alert(db, data)
        return await AlertCoreService.get_alert_response(db, alert.id)

    @staticmethod
    async def list_alert_options(db: AsyncSession) -> list[AlertOption]:
        """Every alert as a picker option."""
        alerts, _ = await AlertCoreService.list_alerts(db, limit=1000)
        return [AlertOption.model_validate(alert) for alert in alerts]

    @staticmethod
    async def count_alerts(db: AsyncSession) -> int:
        """How many alerts exist (soft-deleted excluded)."""
        _, total = await AlertCoreService.list_alerts(db, skip=0, limit=1)
        return total

    @staticmethod
    async def create_alert(
        db: AsyncSession,
        data: AlertCreate,
    ) -> Alert:
        """
        Create a new alert.

        Args:
            db: Database session
            data: Alert creation data

        Returns:
            Created alert
        """
        # Create alert
        alert = Alert(
            name=data.name,
            description=data.description,
            trigger_type=data.trigger_type,
            trigger_config=data.trigger_config,
            is_enabled=data.is_enabled,
            is_global=data.is_global,
            notify_on_recovery=data.notify_on_recovery,
            resend_interval_minutes=data.resend_interval_minutes,
            max_resends=data.max_resends,
            custom_subject=data.custom_subject,
            custom_message=data.custom_message,
        )

        # A global alert is mapped to checks as they are created, not here.
        alert = await AlertCRUD.add_alert(
            db,
            alert,
            provider_ids=data.notification_provider_ids,
            check_ids=[] if data.is_global else data.check_ids,
        )

        logger.info(
            "Created alert",
            extra={"alert_name": alert.name, "alert_id": str(alert.id)},
        )

        return alert

    @staticmethod
    async def update_alert(
        db: AsyncSession,
        alert_id: UUID,
        data: AlertUpdate,
    ) -> Alert:
        """
        Update an alert.

        Args:
            db: Database session
            alert_id: Alert ID
            data: Update data

        Returns:
            Updated alert

        Raises:
            NotFoundException: If alert not found
        """
        alert = await AlertCoreService.get_alert_by_id(db, alert_id)

        # Update fields
        if data.name is not None:
            alert.name = data.name
        if data.description is not None:
            alert.description = data.description
        if data.trigger_type is not None:
            alert.trigger_type = data.trigger_type
        if data.trigger_config is not None:
            alert.trigger_config = data.trigger_config
        if data.is_enabled is not None:
            alert.is_enabled = data.is_enabled
        if data.is_global is not None:
            alert.is_global = data.is_global
        if data.notify_on_recovery is not None:
            alert.notify_on_recovery = data.notify_on_recovery
        if data.resend_interval_minutes is not None:
            alert.resend_interval_minutes = data.resend_interval_minutes
        if data.max_resends is not None:
            alert.max_resends = data.max_resends
        if data.custom_subject is not None:
            alert.custom_subject = data.custom_subject
        if data.custom_message is not None:
            alert.custom_message = data.custom_message

        alert = await AlertCRUD.save_alert(db, alert)

        logger.info("Updated alert", extra={"alert_id": str(alert_id)})

        return alert

    @staticmethod
    async def delete_alert(
        db: AsyncSession,
        alert_id: UUID,
        hard_delete: bool = False,
    ) -> None:
        """
        Delete an alert (soft delete by default).

        Args:
            db: Database session
            alert_id: Alert ID
            hard_delete: If True, permanently delete; if False, soft delete

        Raises:
            NotFoundException: If alert not found
        """
        alert = await AlertCoreService.get_alert_by_id(db, alert_id)

        if hard_delete:
            await AlertCRUD.hard_delete_alert(db, alert)
        else:
            alert.deleted_at = utc_now()

        logger.info(
            "Deleted alert",
            extra={
                "alert_id": str(alert_id),
                "delete_kind": "hard" if hard_delete else "soft",
            },
        )

    @staticmethod
    async def add_check(
        db: AsyncSession,
        alert_id: UUID,
        check_id: UUID,
    ) -> UUID:
        """Add a check to an alert."""
        existing = await AlertCRUD.get_check_mapping(db, alert_id, check_id)

        if existing:
            raise ValidationException("Check already added to this alert")

        mapping = await AlertCRUD.add_check_mapping(db, alert_id, check_id)
        return mapping.id

    @staticmethod
    async def remove_check(
        db: AsyncSession,
        alert_id: UUID,
        check_id: UUID,
    ) -> None:
        """Remove a check from an alert."""
        await AlertCRUD.delete_check_mapping(db, alert_id, check_id)

    @staticmethod
    async def get_alert_ids_for_check(
        db: AsyncSession,
        check_id: UUID,
    ) -> set[UUID]:
        """
        Get all alert IDs assigned to a check.

        Args:
            db: Database session
            check_id: Check UUID

        Returns:
            Set of alert ID UUIDs
        """
        ids = await AlertCRUD.list_alert_ids_for_check(db, check_id)
        return set(ids)

    @staticmethod
    async def assign_global_alerts_to_check(
        db: AsyncSession,
        check_id: UUID,
    ) -> None:
        """
        Assign all global alerts to a check.
        Used when creating a new check.

        Args:
            db: Database session
            check_id: Check UUID
        """
        global_alert_ids = await AlertCRUD.list_global_enabled_alert_ids(db)

        # One read for the check's existing mappings rather than one per global
        # alert — this runs on every check create.
        already_mapped = await AlertCRUD.list_mapped_alert_ids_for_check(db, check_id)

        await AlertCRUD.add_check_mappings_for_alerts(
            db,
            check_id,
            [
                alert_id
                for alert_id in global_alert_ids
                if alert_id not in already_mapped
            ],
        )

        logger.info(
            "Assigned global alerts to check",
            extra={
                "check_id": str(check_id),
                # post-db-filter: log field: the ids were just used to create the mappings
                "global_alert_count": len(global_alert_ids),
            },
        )

    @staticmethod
    async def sync_check_alerts(
        db: AsyncSession,
        check_id: UUID,
        alert_ids: list[UUID],
    ) -> None:
        """
        Sync alert assignments for a check.

        Removes check from alerts not in the list and adds to new alerts.

        Args:
            db: Database session
            check_id: Check UUID
            alert_ids: List of alert IDs to assign
        """
        # Get current assignments
        current_alert_ids = await AlertCoreService.get_alert_ids_for_check(db, check_id)
        new_alert_ids = set(alert_ids)

        # NOT best-effort, and no longer swallowed. Both loops used to log a
        # warning and carry on, so the form returned success while the check was
        # wired to a different set of alerts than the operator had just chosen.
        # On a monitoring product that means an outage produces no notification
        # and nothing anywhere says so. A failure here propagates, the
        # transaction rolls back, and the caller is told. (LUXSWIRL-215)
        #
        # Both loops work on a set DIFFERENCE, so `add_check`'s "Check already
        # added to this alert" cannot fire — every id here is genuinely new or
        # genuinely being removed.

        # Remove from alerts that are no longer selected
        for alert_id in current_alert_ids - new_alert_ids:
            await AlertCoreService.remove_check(db, alert_id, check_id)

        # Add to newly selected alerts
        for alert_id in new_alert_ids - current_alert_ids:
            await AlertCoreService.add_check(db, alert_id, check_id)

    @staticmethod
    async def bulk_assign_to_checks(
        db: AsyncSession,
        alert_id: UUID,
        check_ids: list[UUID],
    ) -> int:
        """
        Assign one alert to many checks (preserving each check's existing alerts).

        Validates the alert exists, then for every check appends the alert to
        its existing assignment list and persists. Commits once at the end.

        Args:
            db: Database session
            alert_id: Alert UUID
            check_ids: Checks to assign the alert to

        Returns:
            Number of checks the alert was assigned to.

        Raises:
            ValueError: If the alert does not exist.
        """
        alert = await AlertCoreService.get_alert_by_id(db, alert_id)
        if not alert:
            raise ValueError(f"Alert not found: {alert_id}")

        # One query for the whole selection's current assignments; the writes
        # below still run per check, but the reads no longer scale with it.
        assigned = await AlertCRUD.list_alert_ids_for_checks(db, check_ids)
        for cid in check_ids:
            existing = list(assigned.get(cid, ()))
            if alert_id not in existing:
                existing.append(alert_id)
                await AlertCoreService.sync_check_alerts(db, cid, existing)
        logger.info(
            "Assigned alert to checks",
            extra={"alert_id": str(alert_id), "check_count": len(check_ids)},
        )
        return len(check_ids)

    @staticmethod
    async def bulk_clear_from_checks(
        db: AsyncSession,
        check_ids: list[UUID],
    ) -> int:
        """
        Clear all alerts from many checks. Commits once at the end.

        Args:
            db: Database session
            check_ids: Checks to clear alerts from

        Returns:
            Number of checks cleared.
        """
        for cid in check_ids:
            await AlertCoreService.sync_check_alerts(db, cid, [])
        logger.info(
            "Cleared alerts from checks",
            extra={"check_count": len(check_ids)},
        )
        return len(check_ids)

    @staticmethod
    async def snooze_alert_check(
        db: AsyncSession, alert_id: UUID, check_id: UUID, minutes: int = 15
    ) -> AlertCheckSnooze:
        """
        Snooze an alert-check relationship - temporarily pause notifications for this specific pairing.

        Monitoring continues, data is collected, but notifications are paused for THIS
        specific alert-check combination. Other alerts watching the same check are unaffected.

        Each call adds the specified minutes to snoozed_until.
        If already snoozed, extends the snooze period.

        Args:
            db: Database session
            alert_id: UUID of alert
            check_id: UUID of check
            minutes: Minutes to add to snooze time (default: 15)

        Returns:
            Updated AlertCheckMapping with snoozed_until set

        Raises:
            NotFoundException: If alert-check mapping not found
        """
        mapping = await AlertCRUD.get_check_mapping(db, alert_id, check_id)

        if not mapping:
            raise NotFoundException(
                f"Alert-check mapping not found for alert {alert_id} and check {check_id}"
            )

        # Calculate new snooze time
        now = utc_now()

        if mapping.snoozed_until and mapping.snoozed_until > now:
            # Already snoozed - extend from current snooze time
            new_snooze_time = mapping.snoozed_until + timedelta(minutes=minutes)
        else:
            # Not currently snoozed - start from now
            new_snooze_time = now + timedelta(minutes=minutes)

        # Update the mapping
        mapping.snoozed_until = new_snooze_time
        mapping = await AlertCRUD.save_check_mapping(db, mapping)

        logger.info(
            "Alert-check mapping snoozed",
            extra={
                "snoozed_until": str(mapping.snoozed_until),
                "alert_name": mapping.alert.name,
                "alert_id": str(mapping.alert.id),
                "check_name": mapping.check.display_name,
                "check_id": str(mapping.check.id),
            },
        )

        return AlertCheckSnooze(
            alert_id=mapping.alert.id,
            alert_name=mapping.alert.name,
            check_id=mapping.check.id,
            check_name=mapping.check.display_name,
            snoozed_until=new_snooze_time,
        )

    @staticmethod
    async def unsnooze_alert_check(
        db: AsyncSession, alert_id: UUID, check_id: UUID
    ) -> None:
        """
        Un-snooze an alert-check relationship - resume notifications immediately.

        Args:
            db: Database session
            alert_id: UUID of alert
            check_id: UUID of check

        Raises:
            NotFoundException: If alert-check mapping not found
        """
        mapping = await AlertCRUD.get_check_mapping(db, alert_id, check_id)

        if not mapping:
            raise NotFoundException(
                f"Alert-check mapping not found for alert {alert_id} and check {check_id}"
            )

        # Clear the snooze
        mapping.snoozed_until = None

        logger.info(
            "Alert-check mapping un-snoozed (notifications resumed)",
            extra={
                "alert_name": mapping.alert.name,
                "alert_id": str(mapping.alert.id),
                "check_name": mapping.check.display_name,
                "check_id": str(mapping.check.id),
            },
        )

        return None
