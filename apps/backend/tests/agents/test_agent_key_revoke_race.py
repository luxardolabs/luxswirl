"""A revoke never loses to a key recovery in flight.

Recovery checks the agent's secret, then issues a key. A revoke that committed
between the two was written over: the row kept the new key, with the secret
cleared, and whoever presented the secret (a captured heartbeat) held a working
key after the operator's revoke succeeded. Both now lock the agent's row, so
the revoke waits for the recovery and then clears its key too.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import bcrypt
import pytest
from app.models.agent_model import Agent
from app.services.core.agent_core_service import AgentCoreService
from sqlalchemy import select

pytestmark = pytest.mark.integration


async def _approved(maker) -> tuple[UUID, str]:
    async with maker() as s:
        agent, secret = await AgentCoreService.register_agent(s, hostname="race.test")
        await s.commit()
    async with maker() as s:
        await AgentCoreService.approve_agent(s, agent.id, actor_is_admin=True)
        await s.commit()
    return agent.id, secret


async def _stored(maker, agent_id: UUID) -> Agent:
    async with maker() as s:
        agent = await s.scalar(select(Agent).where(Agent.id == agent_id))
        assert agent is not None
        return agent


async def test_a_revoke_during_a_key_recovery_leaves_no_working_key(
    worker_db, monkeypatch
) -> None:
    agent_id, secret = await _approved(worker_db)
    issue_key = AgentCoreService.regenerate_agent_key
    revoke: list[asyncio.Task[object]] = []

    async def revoke_on_the_side() -> None:
        async with worker_db() as operator:
            await AgentCoreService.revoke_agent_key(operator, agent_id)
            await operator.commit()

    async def operator_revokes_meanwhile(db, aid):
        # The secret has been checked; the operator revokes now, in a session
        # of their own. Give the revoke time to commit if nothing holds it.
        revoke.append(asyncio.create_task(revoke_on_the_side()))
        await asyncio.wait([revoke[0]], timeout=1.0)
        return await issue_key(db, aid)

    monkeypatch.setattr(
        AgentCoreService,
        "regenerate_agent_key",
        staticmethod(operator_revokes_meanwhile),
    )
    async with worker_db() as capturer:
        key = await AgentCoreService.recover_agent_key(capturer, agent_id, secret)
        await capturer.commit()
    await asyncio.wait_for(revoke[0], timeout=10.0)

    agent = await _stored(worker_db, agent_id)
    assert agent.recovery_secret_hash is None
    assert agent.api_key_hash is None or not bcrypt.checkpw(
        key.encode(), agent.api_key_hash.encode()
    ), "the key recovered with the secret works after the revoke committed"
