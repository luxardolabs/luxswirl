"""
Silence alerts - the alerts that fire on the ABSENCE of data.

`no_data` fires for a check that is expected to run (an active agent is
assigned it) but whose newest result is older than `missed_intervals` of its
interval (see `app.core.liveness`). `agent_offline` fires for an active agent
whose newest heartbeat is that late. No incoming result can drive either, so
the scheduler runs `evaluate_silence_alerts` every minute.

Each silence is an episode. It notifies once, then follows the alert's resend
settings. It ends when data arrives again: a recovery notification goes out if
the alert asks for one and the silence was notified (not suppressed). The next
silence is a new episode.

A silent check whose agents are all offline is the agent's silence, not the
check's. Its row is written SUPPRESSED, so a dead agent with a hundred checks
sends one agent_offline notification, not a hundred no_data ones.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import UUID

from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.liveness import DEFAULT_MISSED_INTERVALS, silent_after
from app.crud.alert_crud import AlertCRUD
from app.crud.check_result_crud import CheckResultCRUD
from app.db import worker_session
from app.models.agent_model import Agent
from app.models.alert_model import Alert
from app.models.check_model import Check
from app.models.enum_model import AlertTriggerType, NotificationStatus
from app.models.notification_log_model import NotificationLog
from app.notifications.providers.base import NotificationContext
from app.schemas.alert_schema import SendDecision, SilencePassCounts
from app.services.core.agent_assignment_core_service import (
    AgentAssignmentCoreService,
)
from app.services.core.alert_evaluation_core_service import AlertEvaluationCoreService

logger = get_logger("luxswirl.services.silence_alert")

# The state keys stored in notification_logs.notification_hash.
CHECK_SILENT = "no_data:silent"
CHECK_REPORTING = "no_data:reporting"
AGENT_OFFLINE = "agent_offline:offline"
AGENT_ONLINE = "agent_offline:online"

_FRESH = SendDecision(send=True, is_resend=False, resend_count=0)


def _missed_intervals(alert: Alert) -> int:
    """The alert's `missed_intervals`, or the default when unset or unusable."""
    value = (alert.trigger_config or {}).get("missed_intervals")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return DEFAULT_MISSED_INTERVALS


def _heartbeat_interval(agent: Agent) -> int:
    return agent.heartbeat_interval or settings.server.default_heartbeat_interval


def _agent_offline(agent: Agent, now: datetime, missed: int) -> bool:
    last = agent.last_seen or agent.created_at
    return last < now - silent_after(_heartbeat_interval(agent), missed)


