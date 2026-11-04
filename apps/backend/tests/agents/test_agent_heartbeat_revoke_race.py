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
from app.core.security import authenticate_heartbeat
from app.schemas.agent_schema import AgentHeartbeat
from app.services.core.agent_core_service import AgentCoreService

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
