"""An agent request whose key is revoked, or whose agent is disabled, while it
is processed is refused.

Each agent-protocol dependency checks the agent's key and status (bcrypt, the
slow part) before the route's body runs, on a row it read unlocked. An
operator's revoke or disable that commits in between would go unseen by a
report, a job's start or results, an artifact upload or the check list (which
hands out decrypted check configs). Core is given the key hash the request
was checked against and, holding the agent's row (FOR KEY SHARE, which a
revoke and a status change wait for), reads the row as stored now
(`AgentCoreService.require_still_admitted`) before it writes or answers: a key
changed since is refused 401, an agent no longer active 403.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.exceptions import AuthenticationException, AuthorizationException
from app.core.rate_limit import limiter
from app.core.security import verify_agent_token
from app.main import app
from app.models.enum_model import JobStatus
from app.notifications.providers.webhook import WebhookNotificationProvider
from app.schemas.check_artifact_schema import CheckArtifactCreate
from app.schemas.check_result_schema import AgentReportSubmission
from app.schemas.job_schema import JobCreate, JobResultSubmit
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.artifact_core_service import ArtifactCoreService
from app.services.core.check_api_core_service import CheckApiCoreService
from app.services.core.check_result_core_service import CheckResultCoreService
from app.services.core.job_api_core_service import JobApiCoreService
from app.services.core.job_core_service import JobCoreService
from fixtures.factories import (
    make_alert,
    make_alert_check_mapping,
    make_alert_notification_mapping,
    make_check,
    make_notification_provider,
)
from fixtures.outbox import deliver_committed
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


class _Seeded:
    def __init__(self, agent_id: UUID, key: str, check_id: UUID, job_id: UUID):
        self.agent_id = agent_id
        self.key = key
        self.check_id = check_id
        self.job_id = job_id


async def _seed(worker_db: Any, *, job_running: bool) -> _Seeded:
    async with worker_db() as s:
        agent, _secret = await AgentCoreService.register_agent(s, hostname="race.test")
        await s.commit()
    async with worker_db() as s:
        _, key = await AgentCoreService.approve_agent(s, agent.id, actor_is_admin=True)
        check = make_check(agent_id=agent.id)
        s.add(check)
        job = await JobCoreService.create_job(
            s,
            JobCreate(
                job_type="network_scan",
                agent_id=agent.id,
                params={"subnet": "10.0.0.0/30"},
            ),
        )
        await s.commit()
        check_id, job_id = check.id, job.id
    if job_running:
        async with worker_db() as s:
            await JobCoreService.update_job_status(s, job_id, JobStatus.RUNNING)
            await s.commit()
    return _Seeded(agent.id, key, check_id, job_id)


def _report(seeded: _Seeded) -> Callable[[AsyncSession, str | None], Awaitable[Any]]:
    async def call(db: AsyncSession, key_hash: str | None) -> Any:
        submission = AgentReportSubmission.model_validate(
            {
                "agent_id": str(seeded.agent_id),
                "checks": [
                    {
                        "timestamp": utc_now().isoformat(),
                        "success": True,
                        "latency_ms": 5.0,
                        "check_id": str(seeded.check_id),
                        "display_name": "probe",
                        "check_type": "ping",
                        "target": "192.0.2.10",
                    }
                ],
            }
        )
        result = await CheckResultCoreService.process_agent_submission(
            db, submission, verified_key_hash=key_hash
        )
        assert result["results_processed"] == 1
        return result

    return call


def _job_start(seeded: _Seeded) -> Callable[[AsyncSession, str | None], Awaitable[Any]]:
    async def call(db: AsyncSession, key_hash: str | None) -> Any:
        return await JobApiCoreService.mark_job_started(
            db, seeded.job_id, agent_id=seeded.agent_id, verified_key_hash=key_hash
        )

    return call


def _job_results(
    seeded: _Seeded,
) -> Callable[[AsyncSession, str | None], Awaitable[Any]]:
    async def call(db: AsyncSession, key_hash: str | None) -> Any:
        return await JobApiCoreService.submit_job_result(
            db,
            seeded.job_id,
            JobResultSubmit(status=JobStatus.COMPLETED, result={"hosts": []}),
            agent_id=seeded.agent_id,
            verified_key_hash=key_hash,
        )

    return call


def _artifact(seeded: _Seeded) -> Callable[[AsyncSession, str | None], Awaitable[Any]]:
    async def call(db: AsyncSession, key_hash: str | None) -> Any:
        return await ArtifactCoreService.create_artifact(
            db,
            seeded.agent_id,
            CheckArtifactCreate(
                agent_id=seeded.agent_id,
                check_id=seeded.check_id,
                check_result_id=uuid4(),
                check_result_timestamp=utc_now(),
                artifact_type="screenshot",
                content_type="image/png",
                filename="shot.png",
                data_base64=base64.b64encode(b"png-bytes").decode(),
            ),
            verified_key_hash=key_hash,
        )

    return call


def _check_list(
    seeded: _Seeded,
) -> Callable[[AsyncSession, str | None], Awaitable[Any]]:
    async def call(db: AsyncSession, key_hash: str | None) -> Any:
        return await CheckApiCoreService.check_list_for_agent(
            db, seeded.agent_id, verified_key_hash=key_hash
        )

    return call


_PATHS = {
    "report": (_report, False),
    "job-start": (_job_start, False),
    "job-results": (_job_results, True),
    "artifact": (_artifact, False),
    "check-list": (_check_list, False),
}


async def _request_after_operator_acts(worker_db: Any, path: str, *, act: str) -> None:
    make_call, job_running = _PATHS[path]
    seeded = await _seed(worker_db, job_running=job_running)
    call = make_call(seeded)
    async with worker_db() as req:
        # What the route's dependency does before the body: the key check.
        row = await AgentCoreService.get_agent_by_id(req, seeded.agent_id)
        checked_hash = row.api_key_hash
        await verify_agent_token(row, f"Bearer {seeded.key}")
        if act != "none":
            async with worker_db() as operator:
                if act == "revoke":
                    await AgentCoreService.revoke_agent_key(operator, seeded.agent_id)
                elif act == "regenerate":
                    await AgentCoreService.regenerate_agent_key(
                        operator, seeded.agent_id
                    )
                else:
                    await AgentCoreService.disable_agent(
                        operator, seeded.agent_id, reason="compromised"
                    )
                await operator.commit()
        try:
            await call(req, checked_hash)
        finally:
            await req.rollback()


@pytest.mark.parametrize("path", list(_PATHS))
@pytest.mark.parametrize("act", ["revoke", "regenerate"])
async def test_a_request_whose_key_changed_meanwhile_is_refused(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch, path: str, act: str
) -> None:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    with pytest.raises(AuthenticationException):
        await _request_after_operator_acts(worker_db, path, act=act)


@pytest.mark.parametrize("path", list(_PATHS))
async def test_a_request_whose_agent_is_disabled_meanwhile_is_refused(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    with pytest.raises(AuthorizationException):
        await _request_after_operator_acts(worker_db, path, act="disable")


@pytest.mark.parametrize("path", list(_PATHS))
async def test_control_a_request_with_nothing_changed_is_served(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    await _request_after_operator_acts(worker_db, path, act="none")


# The re-read holds the agent's row FOR KEY SHARE to the request's commit. A
# revoke or regenerate locks the row (FOR UPDATE), so it waits for the request;
# a transition out of active must wait too, or it could commit between the
# re-read and the request's commit and go unseen.
async def _transition_waits_for_a_request_in_flight(
    worker_db: Any, transition: str
) -> bool:
    seeded = await _seed(worker_db, job_running=False)
    async with worker_db() as req:
        await req.execute(
            text("SELECT id FROM agents WHERE id = :id FOR KEY SHARE"),
            {"id": seeded.agent_id},
        )
        try:
            async with worker_db() as operator:
                await operator.execute(text("SET LOCAL lock_timeout = '500ms'"))
                await getattr(AgentCoreService, transition)(operator, seeded.agent_id)
                await operator.commit()
        except DBAPIError:
            return True
        finally:
            await req.rollback()
    return False


@pytest.mark.parametrize(
    "transition", ["pause_agent", "disable_agent", "archive_agent"]
)
async def test_a_transition_out_of_active_waits_for_a_request_in_flight(
    worker_db: Any, transition: str
) -> None:
    assert await _transition_waits_for_a_request_in_flight(worker_db, transition)


# A report decides its alerts in its own transaction and only queues their
# notifications; the delivery worker sends them after it commits, holding no
# lock on the agent or its checks (a webhook may take its whole 10 s timeout).
# An operator who stops the agent while its report's notification is being
# delivered never waits behind the delivery, nor runs into the web pool's
# statement timeout.
@pytest.mark.parametrize(
    "transition",
    [
        "pause_agent",
        "disable_agent",
        "archive_agent",
        "revoke_agent_key",
        "regenerate_agent_key",
    ],
)
async def test_an_operator_can_stop_an_agent_whose_report_is_delivering_alerts(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch, transition: str
) -> None:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    limiter.reset()
    seeded = await _seed(worker_db, job_running=False)
    async with worker_db() as s:
        provider = make_notification_provider()
        alert = make_alert()
        s.add_all([provider, alert])
        await s.flush()
        s.add_all(
            [
                make_alert_check_mapping(alert_id=alert.id, check_id=seeded.check_id),
                make_alert_notification_mapping(
                    alert_id=alert.id, notification_provider_id=provider.id
                ),
            ]
        )
        await s.commit()
    delivering, stopped = asyncio.Event(), asyncio.Event()

    async def delivering_until_stopped(self: Any, context: Any) -> bool:
        delivering.set()
        await asyncio.wait_for(stopped.wait(), 10)
        return True

    monkeypatch.setattr(WebhookNotificationProvider, "send", delivering_until_stopped)

    submission = {
        "agent_id": str(seeded.agent_id),
        "checks": [
            {
                "timestamp": utc_now().isoformat(),
                "success": False,
                "error": "down",
                "latency_ms": 5.0,
                "check_id": str(seeded.check_id),
                "display_name": "probe",
                "check_type": "ping",
                "target": "192.0.2.10",
            }
        ],
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        report = await client.post(
            "/api/v1/agent/reports",
            json=submission,
            headers={"Authorization": f"Bearer {seeded.key}"},
        )
    # Answered before anything was delivered.
    assert report.status_code == 202, report.text
    assert not delivering.is_set()

    async def operator() -> None:
        await asyncio.wait_for(delivering.wait(), 10)
        try:
            async with worker_db() as op:
                await asyncio.wait_for(
                    getattr(AgentCoreService, transition)(op, seeded.agent_id), 5
                )
                await op.commit()
        finally:
            stopped.set()

    delivered, stop = await asyncio.gather(
        deliver_committed(), operator(), return_exceptions=True
    )

    assert not isinstance(stop, BaseException), f"{stop!r:.400}"
    assert delivered == 1, repr(delivered)
