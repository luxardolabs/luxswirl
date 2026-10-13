"""An agent's first API key is claimed once, in the statement that checks it.

`generate_agent_key` read the agent, checked `api_key_hash` was empty, then wrote a
new hash. Two concurrent requests both passed the check and both wrote, so the admin
who saw the first plaintext key held a key that no longer matched. The claim is now
one UPDATE whose WHERE only matches an agent with no key. FLEET-AUTH-SESSION-STANDARD
§1.2 (`fw.single_use_claim_atomic`).
"""

from __future__ import annotations

import pytest
from app.models.agent_model import Agent
from app.services.core.agent_core_service import AgentCoreService
from fixtures.factories import make_agent
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def test_first_key_is_issued_and_stored(db: AsyncSession) -> None:
    agent = make_agent(api_key_hash=None)
    db.add(agent)
    await db.flush()

    _, key = await AgentCoreService.generate_agent_key(db, agent.id)

    stored = await db.scalar(select(Agent.api_key_hash).where(Agent.id == agent.id))
    assert key.startswith("luxswirl_ak_")
    assert stored


async def test_a_key_written_by_a_concurrent_request_is_not_overwritten(
    db: AsyncSession,
) -> None:
    agent = make_agent(api_key_hash=None)
    db.add(agent)
    await db.flush()
    # This request has the agent loaded with no key. Another request then issues
    # one, which this session's loaded copy does not see.
    await db.execute(
        update(Agent)
        .where(Agent.id == agent.id)
        .values(api_key_hash="hash-from-the-other-request")
        .execution_options(synchronize_session=False)
    )

    with pytest.raises(ValueError, match="already has an API key"):
        await AgentCoreService.generate_agent_key(db, agent.id)

    stored = await db.scalar(
        select(Agent.api_key_hash)
        .where(Agent.id == agent.id)
        .execution_options(populate_existing=True)
    )
    assert stored == "hash-from-the-other-request"
