"""
Alert service - business logic for alert operations and evaluation.
"""

import asyncio
import fnmatch
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import cast
from uuid import UUID

from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.datetime_utils import utc_now
from app.core.exceptions import NotFoundException, ValidationException
from app.crud.alert_crud import AlertCRUD
from app.crud.check_result_crud import CheckResultCRUD
from app.models.alert_check_mapping_model import AlertCheckMapping
from app.models.alert_model import Alert
from app.models.alert_notification_mapping_model import AlertNotificationMapping
from app.models.check_model import Check
from app.models.check_result_model import CheckResult
from app.models.enum_model import NotificationStatus
from app.models.notification_log_model import NotificationLog
from app.notifications.providers.base import NotificationContext
from app.schemas.alert_schema import AlertCreate, AlertUpdate
from app.services.core.notification_core_service import NotificationCoreService

logger = get_logger("luxswirl.services.alert")


@dataclass(frozen=True, slots=True)
class SendDecision:
    """Result of `_should_send_notification`.

    `send` gates whether `_fire_alert` proceeds to call the provider; `is_resend`
    and `resend_count` are written through to `notification_logs.is_resend` and
    `notification_logs.resend_count` so the audit trail shows "this notification
    was a resend (Nth) of an ongoing incident" vs "first contact." LUXSWIRL-149.
    """

    send: bool
    is_resend: bool
    resend_count: int


