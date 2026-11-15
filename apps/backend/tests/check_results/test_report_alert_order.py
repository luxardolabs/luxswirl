"""A report's alerts are evaluated in the order the check's results happened.

The report route stores a report's results and commits them, then evaluates
their alerts, and only then answers the agent. The agent sends one report at a
time, so one report's notifications are decided before its next report is
even sent, and a crash before the answer makes the agent send the report
again: the results it names are evaluated whether this request stored them or
an earlier one did. Each check's results are evaluated in a transaction of
their own, under an advisory lock on the check, so two evaluations of one
check (a replicate check's agents, a resend) never decide on each other's
uncommitted notifications. A result no newer than the one an alert last
recorded for its check is not evaluated for that alert again.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.rate_limit import limiter
from app.crud.alert_crud import AlertCRUD
from app.crud.check_result_crud import CheckResultCRUD
from app.main import app
from app.models.agent_model import Agent
from app.models.check_model import Check
from app.notifications.providers.base import NotificationContext
from app.notifications.providers.webhook import WebhookNotificationProvider
from app.schemas.check_result_schema import AgentReportRequest, CheckResultCreate
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.alert_evaluation_core_service import AlertEvaluationCoreService
from app.services.core.check_result_core_service import CheckResultCoreService
from fixtures.factories import (
    make_agent,
    make_alert,
    make_alert_check_mapping,
    make_alert_notification_mapping,
    make_check,
    make_check_result,
    make_notification_provider,
)
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.integration


class _Webhook:
    """Records each send; a send for `hold` waits for `release` the first time."""

    def __init__(self, hold: str | None = None) -> None:
        self.hold = hold
        self.sent: list[tuple[str, str]] = []
        self.holding = asyncio.Event()
        self.release = asyncio.Event()

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        hook = self

        async def send(self: Any, context: NotificationContext) -> bool:
            hook.sent.append((context.check_name, context.status))
            if context.check_name == hook.hold and not hook.holding.is_set():
                hook.holding.set()
                await hook.release.wait()
            return True

        monkeypatch.setattr(WebhookNotificationProvider, "send", send)

    def of(self, name: str) -> list[str]:
        return [status for check, status in self.sent if check == name]


async def _seed(worker_db: Any, **alert: Any) -> dict[str, Any]:
    """An approved agent with checks `y` and `z`, one alert on both."""
    async with worker_db() as s:
        agent = make_agent(approval_status="pending")
        s.add(agent)
        await s.flush()
        _, key = await AgentCoreService.approve_agent(s, agent.id, actor_is_admin=True)
        y = make_check(agent_id=agent.id, display_name="y-service")
        z = make_check(agent_id=agent.id, display_name="z-service")
        provider = make_notification_provider()
        made = make_alert(**alert)
        s.add_all([y, z, provider, made])
        await s.flush()
        s.add_all(
            [
                make_alert_check_mapping(alert_id=made.id, check_id=y.id),
                make_alert_check_mapping(alert_id=made.id, check_id=z.id),
                make_alert_notification_mapping(
                    alert_id=made.id, notification_provider_id=provider.id
                ),
            ]
        )
        await s.commit()
    return {"agent": agent.id, "key": key, "y": y.id, "z": z.id, "alert": made.id}


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


def _body(ids: dict[str, Any], results: list[dict[str, Any]]) -> dict[str, Any]:
    return {"agent_id": str(ids["agent"]), "checks": results}


@pytest.fixture
def agent_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    limiter.reset()


async def _post(ids: dict[str, Any], results: list[dict[str, Any]]) -> int:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/agent/reports",
            json=_body(ids, results),
            headers={"Authorization": f"Bearer {ids['key']}"},
        )
    return response.status_code


async def _last_state(worker_db: Any, alert_id: UUID, check_id: UUID) -> str | None:
    async with worker_db() as s:
        row = await AlertCRUD.get_last_notification_for_dedup(s, alert_id, check_id)
        return row.notification_hash if row else None


async def test_the_report_is_answered_only_once_its_alerts_are_evaluated(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch, agent_auth: None
) -> None:
    """The agent's next report waits for this one's answer, so this one's
    notifications are decided first: no answer goes out while a delivery is
    still in flight."""
    ids = await _seed(worker_db)
    hook = _Webhook(hold="y-service")
    hook.install(monkeypatch)
    answered = asyncio.Event()

    async def drive() -> None:
        body = json.dumps(
            _body(ids, [_wire(ids["y"], utc_now(), success=False)])
        ).encode()
        sent = False

        async def receive() -> dict[str, Any]:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.Event().wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                answered.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/agent/reports",
            "raw_path": b"/api/v1/agent/reports",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"test"),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"authorization", f"Bearer {ids['key']}".encode()),
            ],
            "client": ("127.0.0.1", 50000),
            "server": ("test", 80),
        }
        await app(scope, receive, send)

    task = asyncio.create_task(drive())
    try:
        await asyncio.wait_for(hook.holding.wait(), 10)
        await asyncio.sleep(0.5)
        answered_while_delivering = answered.is_set()
    finally:
        hook.release.set()
        await asyncio.wait_for(task, 10)
    assert not answered_while_delivering
    assert answered.is_set()
    assert hook.of("y-service") == ["error"]


async def test_a_resent_report_evaluates_results_stored_before_a_crash(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch, agent_auth: None
) -> None:
    """A server that stops between storing a report and evaluating its alerts
    never answers it; the agent sends it again, nothing new is stored, and
    the alerts are evaluated then."""
    ids = await _seed(worker_db)
    hook = _Webhook()
    hook.install(monkeypatch)
    at = utc_now() - timedelta(seconds=5)
    results = [_wire(ids["y"], at, success=False)]

    async with worker_db() as s:
        agent = await s.get(Agent, ids["agent"])
        assert agent is not None
        await CheckResultCoreService.process_agent_report(
            s,
            AgentReportRequest(
                agent_id=ids["agent"],
                checks=[CheckResultCreate.model_validate(r) for r in results],
            ),
            verified_key_hash=agent.api_key_hash,
        )
        await s.commit()
    assert hook.sent == []

    assert await _post(ids, results) == 202
    assert hook.of("y-service") == ["error"]
    # Sent a third time: already notified, nothing more.
    assert await _post(ids, results) == 202
    assert hook.of("y-service") == ["error"]


async def test_overlapping_evaluations_of_one_check_notify_its_outage_once(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch, agent_auth: None
) -> None:
    """Two reports for one check evaluated at once (a replicate check's
    agents): the second waits for the first's notification to commit, and
    sees it."""
    ids = await _seed(worker_db)
    hook = _Webhook(hold="y-service")
    hook.install(monkeypatch)
    t0 = utc_now() - timedelta(seconds=60)

    first = asyncio.create_task(_post(ids, [_wire(ids["y"], t0, success=False)]))
    await asyncio.wait_for(hook.holding.wait(), 10)
    second = asyncio.create_task(
        _post(ids, [_wire(ids["y"], t0 + timedelta(seconds=11), success=False)])
    )
    await asyncio.sleep(1)
    hook.release.set()
    statuses = await asyncio.wait_for(asyncio.gather(first, second), 20)

    assert statuses == [202, 202]
    assert hook.of("y-service") == ["error"], hook.sent


async def test_a_result_older_than_the_last_notified_one_sends_nothing(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A result evaluated after a newer one (another agent's report of a
    replicate check, delayed) never sends: a check that is up never ends
    with its alert down."""
    ids = await _seed(worker_db)
    hook = _Webhook()
    hook.install(monkeypatch)
    t0 = utc_now() - timedelta(seconds=60)

    async with worker_db() as s:
        down = make_check_result(
            check_id=ids["y"], agent_id=ids["agent"], timestamp=t0, success=False
        )
        late = make_check_result(
            check_id=ids["y"],
            agent_id=ids["agent"],
            timestamp=t0 + timedelta(seconds=5),
            success=False,
        )
        up = make_check_result(
            check_id=ids["y"],
            agent_id=ids["agent"],
            timestamp=t0 + timedelta(seconds=10),
            success=True,
        )
        order = [down.id, up.id, late.id]
        s.add_all([down, late, up])
        await s.commit()

    for result_id in order:
        async with worker_db() as s:
            result = await CheckResultCRUD.get_check_result_by_id(s, result_id)
            assert result is not None
            await AlertEvaluationCoreService.evaluate_check_result(s, result)
            await s.commit()

    assert hook.of("y-service") == ["error", "success"], hook.sent
    assert await _last_state(worker_db, ids["alert"], ids["y"]) == "status:up"


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
            _wire(ids["y"], t0, success=False),
            _wire(ids["y"], t0 + timedelta(seconds=5), success=False),
            _wire(ids["y"], t0 + timedelta(seconds=10), success=True),
        ],
    )

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

    assert await _post(ids, [_wire(ids["y"], t0, success=False)]) == 202
    assert (
        await _post(ids, [_wire(ids["y"], t0 + timedelta(seconds=10), success=True)])
        == 202
    )

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
    worker_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While a check's notification is delivered, the agent can still be
    locked and changed (pause, revoke) and the check renamed."""
    ids = await _seed(worker_db)
    hook = _Webhook(hold="z-service")
    hook.install(monkeypatch)
    t0 = utc_now() - timedelta(seconds=60)
    async with worker_db() as s:
        s.add_all(
            [
                make_check_result(
                    check_id=ids["y"],
                    agent_id=ids["agent"],
                    timestamp=t0,
                    success=False,
                ),
                make_check_result(
                    check_id=ids["z"],
                    agent_id=ids["agent"],
                    timestamp=t0 + timedelta(seconds=1),
                    success=False,
                ),
            ]
        )
        await s.commit()
    task = asyncio.create_task(
        CheckResultCoreService.evaluate_alerts(
            [(ids["y"], t0), (ids["z"], t0 + timedelta(seconds=1))]
        )
    )
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
            update(Check).where(Check.id == ids["z"]).values(display_name="z2"),
            6,
        )
    finally:
        hook.release.set()
        evaluated = await asyncio.wait_for(task, 10)
    assert locked and changed and renamed
    assert evaluated == 2
    assert hook.of("y-service") == ["error"]