def _ago(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min"


def _open_episode(
    last: NotificationLog | None, state: str, since: datetime | None
) -> bool:
    """Whether `last` records a silence (`state`) that no data has ended yet."""
    return (
        last is not None
        and last.notification_hash == state
        and (since is None or since <= last.sent_at)
    )


class SilenceAlertCoreService:
    """Evaluates the no_data and agent_offline alerts."""

    @staticmethod
    async def evaluate(db: AsyncSession) -> SilencePassCounts:
        """One pass over every enabled silence alert."""
        counts = SilencePassCounts()
        alerts = await AlertCRUD.list_enabled_alerts_by_trigger(
            db, (AlertTriggerType.NO_DATA, AlertTriggerType.AGENT_OFFLINE)
        )
        if not alerts:
            return counts
        counts.alerts = len(alerts)

        now = utc_now()
        assigned = await AgentAssignmentCoreService.assigned_agents_by_check(db)
        for alert in alerts:
            if alert.trigger_type == AlertTriggerType.NO_DATA:
                await SilenceAlertCoreService._evaluate_no_data(
                    db, alert, assigned, now, counts
                )
            else:
                await SilenceAlertCoreService._evaluate_agent_offline(
                    db, alert, assigned, now, counts
                )
        return counts

    # ── no_data ──────────────────────────────────────────────────────────────

    @staticmethod
    async def _evaluate_no_data(
        db: AsyncSession,
        alert: Alert,
        assigned: Sequence[tuple[Check, list[Agent]]],
        now: datetime,
        counts: SilencePassCounts,
    ) -> None:
        mappings = {m.check_id: m for m in alert.check_mappings if m.is_enabled}
        # Only checks some active agent is to run: a disabled check, or one
        # whose agents are all paused, is not expected to report.
        targets = [(c, agents) for c, agents in assigned if c.id in mappings and agents]
        if not targets:
            return

        missed = _missed_intervals(alert)
        check_ids = [check.id for check, _ in targets]
        last_rows = await AlertCRUD.last_notifications_by_subject(
            db, alert.id, check_ids=check_ids
        )
        # Look back far enough to see every check's threshold, and to tell
        # whether a result arrived after each open episode's notification.
        since = min(now - silent_after(c.interval_seconds, missed) for c, _ in targets)
        for row in last_rows.values():
            if row.notification_hash == CHECK_SILENT:
                since = min(since, row.sent_at)
        latest = await CheckResultCRUD.latest_result_times(db, check_ids, since)

        for check, agents in targets:
            last_result = latest.get(check.id)
            last_row = last_rows.get(check.id)
            threshold = silent_after(check.interval_seconds, missed)
            if (last_result or check.created_at) >= now - threshold:
                await SilenceAlertCoreService._check_reporting(
                    db, alert, check, last_row, last_result, counts
                )
                continue

            counts.silent_checks += 1
            mapping = mappings[check.id]
            if mapping.snoozed_until and mapping.snoozed_until > now:
                continue

            in_episode = _open_episode(last_row, CHECK_SILENT, last_result)
            offline = [
                a for a in agents if _agent_offline(a, now, DEFAULT_MISSED_INTERVALS)
            ]
            if len(offline) == len(agents):
                if not (
                    in_episode
                    and last_row
                    and last_row.status == NotificationStatus.SUPPRESSED
                ):
                    await SilenceAlertCoreService._suppress_no_data(
                        db, alert, check, offline, now
                    )
                    counts.suppressed += 1
                continue

            if (
                in_episode
                and last_row
                and last_row.status != NotificationStatus.SUPPRESSED
            ):
                decision = AlertEvaluationCoreService.resend_decision(alert, last_row)
            else:
                decision = _FRESH
            if not decision.send:
                continue

            if last_result is None and not decision.is_resend:
                # Older than the look-back window: once per episode, find out
                # when it last reported, for the message.
                newest = await CheckResultCRUD.get_latest_result_for_check(db, check.id)
                last_result = newest.timestamp if newest else None
            age = now - (last_result or check.created_at)
            context = SilenceAlertCoreService._check_context(
                alert,
                check,
                agents,
                success=False,
                message=(
                    f"No results for {_ago(age)} (expected every "
                    f"{check.interval_seconds or 60} s)"
                    if last_result
                    else f"No results since it was created {_ago(age)} ago"
                ),
            )
            await AlertEvaluationCoreService.send_to_providers(
                db, alert, context, decision, state_key=CHECK_SILENT, check_id=check.id
            )
            counts.notified += 1
            logger.info(
                "Check silent",
                extra={
                    "alert_id": str(alert.id),
                    "check_id": str(check.id),
                    "check_name": check.display_name,
                    "last_result": last_result.isoformat() if last_result else None,
                    "is_resend": decision.is_resend,
                },
            )

    @staticmethod
    async def _check_reporting(
        db: AsyncSession,
        alert: Alert,
        check: Check,
        last_row: NotificationLog | None,
        last_result: datetime | None,
        counts: SilencePassCounts,
    ) -> None:
        """A reporting check: send the recovery that ends a notified silence."""
        if not (
            alert.notify_on_recovery
            and last_row is not None
            and last_row.notification_hash == CHECK_SILENT
            and last_row.status != NotificationStatus.SUPPRESSED
            and last_result is not None
            and last_result > last_row.sent_at
        ):
            return
        context = SilenceAlertCoreService._check_context(
            alert, check, [], success=True, message=None, is_recovery=True
        )
        await AlertEvaluationCoreService.send_to_providers(
            db, alert, context, _FRESH, state_key=CHECK_REPORTING, check_id=check.id
        )
        counts.recovered += 1

    @staticmethod
    async def _suppress_no_data(
        db: AsyncSession,
        alert: Alert,
        check: Check,
        offline: Sequence[Agent],
        now: datetime,
    ) -> None:
        """Record the silence as the offline agents', one row per provider."""
        names = ", ".join(a.agent_name or str(a.id) for a in offline)
        mappings = await AlertCRUD.list_active_notif_mappings_for_alert(db, alert.id)
        await AlertCRUD.add_notification_logs(
            db,
            [
                NotificationLog(
                    alert_id=alert.id,
                    notification_provider_id=mapping.notification_provider.id,
                    check_id=check.id,
                    check_success=False,
                    status=NotificationStatus.SUPPRESSED,
                    error_message=f"Suppressed: agent offline ({names})",
                    notification_hash=CHECK_SILENT,
                    sent_at=now,
                )
                for mapping in mappings
                if mapping.notification_provider.deleted_at is None
            ],
        )

    @staticmethod
    def _check_context(
        alert: Alert,
        check: Check,
        agents: Sequence[Agent],
        *,
        success: bool,
        message: str | None,
        is_recovery: bool = False,
    ) -> NotificationContext:
        agent = agents[0] if len(agents) == 1 else None
        return NotificationContext(
            check_name=check.display_name,
            check_type=check.check_type,
            target=check.target,
            agent_id=agent.id if agent else None,
            agent_name=(agent.hostname or agent.agent_name) if agent else None,
            status="success" if success else "no_data",
            success=success,
            timestamp=utc_now().isoformat(),
            error_message=message,
            alert_name=alert.name,
            alert_description=alert.description,
            is_recovery=is_recovery,
            custom_subject=alert.custom_subject,
            custom_message=alert.custom_message,
        )

    # ── agent_offline ────────────────────────────────────────────────────────

    @staticmethod
    async def _evaluate_agent_offline(
        db: AsyncSession,
        alert: Alert,
        assigned: Sequence[tuple[Check, list[Agent]]],
        now: datetime,
        counts: SilencePassCounts,
    ) -> None:
        """The agents this alert covers: those assigned any of its checks."""
        mapped = {m.check_id for m in alert.check_mappings if m.is_enabled}
        agents: dict[UUID, Agent] = {}
        for check, check_agents in assigned:
            if check.id in mapped:
                for agent in check_agents:
                    agents[agent.id] = agent
        if not agents:
            return

        missed = _missed_intervals(alert)
        last_rows = await AlertCRUD.last_notifications_by_subject(
            db, alert.id, agent_ids=list(agents)
        )
        for agent in agents.values():
            last_row = last_rows.get(agent.id)
            if not _agent_offline(agent, now, missed):
                if (
                    alert.notify_on_recovery
                    and last_row is not None
                    and last_row.notification_hash == AGENT_OFFLINE
                    and agent.last_seen is not None
                    and agent.last_seen > last_row.sent_at
                ):
                    await AlertEvaluationCoreService.send_to_providers(
                        db,
                        alert,
                        SilenceAlertCoreService._agent_context(
                            alert, agent, success=True, message=None, is_recovery=True
                        ),
                        _FRESH,
                        state_key=AGENT_ONLINE,
                        check_id=None,
                        agent_id=agent.id,
                    )
                    counts.recovered += 1
                continue

            counts.offline_agents += 1
            if _open_episode(last_row, AGENT_OFFLINE, agent.last_seen) and last_row:
                decision = AlertEvaluationCoreService.resend_decision(alert, last_row)
            else:
                decision = _FRESH
            if not decision.send:
                continue

            last = agent.last_seen
            message = (
                f"No heartbeat for {_ago(now - last)} "
                f"(expected every {_heartbeat_interval(agent)} s)"
                if last
                else "No heartbeat since it was approved"
            )
            await AlertEvaluationCoreService.send_to_providers(
                db,
                alert,
                SilenceAlertCoreService._agent_context(
                    alert, agent, success=False, message=message
                ),
                decision,
                state_key=AGENT_OFFLINE,
                check_id=None,
                agent_id=agent.id,
            )
            counts.notified += 1
            logger.info(
                "Agent offline",
                extra={
                    "alert_id": str(alert.id),
                    "agent_id": str(agent.id),
                    "agent_name": agent.agent_name,
                    "last_seen": last.isoformat() if last else None,
                    "is_resend": decision.is_resend,
                },
            )

    @staticmethod
    def _agent_context(
        alert: Alert,
        agent: Agent,
        *,
        success: bool,
        message: str | None,
        is_recovery: bool = False,
    ) -> NotificationContext:
        name = agent.agent_name or str(agent.id)
        return NotificationContext(
            check_name=f"Agent {name}",
            check_type="agent",
            target=agent.hostname or name,
            agent_id=agent.id,
            agent_name=agent.hostname or agent.agent_name,
            status="success" if success else "offline",
            success=success,
            timestamp=utc_now().isoformat(),
            error_message=message,
            alert_name=alert.name,
            alert_description=alert.description,
            is_recovery=is_recovery,
            custom_subject=alert.custom_subject,
            custom_message=alert.custom_message,
        )


async def evaluate_silence_alerts() -> dict[str, int]:
    """Scheduler job: one silence pass in its own session."""
    async with worker_session() as db:
        counts = await SilenceAlertCoreService.evaluate(db)
        return counts.as_dict()
