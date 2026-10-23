"""Deleting an agent from its panel: the confirmation states what is removed,
and the delete only queues when the agent's name is typed."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.main import app
from app.models.maintenance_job_model import MaintenanceJob
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.check_result_core_service import CheckResultCoreService
from fixtures.factories import make_agent, make_check, make_check_result, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def client(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    admin = make_user(role="admin")
    db.add(admin)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, admin.id)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as c:
        c.cookies.set(settings.security.session_cookie_name, token)
        yield c


async def _jobs(db: AsyncSession, agent_id) -> list[MaintenanceJob]:
    result = await db.execute(
        select(MaintenanceJob).where(MaintenanceJob.target_id == agent_id)
    )
    return list(result.scalars().all())


async def test_the_count_is_what_the_cascade_removes(db: AsyncSession) -> None:
    doomed = make_agent()
    other = make_agent()
    db.add_all([doomed, other])
    await db.flush()
    owned = make_check(agent_id=doomed.id)
    elsewhere = make_check(agent_id=other.id)
    db.add_all([owned, elsewhere])
    await db.flush()
    now = utc_now()
    db.add_all(
        [
            # Its own check's results, whoever reported them.
            make_check_result(check_id=owned.id, agent_id=doomed.id, timestamp=now),
            make_check_result(
                check_id=owned.id,
                agent_id=other.id,
                timestamp=now - timedelta(seconds=1),
            ),
            # A result it reported for another agent's check.
            make_check_result(check_id=elsewhere.id, agent_id=doomed.id, timestamp=now),
            # Kept: another agent's result for another agent's check.
            make_check_result(
                check_id=elsewhere.id,
                agent_id=other.id,
                timestamp=now - timedelta(seconds=2),
            ),
            # Past retention (90 days by default): already gone, not counted.
            make_check_result(
                check_id=owned.id,
                agent_id=doomed.id,
                timestamp=now - timedelta(days=200),
            ),
        ]
    )
    await db.flush()

    assert await CheckResultCoreService.count_deleted_with_agent(db, doomed.id) == 3


async def test_the_confirmation_states_what_is_removed(
    client: AsyncClient, db: AsyncSession
) -> None:
    agent = make_agent(agent_name="edge-07")
    db.add(agent)
    await db.flush()
    check = make_check(agent_id=agent.id)
    db.add(check)
    await db.flush()
    db.add(make_check_result(check_id=check.id, agent_id=agent.id, timestamp=utc_now()))
    await db.flush()

    resp = await client.get(f"/agents/{agent.id}/delete-impact")

    assert resp.status_code == 200, resp.text
    assert "1 check" in resp.text and "1 stored result" in resp.text


@pytest.mark.parametrize("typed", ["", "edge", "EDGE-07", "edge-07x"])
async def test_a_wrong_name_queues_nothing(
    client: AsyncClient, db: AsyncSession, typed: str
) -> None:
    agent = make_agent(agent_name="edge-07")
    db.add(agent)
    await db.flush()

    resp = await client.request(
        "DELETE", f"/agents/{agent.id}", data={"confirm_name": typed}
    )

    assert resp.status_code == 422
    assert resp.headers["HX-Retarget"] == f"#agent-delete-error-{agent.id}"
    assert "edge-07" in resp.text
    assert await _jobs(db, agent.id) == []


async def test_the_typed_name_queues_the_delete(
    client: AsyncClient, db: AsyncSession
) -> None:
    agent = make_agent(agent_name="edge-07")
    db.add(agent)
    await db.flush()

    resp = await client.request(
        "DELETE", f"/agents/{agent.id}", data={"confirm_name": " edge-07 "}
    )

    assert resp.status_code == 200, resp.text
    assert len(await _jobs(db, agent.id)) == 1


async def test_an_unnamed_agent_confirms_with_its_hostname(
    client: AsyncClient, db: AsyncSession
) -> None:
    agent = make_agent(agent_name=None, hostname="web01.example.test")
    db.add(agent)
    await db.flush()

    resp = await client.request(
        "DELETE", f"/agents/{agent.id}", data={"confirm_name": "web01.example.test"}
    )

    assert resp.status_code == 200, resp.text
    assert len(await _jobs(db, agent.id)) == 1


async def test_the_panel_asks_for_the_name_the_server_checks(
    client: AsyncClient, db: AsyncSession
) -> None:
    agent = make_agent(agent_name=None, hostname="web01.example.test")
    db.add(agent)
    await db.flush()

    resp = await client.get(f"/agents/{agent.id}/edit-form")

    assert resp.status_code == 200, resp.text
    assert 'name="confirm_name"' in resp.text
    assert "<code" in resp.text and "web01.example.test</code>" in resp.text
    assert f"/agents/{agent.id}/delete-impact" in resp.text


async def test_a_viewer_is_not_offered_the_delete(db: AsyncSession) -> None:
    viewer = make_user(role="viewer")
    agent = make_agent(agent_name="edge-07")
    db.add_all([viewer, agent])
    await db.flush()
    _, token = await AuthCoreService().create_session(db, viewer.id)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as c:
        c.cookies.set(settings.security.session_cookie_name, token)
        panel = await c.get(f"/agents/{agent.id}/edit-form")
        refused = await c.request(
            "DELETE", f"/agents/{agent.id}", data={"confirm_name": "edge-07"}
        )

    assert panel.status_code == 200, panel.text
    assert 'name="confirm_name"' not in panel.text
    assert refused.status_code == 307
    assert await _jobs(db, agent.id) == []
