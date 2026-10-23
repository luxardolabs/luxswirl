"""
Silence alerts - the alerts that fire on the ABSENCE of data.

`no_data` fires for a check that is expected to run (an active agent is
assigned it) but whose newest result is older than `missed_intervals` of its
interval (see `app.core.liveness`). `agent_offline` fires for an active agent
whose newest heartbeat is that late. No incoming result can drive either, so
the scheduler runs `evaluate_silence_alerts` every minute.

Each silence is an episode, recorded by the newest notification row for the
alert and its subject (check or agent): it notifies once, then follows the
alert's resend settings, and ends when data arrives after that row. A recovery
notification goes out then if the alert asks for one and the silence was
notified. The next silence is a new episode.

A silent check whose agents are all offline is the agent's silence, not the
check's: its episode is recorded under its own state key, SUPPRESSED, so a
dead agent with a hundred checks sends one agent_offline notification, not a
hundred no_data ones. A silence already open (notified) stays as it is while
its agent restarts, so its recovery still goes out.

A pass first decides, reading only, then delivers. The scheduled job records
each notification in its own transaction, so a pass cut short (the job's
runtime limit, a slow provider) keeps the rows of what it already delivered
and the next pass does not deliver them again.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.datetime_utils import utc_now
from app.core.liveness import (
    DEFAULT_CHECK_INTERVAL_SECONDS,
    DEFAULT_MISSED_INTERVALS,
    MAX_MISSED_INTERVALS,
    agent_offline,
    heartbeat_interval,
    silent_after,
)
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
from app.shared.logger import get_logger

logger = get_logger("luxswirl.services.silence_alert")

# The state keys stored in notification_logs.notification_hash.
CHECK_SILENT = "no_data:silent"
CHECK_SUPPRESSED = "no_data:agent_offline"
CHECK_REPORTING = "no_data:reporting"
AGENT_OFFLINE = "agent_offline:offline"
AGENT_ONLINE = "agent_offline:online"

_FRESH = SendDecision(send=True, is_resend=False, resend_count=0)


@dataclass(frozen=True, slots=True)
class _Notice:
    """One thing a pass decided to record: a notification to send (`context`
    set) or a suppressed silence (`suppressed` set)."""

    alert_id: UUID
    state_key: str
    counter: str  # the SilencePassCounts field it counts toward
    check_id: UUID | None = None
    agent_id: UUID | None = None
    context: NotificationContext | None = None
    decision: SendDecision = _FRESH
    suppressed: str | None = None


def _missed_intervals(alert: Alert) -> int:
    """The alert's `missed_intervals`, or the default when unset or unusable."""
    value = (alert.trigger_config or {}).get("missed_intervals")
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 1 <= value <= MAX_MISSED_INTERVALS
    ):
        return value
    return DEFAULT_MISSED_INTERVALS


