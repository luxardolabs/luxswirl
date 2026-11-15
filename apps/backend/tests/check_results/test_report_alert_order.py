"""A report's alerts are decided after it, by the alert decision worker, and
delivered after that, by the delivery worker.

The report route stores a report's results and queues each one an alert
evaluates, in its own transaction; it decides nothing, so the agent is
answered as soon as its results are stored. The alert decision worker then
decides each check's queued results oldest first, one short transaction per
check: each decision is a `pending` notification row, de-duplicated against
the decisions already committed, and a result no newer than the check's
cursor is stale. The delivery worker claims the pending rows (FOR UPDATE SKIP
LOCKED), delivers each with no transaction open and records the outcome, one
alert and check's notifications in the order they were decided.
"""

from __future__ import annotations

import asyncio
import time
import zlib
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.rate_limit import limiter
from app.crud.alert_crud import AlertCRUD
from app.crud.alert_decision_crud import AlertDecisionCRUD
from app.crud.check_result_crud import CheckResultCRUD
from app.main import app
from app.models.agent_model import Agent
from app.models.alert_model import Alert
from app.models.check_model import Check
from app.models.notification_log_model import NotificationLog
from app.notifications.providers.base import NotificationContext
from app.notifications.providers.webhook import WebhookNotificationProvider
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.alert_decision_core_service import AlertDecisionCoreService
from app.services.core.notification_delivery_core_service import (
    CLAIM_LEASE,
    MAX_ATTEMPTS,
    RETRY_DELAYS,
    NotificationDeliveryCoreService,
)
from fixtures.factories import (
    make_agent,
    make_alert,
    make_alert_check_mapping,
    make_alert_notification_mapping,
    make_check,
    make_check_result,
    make_notification_provider,
)
from fixtures.outbox import decide_committed, deliver_committed
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.integration


class _Webhook:
    """Records each send. The first send for `hold` waits for `release`;
    every send waits `delay` seconds (only the first, with `delay_once`)."""

    def __init__(
        self, hold: str | None = None, delay: float = 0.0, delay_once: bool = False
    ) -> None:
        self.hold = hold
        self.delay = delay
        self.delay_once = delay_once
        self.sent: list[tuple[str, str]] = []
        self.holding = asyncio.Event()
        self.release = asyncio.Event()

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        hook = self

        async def send(self: Any, context: NotificationContext) -> bool:
            first = not hook.sent
            hook.sent.append((context.check_name, context.status))
            if context.check_name == hook.hold and not hook.holding.is_set():
                hook.holding.set()
                await hook.release.wait()
            if hook.delay and (first or not hook.delay_once):
                await asyncio.sleep(hook.delay)
            return True

        monkeypatch.setattr(WebhookNotificationProvider, "send", send)

    def of(self, name: str) -> list[str]:
        return [status for check, status in self.sent if check == name]


async def _seed(
    worker_db: Any, names: tuple[str, ...] = ("y-service", "z-service"), **alert: Any
) -> dict[str, Any]:
    """An approved agent with the named checks, one alert on all of them."""
    async with worker_db() as s:
        agent = make_agent(approval_status="pending")
        s.add(agent)
        await s.flush()
        _, key = await AgentCoreService.approve_agent(s, agent.id, actor_is_admin=True)
        checks = [make_check(agent_id=agent.id, display_name=n) for n in names]
        provider = make_notification_provider()
        made = make_alert(**alert)
        s.add_all([*checks, provider, made])
        await s.flush()
        s.add_all(
            [
                *[
                    make_alert_check_mapping(alert_id=made.id, check_id=c.id)
                    for c in checks
                ],
                make_alert_notification_mapping(
                    alert_id=made.id, notification_provider_id=provider.id
                ),
            ]
        )
        await s.commit()
    ids: dict[str, Any] = {"agent": agent.id, "key": key, "alert": made.id}
    for name, check in zip(names, checks, strict=True):
        ids[name] = check.id
    return ids


def _wire(check_id: UUID, at: Any, *, success: bool) -> dict[str, Any]:
    return {
        "check_id": str(check_id),
        "display_name": "probe",
        "check_type": "ping",
        "target": "192.0.2.10",
        "timestamp": at.isoformat(),
        "success": success,
        "error": None if success else "down",
        "latency_ms": 5.0,
    }