class AlertCoreService:
    """Service for alert operations and evaluation."""

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

        db.add(alert)
        await db.flush()  # Get alert.id

        # Add notification provider mappings
        for provider_id in data.notification_provider_ids:
            mapping = AlertNotificationMapping(
                alert_id=alert.id,
                notification_provider_id=provider_id,
                is_enabled=True,
            )
            db.add(mapping)

        # Add check mappings (if not global)
        # Global alerts will be applied to NEW checks when they are created
        if not data.is_global:
            for check_id in data.check_ids:
                check_mapping = AlertCheckMapping(
                    alert_id=alert.id,
                    check_id=check_id,
                    is_enabled=True,
                )
                db.add(check_mapping)

        await db.flush()
        await db.refresh(alert)

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

        await db.flush()
        await db.refresh(alert)

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
            await db.delete(alert)
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
    async def add_notification_provider(
        db: AsyncSession,
        alert_id: UUID,
        provider_id: UUID,
    ) -> AlertNotificationMapping:
        """Add a notification provider to an alert."""
        existing = await AlertCRUD.get_notif_mapping(db, alert_id, provider_id)

        if existing:
            raise ValidationException("Provider already added to this alert")

        mapping = AlertNotificationMapping(
            alert_id=alert_id,
            notification_provider_id=provider_id,
            is_enabled=True,
        )

        db.add(mapping)
        await db.flush()
        await db.refresh(mapping)

        return mapping

    @staticmethod
    async def remove_notification_provider(
        db: AsyncSession,
        alert_id: UUID,
        provider_id: UUID,
    ) -> None:
        """Remove a notification provider from an alert."""
        await AlertCRUD.delete_notif_mapping(db, alert_id, provider_id)

    @staticmethod
    async def add_check(
        db: AsyncSession,
        alert_id: UUID,
        check_id: UUID,
    ) -> AlertCheckMapping:
        """Add a check to an alert."""
        existing = await AlertCRUD.get_check_mapping(db, alert_id, check_id)

        if existing:
            raise ValidationException("Check already added to this alert")

        mapping = AlertCheckMapping(
            alert_id=alert_id,
            check_id=check_id,
            is_enabled=True,
        )

        db.add(mapping)
        await db.flush()
        await db.refresh(mapping)

        return mapping

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

        for alert_id in global_alert_ids:
            existing = await AlertCRUD.get_check_mapping(db, alert_id, check_id)
            if not existing:
                mapping = AlertCheckMapping(
                    alert_id=alert_id,
                    check_id=check_id,
                    is_enabled=True,
                )
                db.add(mapping)

        logger.info(
            "Assigned global alerts to check",
            extra={
                "check_id": str(check_id),
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

        # Remove from alerts that are no longer selected
        alerts_to_remove = current_alert_ids - new_alert_ids
        for alert_id in alerts_to_remove:
            try:
                await AlertCoreService.remove_check(db, alert_id, check_id)
            except Exception:
                logger.warning(
                    "Failed to remove check from alert",
                    extra={"alert_id": str(alert_id)},
                    exc_info=True,
                )

        # Add to newly selected alerts
        alerts_to_add = new_alert_ids - current_alert_ids
        for alert_id in alerts_to_add:
            try:
                await AlertCoreService.add_check(db, alert_id, check_id)
            except Exception:
                logger.warning(
                    "Failed to add check to alert",
                    extra={"alert_id": str(alert_id)},
                    exc_info=True,
                )

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

        for cid in check_ids:
            existing = list(await AlertCoreService.get_alert_ids_for_check(db, cid))
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
    async def evaluate_check_result(
        db: AsyncSession,
        check_result: CheckResult,
    ) -> None:
        """
        Evaluate a check result against all applicable alerts.

        This is the main entry point for alert evaluation.

        Args:
            db: Database session
            check_result: Check result to evaluate
        """
        # Load the check with agent
        await db.refresh(check_result, ["check", "agent"])
        check = check_result.check

        # Find all applicable alerts (including global alerts via AlertCheckMapping)
        all_alerts = await AlertCRUD.list_active_alerts_for_check(db, check.id)

        logger.debug(
            "Evaluating alerts for check",
            extra={
                "alert_count": len(all_alerts),
                "check_name": check.display_name,
                "check_id": str(check.id),
                "status": check_result.status,
            },
        )

        # Evaluate each alert
        for alert in all_alerts:
            should_fire = await AlertCoreService._should_fire_alert(db, alert, check, check_result)

            if should_fire:
                await AlertCoreService._fire_alert(db, alert, check, check_result)

    @staticmethod
    async def _check_matches_filters(
        db: AsyncSession,
        check: Check,
        trigger_config: dict,
    ) -> bool:
        """
        Check if a check matches the filters in trigger_config.

        Args:
            db: Database session
            check: Check to evaluate
            trigger_config: Alert trigger configuration

        Returns:
            True if check matches filters
        """
        filters = trigger_config.get("check_filters", {})

        if not filters:
            return True  # No filters = matches all

        # Check agent_ids filter
        if "agent_ids" in filters:
            if check.agent_id not in filters["agent_ids"]:
                return False

        # Check check_types filter
        if "check_types" in filters:
            if check.check_type not in filters["check_types"]:
                return False

        # Check check_names filter (with wildcard support)
        if "check_names" in filters:
            matched = False
            for pattern in filters["check_names"]:
                if fnmatch.fnmatch(check.display_name, pattern):
                    matched = True
                    break
            if not matched:
                return False

        # Check tags filter
        if "tags" in filters and check.tags:
            required_tags = set(filters["tags"])
            check_tags = set(check.tags)
            if not required_tags.issubset(check_tags):
                return False

        return True

    @staticmethod
    def _compute_alert_state_key(
        alert: Alert,
        check_result: CheckResult,
        *,
        parent_down: bool = False,
    ) -> str:
        """Stable string identifying the alert's current state.

        Stored in `notification_logs.notification_hash` so `_should_send_notification`
        can detect state transitions (key changed → fire) vs steady-state (key same →
        dedup). See LUXSWIRL-147 for design; LUXSWIRL-146 for the SSL-band semantics
        this enables.

        Format: `<trigger_type>:<state>` for normal evaluations; `parent_down` when the
        leaf is being suppressed by a downed parent (LUXSWIRL-148).
        """
        if parent_down:
            return "parent_down"

        trigger_type = alert.trigger_type
        config = alert.trigger_config or {}

        if trigger_type == "status_change":
            return f"status:{'up' if check_result.success else 'down'}"

        if trigger_type == "threshold":
            metric = config.get("metric", "latency_ms")
            operator = config.get("operator", ">")
            value = config.get("value")
            return f"threshold:{metric}:{operator}:{value}"

        if trigger_type == "repeated_failure":
            return "repeated_failure:active"

        if trigger_type == "ssl_cert_expiry":
            thresholds = sorted(config.get("days_thresholds") or [config.get("days_threshold", 30)])
            metrics = check_result.get_metrics() or {}
            ssl_cert = metrics.get("response", {}).get("ssl_certificate") or {}
            days_until = ssl_cert.get("days_until_expiration")
            if days_until is None:
                return "ssl:unknown"
            for t in thresholds:
                if days_until <= t:
                    return f"ssl:lte:{t}"
            return "ssl:ok"

        return f"{trigger_type}:active"

    @staticmethod
    async def _should_fire_alert(
        db: AsyncSession,
        alert: Alert,
        check: Check,
        check_result: CheckResult,
    ) -> bool:
        """
        Determine if an alert should fire based on trigger conditions.

        Args:
            db: Database session
            alert: Alert to evaluate
            check: Check being evaluated
            check_result: Latest check result

        Returns:
            True if alert should fire
        """
        trigger_type = alert.trigger_type
        trigger_config = alert.trigger_config

        if trigger_type == "status_change":
            return await AlertCoreService._evaluate_status_change(
                db, alert, check, check_result, trigger_config
            )
        elif trigger_type == "threshold":
            return await AlertCoreService._evaluate_threshold(
                db, alert, check, check_result, trigger_config
            )
        elif trigger_type == "repeated_failure":
            return await AlertCoreService._evaluate_repeated_failure(
                db, alert, check, check_result, trigger_config
            )
        elif trigger_type == "ssl_cert_expiry":
            return await AlertCoreService._evaluate_ssl_cert_expiry(
                db, alert, check, check_result, trigger_config
            )
        else:
            logger.warning(
                "Unknown trigger type",
                extra={"trigger_type": trigger_type},
            )
            return False

    @staticmethod
    async def _evaluate_status_change(
        db: AsyncSession,
        alert: Alert,
        check: Check,
        check_result: CheckResult,
        config: dict,
    ) -> bool:
        """Evaluate status_change trigger."""
        on_status = config.get("on_status", ["error"])
        consecutive_failures = config.get("consecutive_failures", 1)

        current_status = "success" if check_result.success else "error"

        # Check for recovery (error -> success)
        if current_status == "success" and alert.notify_on_recovery:
            recent_results = await AlertCRUD.get_recent_results_for_check(db, check.id, 2)

            if len(recent_results) >= 2:
                previous_status = "success" if recent_results[1].success else "error"
                # If previous was error and current is success -> recovery
                if previous_status == "error":
                    return True

            # No previous error, no recovery notification
            return False

        # Check if current status matches trigger
        if current_status not in on_status:
            return False

        # If consecutive failures required, check previous results
        if consecutive_failures > 1:
            recent_results = await AlertCRUD.get_recent_results_for_check(
                db, check.id, consecutive_failures
            )

            if len(recent_results) < consecutive_failures:
                return False

            # Check if all are failures
            all_failures = all(not r.success for r in recent_results)
            return all_failures

        return True

    @staticmethod
    async def _evaluate_threshold(
        db: AsyncSession,
        alert: Alert,
        check: Check,
        check_result: CheckResult,
        config: dict,
    ) -> bool:
        """Evaluate threshold trigger (e.g., latency > 1000ms)."""
        metric = config.get("metric", "latency_ms")
        operator = config.get("operator", ">")
        threshold_value = config.get("value")

        if threshold_value is None:
            return False

        # Get metric value from result
        if metric == "latency_ms":
            current_value = check_result.latency_ms
        else:
            return False  # Unknown metric

        if current_value is None:
            return False

        # Evaluate operator
        threshold_float = cast(float, threshold_value)
        current_float = cast(float, current_value)
        if operator == ">":
            return bool(current_float > threshold_float)
        elif operator == ">=":
            return bool(current_float >= threshold_float)
        elif operator == "<":
            return bool(current_float < threshold_float)
        elif operator == "<=":
            return bool(current_float <= threshold_float)
        elif operator == "==":
            return bool(current_float == threshold_float)
        else:
            return False

    @staticmethod
    async def _evaluate_repeated_failure(
        db: AsyncSession,
        alert: Alert,
        check: Check,
        check_result: CheckResult,
        config: dict,
    ) -> bool:
        """Evaluate repeated_failure trigger."""
        # Similar to status_change with consecutive failures
        return await AlertCoreService._evaluate_status_change(
            db, alert, check, check_result, config
        )

    @staticmethod
    async def _evaluate_ssl_cert_expiry(
        db: AsyncSession,
        alert: Alert,
        check: Check,
        check_result: CheckResult,
        config: dict,
    ) -> bool:
        """Evaluate ssl_cert_expiry trigger.

        Fires while a cert is within `max(days_thresholds)` days of expiry. The
        escalation between bands (e.g. 30 → 14 → 7) is driven by the state key
        computed in `_compute_alert_state_key` and the dedup logic in
        `_should_send_notification` — this method just gates "are we in any band."

        Also fires once on recovery (cert renewed past all thresholds) when the
        most recent notification for this alert+check was in-band, so operators
        see "cert was at 5 days, renewed to 90 days" instead of silence. Gated by
        `alert.notify_on_recovery` (same flag status_change uses). LUXSWIRL-146.
        """
        # Support both single threshold (legacy) and array of thresholds
        days_thresholds = config.get("days_thresholds", [])
        if not days_thresholds:
            # Fallback to single threshold for backward compatibility
            days_thresholds = [config.get("days_threshold", 30)]

        # Get SSL cert info from check result metrics
        metrics = check_result.get_metrics()
        if not metrics:
            return False

        ssl_cert = metrics.get("response", {}).get("ssl_certificate")
        if not ssl_cert:
            # No SSL cert data - not applicable
            return False

        days_until_expiration = ssl_cert.get("days_until_expiration")
        if days_until_expiration is None:
            # Unable to determine expiration
            return False

        max_threshold = max(days_thresholds)
        in_danger_zone = bool(days_until_expiration <= max_threshold)

        if in_danger_zone:
            return True

        # Cert is currently safe. Fire once if the last notification we sent for
        # this alert+check was in-band (`ssl:lte:*`) — that's the renewal signal.
        # No DB hit if the alert doesn't want recovery notifications.
        if not alert.notify_on_recovery:
            return False

        last_notification = await AlertCRUD.get_last_notification_for_dedup(db, alert.id, check.id)
        if last_notification is None or last_notification.notification_hash is None:
            return False
        return last_notification.notification_hash.startswith("ssl:lte:")

    @staticmethod
    async def _fire_alert(
        db: AsyncSession,
        alert: Alert,
        check: Check,
        check_result: CheckResult,
    ) -> None:
        """
        Fire an alert - send notifications through all configured providers.

        Args:
            db: Database session
            alert: Alert to fire
            check: Check that triggered the alert
            check_result: Check result that triggered the alert
        """
        logger.info(
            "Firing alert",
            extra={
                "alert_name": alert.name,
                "alert_id": str(alert.id),
                "check_name": check.display_name,
                "check_id": str(check.id),
                "status": check_result.status,
            },
        )

        # Check if this is a recovery notification
        is_recovery = check_result.success and alert.notify_on_recovery

        # Recovery notifications bypass parent suppression — operator wants to
        # know a leaf came back even while the parent is still down.
        if not is_recovery:
            parent_suppressed = await AlertCoreService._handle_parent_suppression(
                db, alert, check, check_result
            )
            if parent_suppressed:
                return

        # De-duplication: Check if we should send this notification
        # Industry best practice - don't spam notifications for same status
        decision = await AlertCoreService._should_send_notification(
            db, alert, check, check_result, is_recovery
        )

        if not decision.send:
            logger.debug(
                "Skipping notification - de-duplicated (same status recently notified)",
                extra={"alert_name": alert.name, "alert_id": str(alert.id)},
            )
            return

        # Build notification context
        context = NotificationContext(
            check_name=check.display_name,
            check_type=check.check_type,
            target=check.target,
            agent_id=check_result.agent.id if check_result.agent else None,
            agent_name=(
                (check_result.agent.hostname or check_result.agent.agent_name)
                if check_result.agent
                else None
            ),
            status="success" if check_result.success else "error",
            success=check_result.success,
            latency_ms=check_result.latency_ms,
            timestamp=check_result.timestamp.isoformat(),
            error_message=check_result.error,
            error_type=check_result.error_type,
            http_status_code=check_result.http_status_code,
            alert_name=alert.name,
            alert_description=alert.description,
            is_recovery=is_recovery,
            custom_subject=alert.custom_subject,
            custom_message=alert.custom_message,
        )

        # Get active notification providers for this alert
        mappings = await AlertCRUD.list_active_notif_mappings_for_alert(db, alert.id)

        # State key recorded on every NotificationLog row so dedup can detect transitions
        # (LUXSWIRL-147). Computed once per fire — same value reaches every provider.
        state_key = AlertCoreService._compute_alert_state_key(alert, check_result)

        # Send notifications through each provider IN PARALLEL
        notification_tasks = []
        for mapping in mappings:
            provider = mapping.notification_provider
            if provider.deleted_at is not None:
                continue  # Skip soft-deleted providers

            # Create task but don't await yet - we'll run them all in parallel
            task = NotificationCoreService.send_notification(
                db=db,
                provider_id=provider.id,
                context=context,
                alert_id=alert.id,
                check_result_id=check_result.id,
                check_result_timestamp=check_result.timestamp,
                check_id=check.id,
                notification_hash=state_key,
                is_resend=decision.is_resend,
                resend_count=decision.resend_count,
            )
            notification_tasks.append((provider.id, task))

        # Send all notifications in parallel
        if notification_tasks:
            results = cast(
                list[NotificationLog | BaseException],
                await asyncio.gather(
                    *[task for _, task in notification_tasks], return_exceptions=True
                ),
            )

            # Log any failures
            for (provider_id, _), task_result in zip(notification_tasks, results, strict=False):
                if isinstance(task_result, BaseException):
                    logger.error(
                        "Failed to send notification via provider",
                        extra={
                            "provider_id": str(provider_id),
                            "task_result": str(task_result),
                        },
                    )

    @staticmethod
    async def _handle_parent_suppression(
        db: AsyncSession,
        alert: Alert,
        check: Check,
        check_result: CheckResult,
    ) -> bool:
        """Suppress when parent is currently down. Fail open if parent has no results yet."""
        if check.depends_on_check_id is None:
            return False

        parent_latest = await CheckResultCRUD.get_latest_result_for_check(
            db, check.depends_on_check_id
        )
        if parent_latest is None or parent_latest.success:
            return False

        parent_name = check.parent_check.display_name if check.parent_check else "parent"
        reason = f"Suppressed: parent check '{parent_name}' is down"
        # Distinct state key so dedup treats "parent down → parent up" as a transition
        # and the child's real alert state can fire when the parent recovers (LUXSWIRL-148).
        state_key = AlertCoreService._compute_alert_state_key(alert, check_result, parent_down=True)

        # LUXSWIRL-148: route the suppression-row write through the same dedup that
        # `_fire_alert` uses, so steady-state parent-down doesn't produce one row per
        # check execution. The first time the leaf enters parent-down we write a row
        # for the audit trail; subsequent ticks see the same hash and skip.
        should_log = await AlertCoreService._should_log_suppression(db, alert, check, state_key)

        if should_log:
            mappings = await AlertCRUD.list_active_notif_mappings_for_alert(db, alert.id)
            now = utc_now()
            for mapping in mappings:
                provider = mapping.notification_provider
                if provider.deleted_at is not None:
                    continue
                db.add(
                    NotificationLog(
                        alert_id=alert.id,
                        notification_provider_id=provider.id,
                        check_result_id=check_result.id,
                        check_result_timestamp=check_result.timestamp,
                        check_id=check.id,
                        check_success=check_result.success,
                        check_latency_ms=check_result.latency_ms,
                        status=NotificationStatus.SUPPRESSED.value,
                        error_message=reason,
                        notification_hash=state_key,
                        sent_at=now,
                    )
                )

            logger.info(
                "Notification suppressed - parent check is down (first occurrence)",
                extra={
                    "alert_name": alert.name,
                    "alert_id": str(alert.id),
                    "check_name": check.display_name,
                    "check_id": str(check.id),
                    "parent_check_id": str(check.depends_on_check_id),
                },
            )
        return True

    @staticmethod
    async def _should_log_suppression(
        db: AsyncSession,
        alert: Alert,
        check: Check,
        state_key: str,
    ) -> bool:
        """Whether to write a suppression row for this alert+check+state.

        Returns True only on transitions into the suppression state (or first contact).
        Uses the same dedup-relevant lookup as `_should_send_notification` so audit
        rows aren't duplicated tick-by-tick. See LUXSWIRL-148.
        """
        last_notification = await AlertCRUD.get_last_notification_for_dedup(db, alert.id, check.id)
        if last_notification is None:
            return True
        if last_notification.notification_hash is None:
            # Legacy row without a state key — write one row to seed the new dedup,
            # then steady-state takes over.
            return True
        return last_notification.notification_hash != state_key

    @staticmethod
    async def _should_send_notification(
        db: AsyncSession,
        alert: Alert,
        check: Check,
        check_result: CheckResult,
        is_recovery: bool,
    ) -> SendDecision:
        """Decide whether to send and, if so, classify the send as fresh vs resend.

        Returned `SendDecision` carries:
        - `send`: True to proceed, False to skip
        - `is_resend`: True only when this fire is a same-state resend after the
          configured `resend_interval_minutes`. False on first contact and state
          transitions. Written through to `notification_logs.is_resend`.
        - `resend_count`: 0 on fresh fires, `prior_count + 1` on resends. Written
          through to `notification_logs.resend_count`. If `alert.max_resends` is
          configured and the count would exceed it, returns `send=False`.

        De-duplication strategy:
        - Snoozed alert-check pair → skip
        - Hash key changed (or legacy `check_result.success` changed) → fresh send
        - Same state, no resend interval configured → skip
        - Same state, resend interval elapsed, under max_resends → resend
        """

        # Check if this alert-check relationship is snoozed
        mapping = await AlertCRUD.get_check_mapping(db, alert.id, check.id)

        if mapping and mapping.snoozed_until:
            now = utc_now()
            if mapping.snoozed_until > now:
                logger.debug(
                    "Alert-check relationship is snoozed - skipping notification",
                    extra={
                        "snoozed_until": str(mapping.snoozed_until),
                        "alert_name": alert.name,
                        "alert_id": str(alert.id),
                        "check_name": check.display_name,
                        "check_id": str(check.id),
                    },
                )
                return SendDecision(send=False, is_resend=False, resend_count=0)
            else:
                logger.debug(
                    "Alert-check relationship snooze expired - resuming notifications",
                    extra={
                        "alert_name": alert.name,
                        "alert_id": str(alert.id),
                        "check_name": check.display_name,
                        "check_id": str(check.id),
                    },
                )

        # Most recent dedup-relevant notification row (any status that represents
        # "we already audited this alert+check," not just successful delivery).
        # See LUXSWIRL-145 for why `sent`-only caused dedup amnesia.
        last_notification = await AlertCRUD.get_last_notification_for_dedup(db, alert.id, check.id)

        # No previous notification - always send (fresh state, not a resend)
        if not last_notification:
            logger.debug("No previous notification found - sending")
            return SendDecision(send=True, is_resend=False, resend_count=0)

        current_state_key = AlertCoreService._compute_alert_state_key(alert, check_result)

        if last_notification.notification_hash is not None:
            # Hash-based dedup (LUXSWIRL-147). The key encodes the alert's logical
            # state — for SSL it includes the threshold band, for status_change it's
            # up/down, for threshold it's the metric+operator+value tuple. A different
            # key means the alert moved to a new state and the operator should know.
            if last_notification.notification_hash != current_state_key:
                logger.debug(
                    "Alert state changed - sending notification",
                    extra={
                        "from_state": last_notification.notification_hash,
                        "to_state": current_state_key,
                    },
                )
                # State changed — not a resend, restart the resend counter
                return SendDecision(send=True, is_resend=False, resend_count=0)
        else:
            # Legacy fallback for rows written before LUXSWIRL-147 (no hash recorded):
            # compare check_result.success. Imprecise for metric-based triggers but
            # better than nothing while the backlog ages out under retention.
            last_check_result = await AlertCRUD.get_check_result_by_id(
                db, last_notification.check_result_id
            )
            if not last_check_result:
                logger.debug("Last notification's check result not found (retention?) - sending")
                return SendDecision(send=True, is_resend=False, resend_count=0)
            if last_check_result.success != check_result.success:
                logger.debug(
                    "Status changed (legacy dedup) - sending notification",
                    extra={
                        "from_status_success": last_check_result.success,
                        "to_status_success": check_result.success,
                    },
                )
                return SendDecision(send=True, is_resend=False, resend_count=0)

        # Same state - check resend interval
        if alert.resend_interval_minutes is None:
            # No resend configured - don't send duplicate
            logger.debug("Resend not configured - skipping duplicate notification")
            return SendDecision(send=False, is_resend=False, resend_count=0)

        # Check if enough time has passed
        time_since_last = utc_now() - last_notification.sent_at
        minutes_since_last = time_since_last.total_seconds() / 60

        if minutes_since_last >= alert.resend_interval_minutes:
            logger.debug(
                "Resend interval passed - sending notification",
                extra={
                    "minutes_since_last": round(minutes_since_last, 1),
                    "resend_interval_minutes": alert.resend_interval_minutes,
                },
            )
            # Same state, interval elapsed — this IS a resend (LUXSWIRL-149).
            # Increment from the prior row's count; if max_resends configured, gate it.
            prior_count = last_notification.resend_count or 0
            next_count = prior_count + 1
            if alert.max_resends is not None and next_count > alert.max_resends:
                logger.debug(
                    "Max resends reached - skipping notification",
                    extra={"max_resends": alert.max_resends, "prior_count": prior_count},
                )
                return SendDecision(send=False, is_resend=True, resend_count=prior_count)
            return SendDecision(send=True, is_resend=True, resend_count=next_count)

        logger.debug(
            "Within resend interval - skipping notification",
            extra={
                "minutes_since_last": round(minutes_since_last, 1),
                "resend_interval_minutes": alert.resend_interval_minutes,
            },
        )
        return SendDecision(send=False, is_resend=False, resend_count=0)

    @staticmethod
    async def snooze_alert_check(
        db, alert_id: UUID, check_id: UUID, minutes: int = 15
    ) -> AlertCheckMapping:
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
        await db.flush()
        await db.refresh(mapping)

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

        return mapping

    @staticmethod
    async def unsnooze_alert_check(db, alert_id: UUID, check_id: UUID) -> None:
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