def _ago(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min"


class SilenceAlertCoreService:
    """Evaluates the no_data and agent_offline alerts."""

    @staticmethod
    async def evaluate(db: AsyncSession) -> SilencePassCounts:
        """One pass over every enabled silence alert, on one session."""
        counts = SilencePassCounts()
        for notice in await SilenceAlertCoreService._decide(db, counts):
            await SilenceAlertCoreService._record(db, notice, counts)
        return counts

    @staticmethod
    async def _decide(db: AsyncSession, counts: SilencePassCounts) -> list[_Notice]:
        """What this pass should record, reading only.

        Each alert is decided on its own: one that fails is logged and the
        others still go ahead.
        """
        alerts = await AlertCRUD.list_enabled_alerts_by_trigger(
            db, (AlertTriggerType.NO_DATA, AlertTriggerType.AGENT_OFFLINE)
        )
        if not alerts:
            return []

        now = utc_now()
        assigned = await AgentAssignmentCoreService.assigned_agents_by_check(db)
        notices: list[_Notice] = []
        for alert in alerts:
            try:
                async with db.begin_nested():
                    if not await SilenceAlertCoreService._has_live_provider(db, alert):
                        # Nothing could be delivered or recorded, so no
                        # episode could ever be recorded either.
                        continue
                    counts.alerts += 1
                    if alert.trigger_type == AlertTriggerType.NO_DATA:
                        notices += await SilenceAlertCoreService._decide_no_data(
                            db, alert, assigned, now, counts
                        )
                    else:
                        notices += await SilenceAlertCoreService._decide_agent_offline(
                            db, alert, assigned, now, counts
                        )
            except Exception:
                counts.failed += 1
                logger.exception(
                    "Silence alert evaluation failed",
                    extra={"alert_id": str(alert.id), "alert_name": alert.name},
                )
        return notices

    @staticmethod
    async def _record(
        db: AsyncSession, notice: _Notice, counts: SilencePassCounts
    ) -> None:
        """Send or record one decided notice."""
        if notice.suppressed is not None:
            mappings = await AlertCRUD.list_active_notif_mappings_for_alert(
                db, notice.alert_id
            )
            await AlertCRUD.add_notification_logs(
                db,
                [
                    NotificationLog(
                        alert_id=notice.alert_id,
                        notification_provider_id=mapping.notification_provider.id,
                        check_id=notice.check_id,
                        check_success=False,
                        status=NotificationStatus.SUPPRESSED,
                        error_message=notice.suppressed,
                        notification_hash=notice.state_key,
                        sent_at=utc_now(),
                    )
                    for mapping in mappings
                    if mapping.notification_provider.deleted_at is None
                ],
            )
            counts.suppressed += 1
            return

        if notice.context is None:
            return
        written = await AlertEvaluationCoreService.send_to_providers(
            db,
            notice.alert_id,
            notice.context,
            notice.decision,
            state_key=notice.state_key,
            check_id=notice.check_id,
            agent_id=notice.agent_id,
        )
        if not written:
            return
        setattr(counts, notice.counter, getattr(counts, notice.counter) + 1)
        logger.info(
            "Silence alert notified",
            extra={
                "alert_id": str(notice.alert_id),
                "check_id": str(notice.check_id) if notice.check_id else None,
                "agent_id": str(notice.agent_id) if notice.agent_id else None,
                "state": notice.state_key,
                "is_resend": notice.decision.is_resend,
            },
        )

    @staticmethod
    async def _has_live_provider(db: AsyncSession, alert: Alert) -> bool:
        mappings = await AlertCRUD.list_active_notif_mappings_for_alert(db, alert.id)
        return any(m.notification_provider.deleted_at is None for m in mappings)

    # ── no_data ──────────────────────────────────────────────────────────────

    @staticmethod
    async def _decide_no_data(
        db: AsyncSession,
        alert: Alert,
        assigned: Sequence[tuple[Check, list[Agent], list[Agent]]],
        now: datetime,
        counts: SilencePassCounts,
    ) -> list[_Notice]:
        mappings = {m.check_id: m for m in alert.check_mappings if m.is_enabled}
        # Only checks some active agent is to run: a disabled check, or one
        # whose agents are all paused, is not expected to report.
        targets = [
            (c, agents, pool)
            for c, agents, pool in assigned
            if c.id in mappings and agents
        ]
        if not targets:
            return []

        missed = _missed_intervals(alert)
        check_ids = [check.id for check, _, _ in targets]
        last_rows = await AlertCRUD.last_notifications_by_subject(
            db, alert.id, check_ids=check_ids
        )
        # Bounded by the thresholds alone: an old episode never widens the
        # read for every check of the alert.
        since = min(
            now - silent_after(c.interval_seconds, missed) for c, _, _ in targets
        )
        latest = await CheckResultCRUD.latest_result_times(db, check_ids, since)

        notices: list[_Notice] = []
        silent: list[tuple[Check, list[Agent], SendDecision, datetime | None]] = []
        for check, agents, pool in targets:
            last_result = latest.get(check.id)
            last_row = last_rows.get(check.id)
            threshold = silent_after(check.interval_seconds, missed)
            # A distributed check that moved off an offline agent is the new
            # agent's once it can know: it learns at its next heartbeat after
            # the move, so it gets a full threshold from then.
            expected_from = last_result or check.created_at
            moved_at = AgentAssignmentCoreService.handed_over_at(check, pool, now)
            if moved_at is not None:
                learned_by = moved_at + timedelta(seconds=heartbeat_interval(agents[0]))
                expected_from = max(expected_from, learned_by)
            if expected_from >= now - threshold:
                if (
                    alert.notify_on_recovery
                    and last_row is not None
                    and last_row.notification_hash == CHECK_SILENT
                    and last_result is not None
                    and last_result > last_row.sent_at
                ):
                    notices.append(
                        _Notice(
                            alert_id=alert.id,
                            state_key=CHECK_REPORTING,
                            counter="recovered",
                            check_id=check.id,
                            context=SilenceAlertCoreService._check_context(
                                alert, check, agents, message=None, recovered=True
                            ),
                        )
                    )
                continue

            counts.silent_checks += 1
            mapping = mappings[check.id]
            if mapping.snoozed_until and mapping.snoozed_until > now:
                continue

            in_episode = await SilenceAlertCoreService._episode_open(
                db, check, last_row, last_result
            )
            offline = [
                a for a in agents if agent_offline(a, now, DEFAULT_MISSED_INTERVALS)
            ]
            if len(offline) == len(agents):
                # The agents' silence. An open episode (notified or already
                # suppressed) stays as it is.
                if not in_episode:
                    names = ", ".join(a.agent_name or str(a.id) for a in offline)
                    notices.append(
                        _Notice(
                            alert_id=alert.id,
                            state_key=CHECK_SUPPRESSED,
                            counter="suppressed",
                            check_id=check.id,
                            suppressed=f"Suppressed: agent offline ({names})",
                        )
                    )
                continue

            if in_episode and last_row and last_row.notification_hash == CHECK_SILENT:
                decision = AlertEvaluationCoreService.resend_decision(alert, last_row)
            else:
                # First contact, or a suppressed silence that outlived its
                # agent's outage: now the check's own.
                decision = _FRESH
            if not decision.send:
                continue
            silent.append((check, agents, decision, last_result))

        # A first notice for a check silent longer than the look-back window
        # finds out when it last reported, for the message: one read for all.
        undated = [
            check.id
            for check, _, decision, last_result in silent
            if last_result is None and not decision.is_resend
        ]
        newest = await CheckResultCRUD.newest_result_times(db, undated)
        for check, agents, decision, last_result in silent:
            last_result = last_result or newest.get(check.id)
            age = now - (last_result or check.created_at)
            notices.append(
                _Notice(
                    alert_id=alert.id,
                    state_key=CHECK_SILENT,
                    counter="notified",
                    check_id=check.id,
                    decision=decision,
                    context=SilenceAlertCoreService._check_context(
                        alert,
                        check,
                        agents,
                        message=(
                            f"No results for {_ago(age)} (expected every "
                            f"{check.interval_seconds or DEFAULT_CHECK_INTERVAL_SECONDS} s)"
                            if last_result
                            else f"No results since it was created {_ago(age)} ago"
                        ),
                    ),
                )
            )
        return notices

    @staticmethod
    async def _episode_open(
        db: AsyncSession,
        check: Check,
        last_row: NotificationLog | None,
        last_result: datetime | None,
    ) -> bool:
        """Whether `last_row` records a silence of `check` that no result has
        ended since."""
        if last_row is None or last_row.notification_hash not in (
            CHECK_SILENT,
            CHECK_SUPPRESSED,
        ):
            return False
        if last_result is None:
            # Not in the threshold window: one probe for anything newer than
            # the row, which would mean the check reported in between.
            last_result = await CheckResultCRUD.newest_result_after(
                db, check.id, last_row.sent_at
            )
        return last_result is None or last_result <= last_row.sent_at

    @staticmethod
    def _check_context(
        alert: Alert,
        check: Check,
        agents: Sequence[Agent],
        *,
        message: str | None,
        recovered: bool = False,
    ) -> NotificationContext:
        agent = agents[0] if len(agents) == 1 else None
        return NotificationContext(
            check_name=check.display_name,
            check_type=check.check_type,
            target=check.target,
            agent_id=agent.id if agent else None,
            agent_name=(agent.hostname or agent.agent_name) if agent else None,
            status="success" if recovered else "no_data",
            success=recovered,
            timestamp=utc_now().isoformat(),
            error_message=message,
            alert_name=alert.name,
            alert_description=alert.description,
            is_recovery=recovered,
            custom_subject=alert.custom_subject,
            custom_message=alert.custom_message,
        )

    # ── agent_offline ────────────────────────────────────────────────────────

    @staticmethod
    async def _decide_agent_offline(
        db: AsyncSession,
        alert: Alert,
        assigned: Sequence[tuple[Check, list[Agent], list[Agent]]],
        now: datetime,
        counts: SilencePassCounts,
    ) -> list[_Notice]:
        """The agents this alert covers: every active agent any of its checks
        may run on, so an agent whose distributed checks moved to another
        while it is offline is still watched."""
        mapped = {m.check_id for m in alert.check_mappings if m.is_enabled}
        agents: dict[UUID, Agent] = {}
        for check, _, check_agents in assigned:
            if check.id in mapped:
                for agent in check_agents:
                    agents[agent.id] = agent
        if not agents:
            return []

        missed = _missed_intervals(alert)
        last_rows = await AlertCRUD.last_notifications_by_subject(
            db, alert.id, agent_ids=list(agents)
        )
        notices: list[_Notice] = []
        for agent in agents.values():
            last_row = last_rows.get(agent.id)
            last = agent.last_seen
            if not agent_offline(agent, now, missed):
                if (
                    alert.notify_on_recovery
                    and last_row is not None
                    and last_row.notification_hash == AGENT_OFFLINE
                    and last is not None
                    and last > last_row.sent_at
                ):
                    notices.append(
                        _Notice(
                            alert_id=alert.id,
                            state_key=AGENT_ONLINE,
                            counter="recovered",
                            agent_id=agent.id,
                            context=SilenceAlertCoreService._agent_context(
                                alert, agent, message=None, recovered=True
                            ),
                        )
                    )
                continue

            counts.offline_agents += 1
            in_episode = (
                last_row is not None
                and last_row.notification_hash == AGENT_OFFLINE
                and (last is None or last <= last_row.sent_at)
            )
            decision = (
                AlertEvaluationCoreService.resend_decision(alert, last_row)
                if in_episode and last_row
                else _FRESH
            )
            if not decision.send:
                continue

            notices.append(
                _Notice(
                    alert_id=alert.id,
                    state_key=AGENT_OFFLINE,
                    counter="notified",
                    agent_id=agent.id,
                    decision=decision,
                    context=SilenceAlertCoreService._agent_context(
                        alert,
                        agent,
                        message=(
                            f"No heartbeat for {_ago(now - last)} "
                            f"(expected every {heartbeat_interval(agent)} s)"
                            if last
                            else "No heartbeat since it was approved"
                        ),
                    ),
                )
            )
        return notices

    @staticmethod
    def _agent_context(
        alert: Alert,
        agent: Agent,
        *,
        message: str | None,
        recovered: bool = False,
    ) -> NotificationContext:
        name = agent.agent_name or str(agent.id)
        return NotificationContext(
            check_name=f"Agent {name}",
            check_type="agent",
            target=agent.hostname or name,
            agent_id=agent.id,
            agent_name=agent.hostname or agent.agent_name,
            status="success" if recovered else "offline",
            success=recovered,
            timestamp=utc_now().isoformat(),
            error_message=message,
            alert_name=alert.name,
            alert_description=alert.description,
            is_recovery=recovered,
            custom_subject=alert.custom_subject,
            custom_message=alert.custom_message,
        )


async def evaluate_silence_alerts() -> dict[str, int]:
    """Scheduler job: decide in one session, then record each notice in its own,
    so what is delivered is committed with it before the next is sent."""
    counts = SilencePassCounts()
    async with worker_session() as db:
        notices = await SilenceAlertCoreService._decide(db, counts)
    for notice in notices:
        async with worker_session() as db:
            await SilenceAlertCoreService._record(db, notice, counts)
    return counts.as_dict()