@pytest.fixture
def agent_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    limiter.reset()


async def _post(ids: dict[str, Any], results: list[dict[str, Any]]) -> int:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", timeout=None
    ) as client:
        response = await client.post(
            "/api/v1/agent/reports",
            json={"agent_id": str(ids["agent"]), "checks": results},
            headers={"Authorization": f"Bearer {ids['key']}"},
        )
    return response.status_code


async def _last_state(worker_db: Any, alert_id: UUID, check_id: UUID) -> str | None:
    async with worker_db() as s:
        row = await AlertCRUD.get_last_notification_for_dedup(s, alert_id, check_id)
        return row.notification_hash if row else None


async def _statuses(worker_db: Any, alert_id: UUID) -> list[str]:
    async with worker_db() as s:
        rows = await s.execute(
            select(NotificationLog.status)
            .where(NotificationLog.alert_id == alert_id)
            .order_by(NotificationLog.decision_seq)
        )
        return [str(status) for status in rows.scalars().all()]


async def test_a_report_is_answered_with_its_notification_queued_not_sent(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch, agent_auth: None
) -> None:
    """The report's answer waits on no decision and no provider: the alert
    decision worker decides its notification (a pending row) after the
    agent is answered, and the delivery worker sends it."""
    ids = await _seed(worker_db)
    hook = _Webhook()
    hook.install(monkeypatch)

    assert await _post(ids, [_wire(ids["y-service"], utc_now(), success=False)]) == 202

    assert hook.sent == []
    assert await _statuses(worker_db, ids["alert"]) == []
    assert await decide_committed() == 1
    assert await _statuses(worker_db, ids["alert"]) == ["pending"]
    assert await deliver_committed() == 1
    assert hook.of("y-service") == ["error"]
    assert await _statuses(worker_db, ids["alert"]) == ["sent"]


async def test_three_down_checks_are_answered_well_inside_the_agents_timeout(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch, agent_auth: None
) -> None:
    """Three checks down behind a webhook that waits out its whole 10 s
    timeout: the report is answered at once (the agent gives up at 30 s),
    and the worker delivers the three side by side."""
    names = ("a", "b", "c")
    ids = await _seed(worker_db, names)
    hook = _Webhook(delay=10.5)
    hook.install(monkeypatch)
    at = utc_now() - timedelta(seconds=5)

    started = time.monotonic()
    status = await _post(ids, [_wire(ids[n], at, success=False) for n in names])
    answered_after = time.monotonic() - started

    assert status == 202
    assert answered_after < 5, answered_after
    started = time.monotonic()
    assert await deliver_committed() == 3
    assert time.monotonic() - started < 20
    assert sorted(check for check, _ in hook.sent) == ["a", "b", "c"]


async def test_a_resent_report_queues_its_outage_once(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch, agent_auth: None
) -> None:
    """An agent that never had its answer (it gave up, the connection
    dropped) sends the report again, even while the first send's
    notification is being delivered: the outage is notified once."""
    ids = await _seed(worker_db)
    hook = _Webhook(hold="y-service")
    hook.install(monkeypatch)
    results = [
        _wire(ids["y-service"], utc_now() - timedelta(seconds=30), success=False)
    ]

    assert await _post(ids, results) == 202
    delivering = asyncio.create_task(deliver_committed())
    await asyncio.wait_for(hook.holding.wait(), 10)
    assert await _post(ids, results) == 202
    hook.release.set()
    await asyncio.wait_for(delivering, 10)
    assert await _post(ids, results) == 202
    await deliver_committed()

    assert hook.of("y-service") == ["error"], hook.sent


