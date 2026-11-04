"""The check edit, clone and dependents panels render from CheckView DTOs."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.core.config import settings
from app.main import app
from app.models.check_model import Check
from app.models.maintenance_job_model import MaintenanceJob
from app.services.core.auth_core_service import AuthCoreService
from fixtures.factories import make_agent, make_check, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
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


async def test_saving_the_dependents_form_with_none_ticked_clears_them(
    client: AsyncClient, db: AsyncSession
) -> None:
    """A form with no box ticked sends no dependent_ids field at all; that reads
    as an empty list, not a refused request."""
    agent = make_agent()
    db.add(agent)
    await db.flush()
    parent = make_check(agent_id=agent.id)
    db.add(parent)
    await db.flush()
    child = make_check(agent_id=agent.id, depends_on_check_id=parent.id)
    db.add(child)
    await db.flush()

    resp = await client.post(f"/checks/{parent.id}/dependents", data={})

    assert resp.status_code == 200, resp.text[:300]
    await db.refresh(child)
    assert child.depends_on_check_id is None


async def test_edit_clone_and_dependents_panels(
    client: AsyncClient, db: AsyncSession
) -> None:
    agent = make_agent(agent_name="edge-a")
    db.add(agent)
    await db.flush()
    parent = make_check(agent_id=agent.id, display_name="gateway")
    child = make_check(agent_id=agent.id, display_name="api-behind-gateway")
    db.add_all([parent, child])
    await db.flush()

    paths = {
        "edit": f"/checks/{parent.id}/edit-form",
        "clone": f"/checks/{parent.id}/clone-form",
        "dependents": f"/checks/{parent.id}/dependents",
    }
    for name, path in paths.items():
        resp = await client.get(path)
        assert resp.status_code == 200, (name, resp.text[:300])
        assert "gateway" in resp.text, name


async def _check_on_archived_agent(db: AsyncSession) -> Check:
    owner = make_agent(agent_name="retired-edge", approval_status="archived")
    db.add_all([owner, make_agent(agent_name="live-edge")])
    await db.flush()
    check = make_check(agent_id=owner.id, display_name="old-gateway")
    db.add(check)
    await db.flush()
    return check


async def test_the_edit_form_shows_an_archived_owner_selected(
    client: AsyncClient, db: AsyncSession
) -> None:
    """A check whose agent is archived names that agent, chosen, in the
    agent picker, rather than falling back to "Select an agent..."."""
    check = await _check_on_archived_agent(db)

    resp = await client.get(f"/checks/{check.id}/edit-form")

    assert resp.status_code == 200, resp.text[:300]
    assert f'<option value="{check.agent_id}" selected>' in resp.text
    assert "retired-edge" in resp.text
    assert "(archived)" in resp.text


async def test_a_check_on_an_archived_agent_saves_without_moving(
    client: AsyncClient, db: AsyncSession
) -> None:
    check = await _check_on_archived_agent(db)
    owner_id = check.agent_id

    resp = await client.post(
        f"/checks/{check.id}/update",
        data={
            "agent_id": str(owner_id),
            "check_type": check.check_type,
            "display_name": "old-gateway",
            "target": check.target,
            "interval": "120",
        },
    )

    assert resp.status_code == 200, resp.text[:300]
    await db.refresh(check)
    assert check.agent_id == owner_id
    assert check.interval_seconds == 120


async def test_a_bulk_move_to_an_archived_agent_is_refused_before_queueing(
    client: AsyncClient, db: AsyncSession
) -> None:
    owner = make_agent()
    target = make_agent(approval_status="archived")
    db.add_all([owner, target])
    await db.flush()
    check = make_check(agent_id=owner.id)
    db.add(check)
    await db.flush()

    resp = await client.post(
        "/checks/bulk-modify",
        data={
            "select_all": "false",
            "check_ids": [str(check.id)],
            "agent_id": str(target.id),
        },
    )

    assert resp.status_code == 422, resp.text[:300]
    jobs = await db.scalar(select(func.count()).select_from(MaintenanceJob))
    assert jobs == 0
