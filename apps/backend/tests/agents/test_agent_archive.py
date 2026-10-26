"""Archiving an agent puts it out of use without deleting anything.

An archived agent is refused by the agent API with its own key, so it gets no
checks and reports nothing; no check is assigned to it, so no alert covers it;
it is left out of the agent lists and pickers, and a check pinned to it leaves
the dashboard. The agent, its checks and their results are kept, and
restoring it makes it disabled, so it runs nothing until it is enabled.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.exceptions import ValidationException
from app.core.rate_limit import limiter
from app.crud.check_result_crud import CheckResultCRUD
from app.main import app
from app.models.agent_model import Agent
from app.models.check_model import Check
from app.models.check_result_model import CheckResult
from app.services.core.agent_assignment_core_service import AgentAssignmentCoreService
from app.services.core.agent_core_service import AgentCoreService
from fixtures.factories import make_agent, make_check, make_check_result
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def client(db: AsyncSession, monkeypatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    limiter.reset()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _agent(db: AsyncSession, **overrides: Any) -> Agent:
    agent = make_agent(**overrides)
    db.add(agent)
    await db.flush()
    return agent


async def _check(db: AsyncSession, agent_id: UUID, **overrides: Any) -> Check:
    check = make_check(agent_id=agent_id, **overrides)
    db.add(check)
    await db.flush()
    return check


async def _approved_with_check(db: AsyncSession) -> tuple[Agent, str, Check]:
    agent = await _agent(db, approval_status="pending")
    check = await _check(db, agent.id)
    _, key = await AgentCoreService.approve_agent(db, agent.id, actor_is_admin=True)
    return agent, key, check


async def _status(db: AsyncSession, agent_id: UUID) -> str:
    db.expire_all()
    agent = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one()
    return agent.approval_status


# --- archive and restore ------------------------------------------------------


@pytest.mark.parametrize("status", ["active", "paused", "disabled"])
async def test_an_admitted_agent_is_archived(db: AsyncSession, status: str) -> None:
    agent = await _agent(db, approval_status=status, status_reason="old reason")

    await AgentCoreService.archive_agent(db, agent.id)

    assert await _status(db, agent.id) == "archived"
    assert agent.status_reason is None


@pytest.mark.parametrize("status", ["pending", "rejected", "archived"])
async def test_only_an_admitted_agent_can_be_archived(
    db: AsyncSession, status: str
) -> None:
    agent = await _agent(db, approval_status=status)

    with pytest.raises(ValidationException, match="can be archived"):
        await AgentCoreService.archive_agent(db, agent.id)

    assert await _status(db, agent.id) == status


async def test_archiving_keeps_the_agent_its_checks_and_their_results(
    db: AsyncSession,
) -> None:
    agent = await _agent(db)
    check = await _check(db, agent.id)
    db.add(make_check_result(check_id=check.id, agent_id=agent.id))
    await db.flush()

    check_id = check.id
    await AgentCoreService.archive_agent(db, agent.id)

    db.expire_all()
    assert (await db.execute(select(Check).where(Check.id == check_id))).scalar_one()
    results = await db.execute(
        select(CheckResult).where(CheckResult.check_id == check_id)
    )
    assert len(results.scalars().all()) == 1


async def test_a_restored_agent_is_disabled(db: AsyncSession) -> None:
    agent = await _agent(db, approval_status="archived")

    await AgentCoreService.restore_agent(db, agent.id)

    assert await _status(db, agent.id) == "disabled"


@pytest.mark.parametrize("status", ["pending", "active", "paused", "disabled"])
async def test_only_an_archived_agent_can_be_restored(
    db: AsyncSession, status: str
) -> None:
    agent = await _agent(db, approval_status=status)

    with pytest.raises(ValidationException, match="can be restored"):
        await AgentCoreService.restore_agent(db, agent.id)

    assert await _status(db, agent.id) == status


# --- the agent API refuses it -------------------------------------------------


async def test_an_archived_agent_gets_no_checks_with_its_own_key(
    client: AsyncClient, db: AsyncSession
) -> None:
    agent, key, _ = await _approved_with_check(db)
    await AgentCoreService.archive_agent(db, agent.id)

    resp = await client.get(
        "/api/v1/agent/checks",
        params={"agent_id": str(agent.id)},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 403, resp.text
    assert "archived" in resp.text


async def test_an_archived_agent_heartbeat_is_refused(
    client: AsyncClient, db: AsyncSession
) -> None:
    agent, key, _ = await _approved_with_check(db)
    await AgentCoreService.archive_agent(db, agent.id)

    resp = await client.post(
        "/api/v1/agent/heartbeat",
        json={
            "agent_id": str(agent.id),
            "timestamp": utc_now().isoformat(),
            "status": "online",
        },
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 403, resp.text


async def test_an_archived_agent_cannot_report(
    client: AsyncClient, db: AsyncSession
) -> None:
    agent, key, check = await _approved_with_check(db)
    await AgentCoreService.archive_agent(db, agent.id)

    resp = await client.post(
        "/api/v1/agent/reports",
        json={
            "agent_id": str(agent.id),
            "checks": [
                {
                    "check_id": str(check.id),
                    "display_name": check.display_name,
                    "check_type": "http",
                    "target": "https://example.test",
                    "success": True,
                    "latency_ms": 5.0,
                    "timestamp": utc_now().isoformat(),
                }
            ],
        },
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 403, resp.text


async def test_a_restored_agent_is_still_refused_until_enabled(
    client: AsyncClient, db: AsyncSession
) -> None:
    agent, key, _ = await _approved_with_check(db)
    await AgentCoreService.archive_agent(db, agent.id)
    await AgentCoreService.restore_agent(db, agent.id)

    refused = await client.get(
        "/api/v1/agent/checks",
        params={"agent_id": str(agent.id)},
        headers={"Authorization": f"Bearer {key}"},
    )
    await AgentCoreService.enable_agent(db, agent.id)
    served = await client.get(
        "/api/v1/agent/checks",
        params={"agent_id": str(agent.id)},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert refused.status_code == 403, refused.text
    assert served.status_code == 200, served.text


# --- nothing is assigned to it, so no alert covers it ------------------------


async def test_no_check_is_assigned_to_an_archived_agent(db: AsyncSession) -> None:
    agent = await _agent(db, tags=["edge"])
    pinned = await _check(db, agent.id)
    replicated = await _check(
        db, agent.id, assignment_mode="replicate", agent_selector={"tags": ["edge"]}
    )
    await AgentCoreService.archive_agent(db, agent.id)

    assigned = {
        check.id: (agents, pool)
        for check, agents, pool in await AgentAssignmentCoreService.assigned_agents_by_check(
            db
        )
    }

    assert assigned[pinned.id] == ([], [])
    assert agent.id not in {a.id for a in assigned[replicated.id][1]}


# --- lists and the dashboard leave it out ------------------------------------


async def test_the_agent_lists_leave_out_archived_agents(db: AsyncSession) -> None:
    kept = await _agent(db, approval_status="disabled")
    archived = await _agent(db, approval_status="archived")

    listed, _ = await AgentCoreService.list_agents(db, limit=1000, exclude_pending=True)
    only_archived, total = await AgentCoreService.list_agents(
        db, limit=1000, exclude_pending=True, archived=True
    )
    admitted = await AgentCoreService.get_admitted_agents(db)
    options = await AgentCoreService.list_agent_options(db)

    assert kept.id in {a.id for a in listed}
    assert archived.id not in {a.id for a in listed}
    assert [a.id for a in only_archived] == [archived.id] and total == 1
    assert archived.id not in {a.id for a in admitted}
    assert kept.id in {a.id for a in admitted}
    assert archived.id not in {o.id for o in options}


async def test_the_dashboard_leaves_out_checks_pinned_to_an_archived_agent(
    db: AsyncSession,
) -> None:
    archived = await _agent(db, approval_status="archived")
    disabled = await _agent(db, approval_status="disabled")
    pinned = await _check(db, archived.id)
    replicated = await _check(db, archived.id, assignment_mode="replicate")
    on_disabled = await _check(db, disabled.id)

    rows, total = await CheckResultCRUD.get_checks_with_agents_filtered(
        db, check_ids=[pinned.id, replicated.id, on_disabled.id]
    )

    assert {check.id for check, _ in rows} == {replicated.id, on_disabled.id}
    assert total == 2