async def test_a_recovery_waits_for_its_outage_to_be_delivered(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch, agent_auth: None
) -> None:
    """The check's recovery is decided while its outage is still being
    delivered. A second worker pass does not deliver the recovery first;
    once the outage is sent, the recovery is, and the alert ends up."""
    ids = await _seed(worker_db)
    hook = _Webhook(hold="y-service")
    hook.install(monkeypatch)
    t0 = utc_now() - timedelta(seconds=60)

    assert await _post(ids, [_wire(ids["y-service"], t0, success=False)]) == 202
    first_pass = asyncio.create_task(deliver_committed())
    await asyncio.wait_for(hook.holding.wait(), 10)
    up = [_wire(ids["y-service"], t0 + timedelta(seconds=10), success=True)]
    assert await _post(ids, up) == 202
    # A second worker meanwhile: the recovery waits behind its outage.
    assert await deliver_committed() == 0
    assert hook.of("y-service") == ["error"]
    hook.release.set()
    await asyncio.wait_for(first_pass, 10)
    await deliver_committed()
    # The report after: still up, nothing more.
    up = [_wire(ids["y-service"], t0 + timedelta(seconds=20), success=True)]
    assert await _post(ids, up) == 202
    await deliver_committed()

    assert hook.of("y-service") == ["error", "success"], hook.sent
    assert await _last_state(worker_db, ids["alert"], ids["y-service"]) == "status:up"


async def test_a_recovery_reported_while_another_check_delivers_ends_the_alert_up(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch, agent_auth: None
) -> None:
    """One report holds two outages, one of them stuck in delivery; the next
    report's recovery of the other is decided after it and sent: the check
    that is up never ends with its alert down."""
    ids = await _seed(worker_db, ("y-service", "slow-service"))
    hook = _Webhook(hold="slow-service")
    hook.install(monkeypatch)
    t0 = utc_now() - timedelta(seconds=60)

    assert (
        await _post(
            ids,
            [
                _wire(ids["slow-service"], t0, success=False),
                _wire(ids["y-service"], t0 + timedelta(seconds=1), success=False),
            ],
        )
        == 202
    )
    stuck = asyncio.create_task(deliver_committed())
    await asyncio.wait_for(hook.holding.wait(), 10)
    up = [_wire(ids["y-service"], t0 + timedelta(seconds=11), success=True)]
    assert await _post(ids, up) == 202
    await deliver_committed()
    hook.release.set()
    await asyncio.wait_for(stuck, 10)

    assert hook.of("y-service") == ["error", "success"], hook.sent
    assert await _last_state(worker_db, ids["alert"], ids["y-service"]) == "status:up"


async def test_overlapping_reports_of_one_check_notify_its_outage_once(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch, agent_auth: None
) -> None:
    """Two reports of one check at once (a replicate check's agents): their
    decisions are serialised per check, so the second sees the first's."""
    ids = await _seed(worker_db)
    hook = _Webhook()
    hook.install(monkeypatch)
    t0 = utc_now() - timedelta(seconds=60)

    statuses = await asyncio.gather(
        _post(ids, [_wire(ids["y-service"], t0, success=False)]),
        _post(
            ids, [_wire(ids["y-service"], t0 + timedelta(seconds=11), success=False)]
        ),
    )
    await deliver_committed()

    assert statuses == [202, 202]
    assert hook.of("y-service") == ["error"], hook.sent


async def test_a_check_whose_decisions_are_held_is_left_for_the_next_pass(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch, agent_auth: None
) -> None:
    """While another transaction holds a check's decision lock, the report
    is still stored and answered, and the worker leaves the check queued
    rather than wait; once the lock is free the recovery is decided and
    sent: it is never dropped."""
    ids = await _seed(worker_db)
    hook = _Webhook()
    hook.install(monkeypatch)
    t0 = utc_now() - timedelta(seconds=60)
    assert await _post(ids, [_wire(ids["y-service"], t0, success=False)]) == 202
    await deliver_committed()
    up = [_wire(ids["y-service"], t0 + timedelta(seconds=10), success=True)]

    async with worker_db() as holder:
        assert await AlertDecisionCRUD.try_lock_check(
            holder, AlertDecisionCoreService.decision_lock_key(ids["y-service"])
        )
        assert await _post(ids, up) == 202
        assert await decide_committed() == 0
        await holder.rollback()
    async with worker_db() as s:
        stored = await CheckResultCRUD.list_for_check_at(
            s, ids["y-service"], [t0 + timedelta(seconds=10)]
        )
    assert len(stored) == 1
    assert hook.of("y-service") == ["error"]

    await deliver_committed()
    assert hook.of("y-service") == ["error", "success"], hook.sent


