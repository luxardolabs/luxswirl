"""
Alert evaluation - decides, per incoming check result, whether an alert fires
and whether its notification is sent, resent, de-duplicated or suppressed.

The configuration side of alerts (CRUD, check assignment, snoozing) lives in
`alert_core_service`. This is the engine the result-ingestion pipeline drives:
it reads the configured alerts, writes the notification audit trail
(`notification_logs`) and hands sends to the notification providers.
"""

import fnmatch
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.credential_mask import mask_credentials
from app.core.datetime_utils import utc_now
from app.crud.alert_crud import AlertCRUD
from app.crud.check_result_crud import CheckResultCRUD
from app.models.alert_model import Alert
from app.models.check_model import Check
from app.models.check_result_model import CheckResult
from app.models.enum_model import AlertTriggerType, NotificationStatus
from app.models.notification_log_model import NotificationLog
from app.notifications.providers.base import NotificationContext
from app.schemas.alert_schema import SendDecision
from app.services.core.notification_core_service import NotificationCoreService
from app.shared.logger import get_logger

logger = get_logger("luxswirl.services.alert")

SILENCE_TRIGGER_TYPES = frozenset(
    {AlertTriggerType.NO_DATA, AlertTriggerType.AGENT_OFFLINE}
)


class AlertEvaluationCoreService:
    """Evaluates check results against alerts and sends their notifications."""

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
        await AlertCRUD.load_result_check_and_agent(db, check_result)
        check = check_result.check

        # Find all applicable alerts (including global alerts via AlertCheckMapping)
        all_alerts = await AlertCRUD.list_active_alerts_for_check(db, check.id)

        logger.debug(
            "Evaluating alerts for check",
            extra={
                # post-db-filter: log field: every alert listed is evaluated below
                "alert_count": len(all_alerts),
                "check_name": check.display_name,
                "check_id": str(check.id),
                "status": check_result.status,
            },
        )

        # Evaluate each alert. The silence triggers fire on the absence of
        # results, so a result is not theirs to evaluate: the scheduled
        # SilenceAlertCoreService pass owns them, recoveries included.
        # Each alert in its own savepoint: one that fails (a stored trigger_config
        # of the wrong type, say) is logged and rolled back, and the next alert
        # on the check is still evaluated.
        for alert in all_alerts:
            if alert.trigger_type in SILENCE_TRIGGER_TYPES:
                continue
            # swallowed-exceptions: one alert's failure is logged with its id and rolled back in its savepoint; the other alerts on the check must still fire
            try:
                async with db.begin_nested():
                    should_fire = await AlertEvaluationCoreService._should_fire_alert(
                        db, alert, check, check_result
                    )
                    if should_fire:
                        await AlertEvaluationCoreService._fire_alert(
                            db, alert, check, check_result
                        )
            except Exception:
                logger.exception(
                    "Alert evaluation failed",
                    extra={
                        "alert_id": str(alert.id),
                        "alert_name": alert.name,
                        "check_id": str(check.id),
                    },
                )

    @staticmethod
    async def _check_matches_filters(
        db: AsyncSession,
        check: Check,
        trigger_config: dict[str, Any],
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
        if "agent_ids" in filters and check.agent_id not in filters["agent_ids"]:
            return False

        # Check check_types filter
        if "check_types" in filters and check.check_type not in filters["check_types"]:
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
        dedup). For SSL the key carries the threshold band, so each band crossed
        is its own transition.

        Format: `<trigger_type>:<state>` for normal evaluations; `parent_down` when the
        leaf is being suppressed by a downed parent.
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
            thresholds = sorted(
                config.get("days_thresholds") or [config.get("days_threshold", 30)]
            )
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
            return await AlertEvaluationCoreService._evaluate_status_change(
                db, alert, check, check_result, trigger_config
            )
        elif trigger_type == "threshold":
            return await AlertEvaluationCoreService._evaluate_threshold(
                db, alert, check, check_result, trigger_config
            )
        elif trigger_type == "repeated_failure":
            return await AlertEvaluationCoreService._evaluate_repeated_failure(
                db, alert, check, check_result, trigger_config
            )
        elif trigger_type == "ssl_cert_expiry":
            return await AlertEvaluationCoreService._evaluate_ssl_cert_expiry(
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
        config: dict[str, Any],
    ) -> bool:
        """Evaluate status_change trigger."""
        on_status = config.get("on_status", ["error"])
        consecutive_failures = config.get("consecutive_failures", 1)

        current_status = "success" if check_result.success else "error"

        # Check for recovery (error -> success)
        if current_status == "success" and alert.notify_on_recovery:
            recent_results = await AlertCRUD.get_recent_results_for_check(
                db, check.id, 2
            )

            # post-db-filter: the read is LIMIT 2; this asks whether both rows exist
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

            # post-db-filter: the read is LIMIT consecutive_failures; this asks whether that many exist
            if len(recent_results) < consecutive_failures:
                return False

            # Check if all are failures
            all_failures = all(not r.success for r in recent_results)
            return all_failures

        return True

    @staticmethod
    # dropped-input: one of the _evaluate_* family, dispatched by trigger type on a
    # shared signature; a threshold reads only the result and its own config.
    async def _evaluate_threshold(
        db: AsyncSession,
        alert: Alert,
        check: Check,
        check_result: CheckResult,
        config: dict[str, Any],
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

        # trigger_config is free-form JSON, so the threshold may be a number or
        # a numeric string; anything else cannot be compared and never fires.
        if not isinstance(threshold_value, int | float | str):
            return False
        try:
            threshold_float = float(threshold_value)
        except ValueError:
            logger.warning(
                "Threshold trigger value is not a number",
                extra={"alert_id": str(alert.id), "value": threshold_value},
            )
            return False
        current_float = float(current_value)
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
        config: dict[str, Any],
    ) -> bool:
        """Evaluate repeated_failure trigger."""
        # Similar to status_change with consecutive failures
        return await AlertEvaluationCoreService._evaluate_status_change(
            db, alert, check, check_result, config
        )

    @staticmethod
    async def _evaluate_ssl_cert_expiry(
        db: AsyncSession,
        alert: Alert,
        check: Check,
        check_result: CheckResult,
        config: dict[str, Any],
    ) -> bool:
        """Evaluate ssl_cert_expiry trigger.

        Fires while a cert is within `max(days_thresholds)` days of expiry. The
        escalation between bands (e.g. 30 → 14 → 7) is driven by the state key
        computed in `_compute_alert_state_key` and the dedup logic in
        `_should_send_notification` — this method just gates "are we in any band."

        Also fires once on recovery (cert renewed past all thresholds) when the
        most recent notification for this alert+check was in-band, so operators
        see "cert was at 5 days, renewed to 90 days" instead of silence. Gated by
        `alert.notify_on_recovery` (same flag status_change uses).
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
            # A certificate whose expiry cannot be read is one this alert cannot
            # watch: say so (state `ssl:unknown`, once) rather than stay silent.
            return True

        max_threshold = max(days_thresholds)
        in_danger_zone = bool(days_until_expiration <= max_threshold)

        if in_danger_zone:
            return True

        # Cert is currently safe. Fire once if the last notification we sent for
        # this alert+check was in-band (`ssl:lte:*`) or about an unreadable
        # expiry (`ssl:unknown`): the renewal, or the expiry readable again.
        # No DB hit if the alert doesn't want recovery notifications.
        if not alert.notify_on_recovery:
            return False

        last_notification = await AlertCRUD.get_last_notification_for_dedup(
            db, alert.id, check.id
        )
        if last_notification is None or last_notification.notification_hash is None:
            return False
        return last_notification.notification_hash.startswith("ssl:lte:") or (
            last_notification.notification_hash == "ssl:unknown"
        )

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

        is_recovery = AlertEvaluationCoreService._is_recovery(alert, check_result)

        # Recovery notifications bypass parent suppression — operator wants to
        # know a leaf came back even while the parent is still down.
        if not is_recovery:
            parent_suppressed = (
                await AlertEvaluationCoreService._handle_parent_suppression(
                    db, alert, check, check_result
                )
            )
            if parent_suppressed:
                return

        # De-duplication: Check if we should send this notification
        # Industry best practice - don't spam notifications for same status
        decision = await AlertEvaluationCoreService._should_send_notification(
            db, alert, check, check_result
        )

        if not decision.send:
            logger.debug(
                "Skipping notification - de-duplicated (same status recently notified)",
                extra={"alert_name": alert.name, "alert_id": str(alert.id)},
            )
            return

        context = AlertEvaluationCoreService._build_notification_context(
            alert, check, check_result, is_recovery=is_recovery
        )
        # State key recorded on every NotificationLog row so dedup can detect
        # transitions. Computed once per fire.
        await AlertEvaluationCoreService.send_to_providers(
            db,
            alert.id,
            context,
            decision,
            state_key=AlertEvaluationCoreService._compute_alert_state_key(
                alert, check_result
            ),
            check_id=check.id,
            check_result_id=check_result.id,
            check_result_timestamp=check_result.timestamp,
        )

    @staticmethod
    def _is_recovery(alert: Alert, check_result: CheckResult) -> bool:
        """Whether this fire tells of a recovery.

        For an SSL expiry alert, only a certificate back out of every band
        (state `ssl:ok`): the check succeeds while a certificate expires, so
        its success says nothing. For the others, a successful check.
        """
        if not alert.notify_on_recovery:
            return False
        if alert.trigger_type == AlertTriggerType.SSL_CERT_EXPIRY:
            return (
                AlertEvaluationCoreService._compute_alert_state_key(alert, check_result)
                == "ssl:ok"
            )
        return bool(check_result.success)

    @staticmethod
    def _ssl_detail(check_result: CheckResult) -> str | None:
        """What an SSL expiry alert found, in one line, or None without a cert."""
        metrics = check_result.get_metrics() or {}
        cert = metrics.get("response", {}).get("ssl_certificate") or {}
        if not cert:
            return None
        days = cert.get("days_until_expiration")
        if days is None:
            return (
                "SSL certificate expiry could not be read "
                f"(reported as {cert.get('expiration_date')!r})"
            )
        if days < 0:
            return f"SSL certificate expired {-days} day{'s' if days != -1 else ''} ago"
        if days == 0:
            return "SSL certificate expires today"
        return f"SSL certificate expires in {days} day{'s' if days != 1 else ''}"

    @staticmethod
    def _build_notification_context(
        alert: Alert,
        check: Check,
        check_result: CheckResult,
        *,
        is_recovery: bool,
    ) -> NotificationContext:
        """The provider-neutral payload every notification for this fire carries."""
        return NotificationContext(
            check_name=check.display_name,
            check_type=check.check_type,
            target=mask_credentials(check.target),
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
            detail=(
                AlertEvaluationCoreService._ssl_detail(check_result)
                if alert.trigger_type == AlertTriggerType.SSL_CERT_EXPIRY
                else None
            ),
        )

    @staticmethod
    async def send_to_providers(
        db: AsyncSession,
        alert_id: UUID,
        context: NotificationContext,
        decision: SendDecision,
        *,
        state_key: str,
        check_id: UUID | None,
        agent_id: UUID | None = None,
        check_result_id: UUID | None = None,
        check_result_timestamp: datetime | None = None,
    ) -> int:
        """Send `context` through each of the alert's live providers.

        Every provider's NotificationLog row records `state_key`, the subject
        (`check_id`, or `agent_id` for an agent_offline alert) and the result
        that triggered it, when one did. A provider that fails is recorded as
        failed; it never stops the others.

        Returns:
            How many notification rows were written (0 when the alert has no
            live provider).
        """
        mappings = await AlertCRUD.list_active_notif_mappings_for_alert(db, alert_id)
        provider_ids = [
            mapping.notification_provider.id
            for mapping in mappings
            if mapping.notification_provider.deleted_at is None
        ]
        if not provider_ids:
            return 0
        rows = await NotificationCoreService.send_notifications(
            db,
            provider_ids,
            context,
            alert_id=alert_id,
            check_result_id=check_result_id,
            check_result_timestamp=check_result_timestamp,
            check_id=check_id,
            agent_id=agent_id,
            notification_hash=state_key,
            is_resend=decision.is_resend,
            resend_count=decision.resend_count,
        )
        return len(rows)

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

        parent_name = (
            check.parent_check.display_name if check.parent_check else "parent"
        )
        reason = f"Suppressed: parent check '{parent_name}' is down"
        # Distinct state key so dedup treats "parent down → parent up" as a transition
        # and the child's real alert state can fire when the parent recovers.
        state_key = AlertEvaluationCoreService._compute_alert_state_key(
            alert, check_result, parent_down=True
        )

        # Route the suppression-row write through the same dedup that
        # `_fire_alert` uses, so steady-state parent-down doesn't produce one row per
        # check execution. The first time the leaf enters parent-down we write a row
        # for the audit trail; subsequent ticks see the same hash and skip.
        should_log = await AlertEvaluationCoreService._should_log_suppression(
            db, alert, check, state_key
        )

        if should_log:
            mappings = await AlertCRUD.list_active_notif_mappings_for_alert(
                db, alert.id
            )
            now = utc_now()
            await AlertCRUD.add_notification_logs(
                db,
                [
                    NotificationLog(
                        alert_id=alert.id,
                        notification_provider_id=mapping.notification_provider.id,
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
                    for mapping in mappings
                    if mapping.notification_provider.deleted_at is None
                ],
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
        rows aren't duplicated tick-by-tick.
        """
        last_notification = await AlertCRUD.get_last_notification_for_dedup(
            db, alert.id, check.id
        )
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
        if await AlertEvaluationCoreService._is_snoozed(db, alert, check):
            return SendDecision(send=False, is_resend=False, resend_count=0)

        # Most recent dedup-relevant notification row (any status that represents
        # "we already audited this alert+check," not just successful delivery):
        # counting only `sent` would forget the state each time delivery failed.
        last_notification = await AlertCRUD.get_last_notification_for_dedup(
            db, alert.id, check.id
        )

        # No previous notification - always send (fresh state, not a resend)
        if not last_notification:
            logger.debug("No previous notification found - sending")
            return SendDecision(send=True, is_resend=False, resend_count=0)

        if await AlertEvaluationCoreService._state_changed_since(
            db, alert, check_result, last_notification
        ):
            # State changed — not a resend, restart the resend counter
            return SendDecision(send=True, is_resend=False, resend_count=0)

        return AlertEvaluationCoreService.resend_decision(alert, last_notification)

    @staticmethod
    async def _is_snoozed(db: AsyncSession, alert: Alert, check: Check) -> bool:
        """Whether this alert-check pair is snoozed right now."""
        mapping = await AlertCRUD.get_check_mapping(db, alert.id, check.id)
        if not (mapping and mapping.snoozed_until):
            return False

        if mapping.snoozed_until > utc_now():
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
            return True

        logger.debug(
            "Alert-check relationship snooze expired - resuming notifications",
            extra={
                "alert_name": alert.name,
                "alert_id": str(alert.id),
                "check_name": check.display_name,
                "check_id": str(check.id),
            },
        )
        return False

    @staticmethod
    async def _state_changed_since(
        db: AsyncSession,
        alert: Alert,
        check_result: CheckResult,
        last_notification: NotificationLog,
    ) -> bool:
        """Whether the alert has left the state `last_notification` recorded."""
        if last_notification.notification_hash is not None:
            # Hash-based dedup. The key encodes the alert's logical
            # state — for SSL it includes the threshold band, for status_change it's
            # up/down, for threshold it's the metric+operator+value tuple. A different
            # key means the alert moved to a new state and the operator should know.
            current_state_key = AlertEvaluationCoreService._compute_alert_state_key(
                alert, check_result
            )
            if last_notification.notification_hash == current_state_key:
                return False
            logger.debug(
                "Alert state changed - sending notification",
                extra={
                    "from_state": last_notification.notification_hash,
                    "to_state": current_state_key,
                },
            )
            return True

        # A row with no hash recorded falls back to comparing
        # check_result.success: imprecise for metric-based triggers, and such
        # rows age out under retention.
        last_check_result = (
            await AlertCRUD.get_check_result_by_id(
                db, last_notification.check_result_id
            )
            if last_notification.check_result_id is not None
            else None
        )
        if not last_check_result:
            logger.debug(
                "Last notification's check result not found (retention?) - sending"
            )
            return True
        if last_check_result.success != check_result.success:
            logger.debug(
                "Status changed (legacy dedup) - sending notification",
                extra={
                    "from_status_success": last_check_result.success,
                    "to_status_success": check_result.success,
                },
            )
            return True
        return False

    @staticmethod
    def resend_decision(
        alert: Alert, last_notification: NotificationLog
    ) -> SendDecision:
        """Same state as last time: resend only once the alert's interval has
        elapsed, and never past `max_resends`."""
        if alert.resend_interval_minutes is None:
            # No resend configured - don't send duplicate
            logger.debug("Resend not configured - skipping duplicate notification")
            return SendDecision(send=False, is_resend=False, resend_count=0)

        # Check if enough time has passed
        time_since_last = utc_now() - last_notification.sent_at
        minutes_since_last = time_since_last.total_seconds() / 60

        if minutes_since_last < alert.resend_interval_minutes:
            logger.debug(
                "Within resend interval - skipping notification",
                extra={
                    "minutes_since_last": round(minutes_since_last, 1),
                    "resend_interval_minutes": alert.resend_interval_minutes,
                },
            )
            return SendDecision(send=False, is_resend=False, resend_count=0)

        logger.debug(
            "Resend interval passed - sending notification",
            extra={
                "minutes_since_last": round(minutes_since_last, 1),
                "resend_interval_minutes": alert.resend_interval_minutes,
            },
        )
        # Same state, interval elapsed — this IS a resend.
        # Increment from the prior row's count; if max_resends configured, gate it.
        prior_count = last_notification.resend_count or 0
        next_count = prior_count + 1
        if alert.max_resends is not None and next_count > alert.max_resends:
            logger.debug(
                "Max resends reached - skipping notification",
                extra={
                    "max_resends": alert.max_resends,
                    "prior_count": prior_count,
                },
            )
            return SendDecision(send=False, is_resend=True, resend_count=prior_count)
        return SendDecision(send=True, is_resend=True, resend_count=next_count)
