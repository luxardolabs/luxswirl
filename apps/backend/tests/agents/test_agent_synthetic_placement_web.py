"""The web routes hold the synthetic-placement rule, readably, and under races.

An editor's agent edit, approval or delete that would move or remove a
synthetic check is refused with a message (never a 500); an admin's goes
through; an editor's ordinary change still works; and a pending agent's
heartbeat cannot slip new tags in while an editor's approval is judging them.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from app.core.config import settings
from app.main import app
from app.models.agent_model import Agent
from app.models.maintenance_job_model import MaintenanceJob
from app.schemas.agent_schema import AgentHeartbeat
from app.services.core.agent_assignment_core_service import AgentAssignmentCoreService
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.auth_core_service import AuthCoreService
from fixtures.factories import make_agent, make_check, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

SELECTOR = {"tags": ["role:browser"]}
HX = {"HX-Request": "true"}


async def _client(db: AsyncSession, role: str) -> AsyncClient:
    user = make_user(role=role)
    db.add(user)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, user.id)
    c = AsyncClient(transport=ASGITransport(app=app), base_url="https://test")
    c.cookies.set(settings.security.session_cookie_name, token)
    return c


@pytest_asyncio.fixture
async def editor(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    c = await _client(db, "editor")
    async with c:
        yield c


@pytest_asyncio.fixture
async def admin(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    c = await _client(db, "admin")
    async with c:
        yield c


def _synthetic(owner_id):
    return make_check(
        agent_id=owner_id,
        check_type="synthetic",
        target="https://a.example",
        script_code="await page.goto(target)",
        assignment_mode="replicate",
        agent_selector=SELECTOR,
    )


async def _seed(db: AsyncSession) -> tuple[Agent, Agent]:
    owner = make_agent(tags=["role:browser"])
    other = make_agent(tags=["role:db"])
    db.add_all([owner, other])
    await db.flush()
    db.add(_synthetic(owner.id))
    await db.flush()
    return owner, other


async def _fresh(db: AsyncSession, agent_id) -> Agent:
    await db.flush()
    db.expire_all()
    return await db.scalar(select(Agent).where(Agent.id == agent_id))


async def test_editor_ordinary_edit_on_agent_in_reach_passes(db, editor) -> None:
    owner, _ = await _seed(db)
    resp = await editor.patch(
        f"/agents/{owner.id}",
        data={"agent_name": owner.agent_name, "hostname": "h2", "tags": "role:browser"},
        headers=HX,
    )
    assert resp.status_code == 200, resp.text


async def test_editor_retag_into_reach_is_refused_readably_and_rolled_back(
    db, editor
) -> None:
    _, other = await _seed(db)
    resp = await editor.patch(
        f"/agents/{other.id}",
        data={"agent_name": other.agent_name, "tags": "role:browser"},
        headers=HX,
    )
    assert resp.status_code == 403, resp.text
    assert "admin" in resp.headers.get("HX-Trigger", "").lower(), resp.headers
    assert (await _fresh(db, other.id)).tags == ["role:db"]


async def test_admin_retag_into_reach_passes(db, admin) -> None:
    _, other = await _seed(db)
    resp = await admin.patch(
        f"/agents/{other.id}",
        data={"agent_name": other.agent_name, "tags": "role:browser"},
        headers=HX,
    )
    assert resp.status_code == 200, resp.text
    assert (await _fresh(db, other.id)).tags == ["role:browser"]


async def test_editor_approve_matching_refused_readably(db, editor) -> None:
    await _seed(db)
    pending = make_agent(approval_status="pending", tags=["role:browser"])
    db.add(pending)
    await db.flush()
    resp = await editor.post(f"/agents/{pending.id}/approve-web", headers=HX)
    assert resp.status_code == 403, resp.text
    assert "admin" in resp.headers.get("HX-Trigger", "").lower(), resp.headers
    assert (await _fresh(db, pending.id)).approval_status == "pending"


async def test_editor_approve_non_matching_passes(db, editor) -> None:
    await _seed(db)
    pending = make_agent(approval_status="pending", tags=["role:db"])
    db.add(pending)
    await db.flush()
    resp = await editor.post(f"/agents/{pending.id}/approve-web", headers=HX)
    assert resp.status_code == 200, resp.text


async def test_admin_approve_matching_passes(db, admin) -> None:
    await _seed(db)
    pending = make_agent(approval_status="pending", tags=["role:browser"])
    db.add(pending)
    await db.flush()
    resp = await admin.post(f"/agents/{pending.id}/approve-web", headers=HX)
    assert resp.status_code == 200, resp.text


async def test_editor_delete_owner_refused_readably_not_500(db, editor) -> None:
    owner, _ = await _seed(db)
    resp = await editor.request(
        "DELETE",
        f"/agents/{owner.id}",
        data={"confirm_name": owner.agent_name},
        headers=HX,
    )
    jobs = (
        (
            await db.execute(
                select(MaintenanceJob).where(MaintenanceJob.target_id == owner.id)
            )
        )
        .scalars()
        .all()
    )
    assert jobs == []
    assert resp.status_code == 403, (
        resp.status_code,
        dict(resp.headers),
        resp.text[:300],
    )


async def test_editor_delete_non_owner_passes(db, editor) -> None:
    _, other = await _seed(db)
    resp = await editor.request(
        "DELETE",
        f"/agents/{other.id}",
        data={"confirm_name": other.agent_name},
        headers=HX,
    )
    assert resp.status_code == 200, resp.text


async def test_admin_delete_owner_passes(db, admin) -> None:
    owner, _ = await _seed(db)
    resp = await admin.request(
        "DELETE",
        f"/agents/{owner.id}",
        data={"confirm_name": owner.agent_name},
        headers=HX,
    )
    assert resp.status_code == 200, resp.text


async def test_pending_heartbeat_tags_landing_during_approval(
    worker_db, monkeypatch
) -> None:
    """An editor approves a pending agent no synthetic check reaches while the
    agent's heartbeat suggests tags that one would: the two never interleave."""
    maker = worker_db
    async with maker() as s:
        owner = make_agent(tags=["role:browser"])
        pending = make_agent(approval_status="pending", tags=["role:db"])
        s.add_all([owner, pending])
        await s.flush()
        s.add(_synthetic(owner.id))
        await s.commit()
        pending_id = pending.id

    real = AgentAssignmentCoreService.synthetic_checks_reaching
    judged = asyncio.Event()

    async def judged_then_pause(db, agent):
        result = await real(db, agent)
        judged.set()
        await asyncio.sleep(0.5)  # the window a heartbeat would land in
        return result

    monkeypatch.setattr(
        AgentAssignmentCoreService,
        "synthetic_checks_reaching",
        staticmethod(judged_then_pause),
    )

    async def approve() -> None:
        async with maker() as s:
            await AgentCoreService.approve_agent(s, pending_id, actor_is_admin=False)
            await s.commit()

    async def heartbeat() -> None:
        await judged.wait()
        async with maker() as hb:
            await AgentCoreService.process_heartbeat(
                hb,
                pending_id,
                AgentHeartbeat(
                    agent_id=pending_id,
                    timestamp=datetime.now(UTC),
                    status="online",
                    tags=["role:browser"],
                ),
                using_agent_key=False,
            )
            await hb.commit()

    await asyncio.wait_for(asyncio.gather(approve(), heartbeat()), timeout=20)
    monkeypatch.setattr(
        AgentAssignmentCoreService, "synthetic_checks_reaching", staticmethod(real)
    )

    async with maker() as s:
        agent = await s.scalar(select(Agent).where(Agent.id == pending_id))
        reached = await real(s, agent)
        assert not (agent.approval_status == "active" and reached), (
            f"editor-approved agent is active with tags {agent.tags} reaching {reached}"
        )