async def test_a_delivery_slower_than_the_idle_transaction_timeout_is_sent_once(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch, agent_auth: None
) -> None:
    """A receiver that takes 31 s to answer, past the pool's 30 s
    idle_in_transaction_session_timeout: no transaction is open while it is
    waited on, so the delivery is recorded as sent, and the next result in
    the same state sends nothing."""
    ids = await _seed(worker_db)
    hook = _Webhook(delay=31, delay_once=True)
    hook.install(monkeypatch)
    at = utc_now() - timedelta(seconds=40)

    assert await _post(ids, [_wire(ids["y-service"], at, success=False)]) == 202
    await deliver_committed()
    still_down = [_wire(ids["y-service"], at + timedelta(seconds=10), success=False)]
    assert await _post(ids, still_down) == 202
    await deliver_committed()

    assert hook.of("y-service") == ["error"], hook.sent
    assert await _statuses(worker_db, ids["alert"]) == ["sent"]


async def test_a_result_older_than_the_last_decided_one_queues_nothing(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A result decided after a newer one (another agent's report of a
    replicate check, delayed) queues nothing: a check that is up never ends
    with its alert down."""
    ids = await _seed(worker_db)
    hook = _Webhook()
    hook.install(monkeypatch)
    t0 = utc_now() - timedelta(seconds=60)
    stamps = [t0, t0 + timedelta(seconds=10), t0 + timedelta(seconds=5)]

    async with worker_db() as s:
        s.add_all(
            make_check_result(
                check_id=ids["y-service"],
                agent_id=ids["agent"],
                timestamp=stamp,
                success=stamp == stamps[1],
            )
            for stamp in stamps
        )
        await s.commit()

    # Down, then up, then the delayed down between them, each decided
    # before the next is queued.
    for stamp in stamps:
        async with worker_db() as s:
            await AlertDecisionCoreService.queue_report_results(
                s, [(ids["y-service"], stamp)]
            )
            await s.commit()
        await decide_committed()
    await deliver_committed()

    assert hook.of("y-service") == ["error", "success"], hook.sent
    assert await _last_state(worker_db, ids["alert"], ids["y-service"]) == "status:up"


async def test_a_batch_holding_an_outage_and_its_recovery_notifies_both(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch, agent_auth: None
) -> None:
    """Each result is judged against the results up to it, not the check's
    newest: a report carrying two failures (the alert needs two in a row)
    and the recovery after them sends the outage, then the recovery."""
    ids = await _seed(
        worker_db,
        trigger_config={"on_status": ["error"], "consecutive_failures": 2},
    )
    hook = _Webhook()
    hook.install(monkeypatch)
    t0 = utc_now() - timedelta(seconds=60)

    status = await _post(
        ids,
        [
            _wire(ids["y-service"], t0, success=False),
            _wire(ids["y-service"], t0 + timedelta(seconds=5), success=False),
            _wire(ids["y-service"], t0 + timedelta(seconds=10), success=True),
        ],
    )
    await deliver_committed()

    assert status == 202
    assert hook.of("y-service") == ["error", "success"], hook.sent


async def test_no_recovery_is_sent_for_an_outage_never_notified(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch, agent_auth: None
) -> None:
    """One failure where the alert needs two, then a success: nobody was
    told of an outage, so nobody is told it ended."""
    ids = await _seed(
        worker_db,
        trigger_config={"on_status": ["error"], "consecutive_failures": 2},
    )
    hook = _Webhook()
    hook.install(monkeypatch)
    t0 = utc_now() - timedelta(seconds=60)

    assert await _post(ids, [_wire(ids["y-service"], t0, success=False)]) == 202
    up = [_wire(ids["y-service"], t0 + timedelta(seconds=10), success=True)]
    assert await _post(ids, up) == 202
    await deliver_committed()

    assert hook.sent == []


async def _runs_within(worker_db: Any, stmt: Any, seconds: float) -> bool:
    async def go() -> None:
        async with worker_db() as op:
            await op.execute(text("SET LOCAL lock_timeout = '3s'"))
            await op.execute(stmt)
            await op.commit()

    try:
        await asyncio.wait_for(go(), seconds)
        return True
    except TimeoutError, DBAPIError:
        return False


async def test_operator_writes_are_not_held_by_a_delivery_in_flight(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch, agent_auth: None
) -> None:
    """While a check's notification is delivered, the agent can still be
    locked and changed (pause, revoke), the check renamed, and the alert
    deleted (its rows go with it, and the outcome finds none to record)."""
    ids = await _seed(worker_db)
    hook = _Webhook(hold="z-service")
    hook.install(monkeypatch)
    t0 = utc_now() - timedelta(seconds=60)
    assert (
        await _post(
            ids,
            [
                _wire(ids["y-service"], t0, success=False),
                _wire(ids["z-service"], t0 + timedelta(seconds=1), success=False),
            ],
        )
        == 202
    )
    task = asyncio.create_task(deliver_committed())
    await asyncio.wait_for(hook.holding.wait(), 10)
    try:
        locked = await _runs_within(
            worker_db,
            select(Agent.id).where(Agent.id == ids["agent"]).with_for_update(),
            6,
        )
        changed = await _runs_within(
            worker_db,
            update(Agent).where(Agent.id == ids["agent"]).values(hostname="h2"),
            6,
        )
        renamed = await _runs_within(
            worker_db,
            update(Check).where(Check.id == ids["z-service"]).values(display_name="z2"),
            6,
        )
        deleted = await _runs_within(
            worker_db, delete(Alert).where(Alert.id == ids["alert"]), 6
        )
    finally:
        hook.release.set()
        delivered = await asyncio.wait_for(task, 10)
    assert locked and changed and renamed and deleted
    assert delivered == 2
    assert hook.of("y-service") == ["error"]
    assert await _statuses(worker_db, ids["alert"]) == []


async def test_a_claim_whose_worker_stopped_is_delivered_again_after_its_lease(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch, agent_auth: None
) -> None:
    """A worker that claims a row and stops before recording it (a crash, a
    restart) leaves it `sending`: no one else takes it while the lease runs,
    and the next worker past the lease delivers it."""
    ids = await _seed(worker_db)
    hook = _Webhook()
    hook.install(monkeypatch)
    assert await _post(ids, [_wire(ids["y-service"], utc_now(), success=False)]) == 202
    await decide_committed()
    now = utc_now()
    async with worker_db() as s:
        batch = await NotificationDeliveryCoreService.claim_due(s, now, 10)
        await s.commit()
    assert len(batch.deliveries) == 1

    assert await deliver_committed(now=now + CLAIM_LEASE / 2) == 0
    assert await deliver_committed(now=now + CLAIM_LEASE + timedelta(seconds=1)) == 1
    assert hook.of("y-service") == ["error"]
    assert await _statuses(worker_db, ids["alert"]) == ["sent"]
    # The stopped worker's late outcome is not recorded over the new one.
    outcome = await NotificationDeliveryCoreService.deliver(
        batch.deliveries[0], batch.network
    )
    async with worker_db() as s:
        recorded = await NotificationDeliveryCoreService.record(
            s, batch.deliveries[0], outcome, utc_now()
        )
        await s.commit()
    assert recorded is False


async def test_a_failed_delivery_is_retried_with_backoff_then_given_up(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch, agent_auth: None
) -> None:
    """A provider that keeps failing: each attempt leaves the row retrying,
    due again only after its delay, until the last attempt fails it; the
    alert's next notification for the check waits for it in turn."""
    ids = await _seed(worker_db)
    attempts: list[str] = []

    async def refuse(self: Any, context: NotificationContext) -> bool:
        attempts.append(context.status)
        return False

    monkeypatch.setattr(WebhookNotificationProvider, "send", refuse)
    t0 = utc_now() - timedelta(seconds=60)
    assert await _post(ids, [_wire(ids["y-service"], t0, success=False)]) == 202
    up = [_wire(ids["y-service"], t0 + timedelta(seconds=10), success=True)]
    assert await _post(ids, up) == 202
    await decide_committed()

    now = utc_now()
    assert await deliver_committed(now=now) == 1
    assert await _statuses(worker_db, ids["alert"]) == ["retrying", "pending"]
    # Not due before its delay, and the recovery stays behind it.
    assert await deliver_committed(now=now + RETRY_DELAYS[0] / 2) == 0
    for delay in RETRY_DELAYS:
        now += delay
        await deliver_committed(now=now)

    assert attempts == ["error"] * MAX_ATTEMPTS + ["success"]
    assert await _statuses(worker_db, ids["alert"]) == ["failed", "retrying"]


async def _seed_many(
    worker_db: Any, n: int, *, bucket_of: set[int] | None = None
) -> dict[str, Any]:
    """An approved agent with `n` checks, one alert on them all. With
    `bucket_of`, each check's id hashes (crc32 mod 1024) into one of those
    buckets."""
    async with worker_db() as s:
        agent = make_agent(approval_status="pending")
        s.add(agent)
        await s.flush()
        _, key = await AgentCoreService.approve_agent(s, agent.id, actor_is_admin=True)
        checks = []
        for i in range(n):
            check_id = uuid4()
            while bucket_of is not None and _bucket(check_id) not in bucket_of:
                check_id = uuid4()
            checks.append(
                make_check(id=check_id, agent_id=agent.id, display_name=f"k{i}")
            )
        provider = make_notification_provider()
        made = make_alert()
        s.add_all([*checks, provider, made])
        await s.flush()
        s.add_all(
            [make_alert_check_mapping(alert_id=made.id, check_id=c.id) for c in checks]
        )
        s.add(
            make_alert_notification_mapping(
                alert_id=made.id, notification_provider_id=provider.id
            )
        )
        await s.commit()
    return {"agent": agent.id, "key": key, "checks": [c.id for c in checks]}


def _bucket(check_id: UUID) -> int:
    return zlib.crc32(check_id.bytes) % 1024


async def test_an_unrelated_agents_report_is_stored_while_a_big_report_decides(
    worker_db: Any, agent_auth: None
) -> None:
    """Agent A replays a backlog: 2000 failing results, one alert on its
    checks. Agent B reports one result for its own check, whose id hashes
    alike one of A's (so any per-bucket serialisation of reports would hold
    B behind A). B's result is stored, and A is answered well inside the
    agent's 30 s timeout."""
    a = await _seed_many(worker_db, 2000)
    b = await _seed_many(worker_db, 1, bucket_of={_bucket(c) for c in a["checks"]})
    at = utc_now() - timedelta(seconds=30)

    started = time.monotonic()
    a_task = asyncio.create_task(
        _post(a, [_wire(c, at, success=False) for c in a["checks"]])
    )
    await asyncio.sleep(1.5)
    b_status = await _post(b, [_wire(b["checks"][0], at, success=False)])
    a_status = await a_task
    a_took = time.monotonic() - started
    async with worker_db() as s:
        b_stored = await CheckResultCRUD.list_for_check_at(s, b["checks"][0], [at])

    assert (a_status, b_status, len(b_stored)) == (202, 202, 1)
    assert a_took < 30, f"A's report answered after {a_took:.1f}s"


async def test_operator_write_on_the_agent_is_not_held_by_a_reports_decisions(
    worker_db: Any, agent_auth: None
) -> None:
    """While a 1000-result report is handled, the operator pauses (disables,
    archives, revokes) its agent, locking the agent's row: the lock is had
    within the pool's 5 s statement timeout."""
    a = await _seed_many(worker_db, 1000)
    at = utc_now() - timedelta(seconds=30)
    a_task = asyncio.create_task(
        _post(a, [_wire(c, at, success=False) for c in a["checks"]])
    )
    await asyncio.sleep(1.5)
    locked = True
    try:
        async with worker_db() as s:
            # As pause, disable, archive and revoke lock it (AgentCRUD.lock).
            await s.execute(
                select(Agent.id).where(Agent.id == a["agent"]).with_for_update()
            )
            await s.commit()
    except DBAPIError:
        locked = False

    assert await a_task == 202
    assert locked, "the agent's row was held past the statement timeout"
