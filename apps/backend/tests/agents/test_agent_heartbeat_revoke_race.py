"""A heartbeat whose key is revoked while it is checked is not applied.

The heartbeat's key is checked (bcrypt, the slow part) before core locks and
reloads the agent's row. A revoke that committed in between used to go
unseen: the heartbeat was applied with the revoked key, answered
`agent_key_accepted: true`, and handed the agent's jobs. Core now holds the
key hash that was checked against the row it locked.
"""

from __future__ import annotations

import pytest
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.exceptions import (
    AuthorizationException,
    RecoverySecretRefusedException,
)
from app.core.security import authenticate_heartbeat
from app.models.agent_model import Agent
from app.schemas.agent_schema import AgentHeartbeat, AgentHeartbeatResponse
from app.schemas.job_schema import JobCreate
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.job_core_service import JobCoreService

pytestmark = pytest.mark.integration


async def _heartbeat(worker_db, *, revoke_meanwhile: bool) -> bool:
    async with worker_db() as s:
        agent, _secret = await AgentCoreService.register_agent(s, hostname="hb.test")
        await s.commit()
    async with worker_db() as s:
        _, key = await AgentCoreService.approve_agent(s, agent.id, actor_is_admin=True)
        await s.commit()

    async with worker_db() as hb:
        row = await AgentCoreService.get_agent_by_id(hb, agent.id)
        checked_hash = row.api_key_hash
        used_key = await authenticate_heartbeat(
            row, f"Bearer {key}", None, hb, "198.51.100.7"
        )
        assert used_key is True
        if revoke_meanwhile:
            async with worker_db() as operator:
                await AgentCoreService.revoke_agent_key(operator, agent.id)
                await operator.commit()
        resp = await AgentCoreService.process_heartbeat(
            hb,
            agent.id,
            AgentHeartbeat(agent_id=agent.id, timestamp=utc_now(), status="online"),
            using_agent_key=used_key,
            verified_key_hash=checked_hash,
            verified_secret_hash=None,
        )
        await hb.commit()
    return resp.agent_key_accepted


async def test_a_heartbeat_whose_key_is_revoked_meanwhile_is_refused(
    worker_db, monkeypatch
) -> None:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    assert await _heartbeat(worker_db, revoke_meanwhile=True) is False


async def test_control_a_heartbeat_with_its_key_unchanged_is_accepted(
    worker_db, monkeypatch
) -> None:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    assert await _heartbeat(worker_db, revoke_meanwhile=False) is True


# M1: the same race with a disable instead of a revoke. The key still matches,
# so the heartbeat was applied and handed the disabled agent its jobs. It is
# refused as the key check refuses a disabled agent's key now (403).
async def _heartbeat_disabled_meanwhile(
    worker_db, *, disable: bool
) -> AgentHeartbeatResponse:
    async with worker_db() as s:
        agent, _secret = await AgentCoreService.register_agent(s, hostname="m1.test")
        await s.commit()
    async with worker_db() as s:
        _, key = await AgentCoreService.approve_agent(s, agent.id, actor_is_admin=True)
        await JobCoreService.create_job(
            s,
            JobCreate(
                job_type="network_scan",
                agent_id=agent.id,
                params={"subnet": "10.0.0.0/30"},
            ),
        )
        await s.commit()
    async with worker_db() as hb:
        row = await AgentCoreService.get_agent_by_id(hb, agent.id)
        checked_hash = row.api_key_hash
        used_key = await authenticate_heartbeat(
            row, f"Bearer {key}", None, hb, "198.51.100.9"
        )
        assert used_key is True
        if disable:
            async with worker_db() as operator:
                await AgentCoreService.disable_agent(
                    operator, agent.id, reason="compromised"
                )
                await operator.commit()
        try:
            return await AgentCoreService.process_heartbeat(
                hb,
                agent.id,
                AgentHeartbeat(agent_id=agent.id, timestamp=utc_now(), status="online"),
                using_agent_key=used_key,
                verified_key_hash=checked_hash,
                verified_secret_hash=None,
            )
        finally:
            await hb.rollback()


async def test_m1_a_heartbeat_whose_agent_is_disabled_meanwhile_is_refused(
    worker_db, monkeypatch
) -> None:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    with pytest.raises(AuthorizationException):
        await _heartbeat_disabled_meanwhile(worker_db, disable=True)


async def test_m1_control_an_active_agent_gets_its_jobs(worker_db, monkeypatch) -> None:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    resp = await _heartbeat_disabled_meanwhile(worker_db, disable=False)
    assert resp.agent_key_accepted is True
    assert len(resp.jobs) == 1


# M2: a pending agent's heartbeat whose recovery secret was checked, with a
# revoke voiding the secret before core locks the row, was still applied. It
# is refused as the secret check refuses a voided secret now (marked 401).
async def _pending_heartbeat_revoked_meanwhile(worker_db, *, revoke: bool) -> Agent:
    async with worker_db() as s:
        agent, secret = await AgentCoreService.register_agent(s, hostname="m2.test")
        await s.commit()
    async with worker_db() as hb:
        row = await AgentCoreService.get_agent_by_id(hb, agent.id)
        checked_secret = row.recovery_secret_hash
        used_key = await authenticate_heartbeat(row, None, secret, hb, "198.51.100.10")
        assert used_key is False
        if revoke:
            async with worker_db() as operator:
                await AgentCoreService.revoke_agent_key(operator, agent.id)
                await operator.commit()
        await AgentCoreService.process_heartbeat(
            hb,
            agent.id,
            AgentHeartbeat(
                agent_id=agent.id, timestamp=utc_now(), status="online", tags=["m2"]
            ),
            using_agent_key=False,
            verified_key_hash=None,
            verified_secret_hash=checked_secret,
        )
        await hb.commit()
    async with worker_db() as s:
        return await AgentCoreService.get_agent_by_id(s, agent.id)


async def test_m2_a_heartbeat_whose_secret_is_voided_meanwhile_is_refused(
    worker_db, monkeypatch
) -> None:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    with pytest.raises(RecoverySecretRefusedException):
        await _pending_heartbeat_revoked_meanwhile(worker_db, revoke=True)


async def test_m2_control_a_live_secret_heartbeat_is_applied(
    worker_db, monkeypatch
) -> None:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    stored = await _pending_heartbeat_revoked_meanwhile(worker_db, revoke=False)
    assert stored.tags == ["m2"]
