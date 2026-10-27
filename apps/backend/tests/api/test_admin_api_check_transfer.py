"""An agent's checks on the admin API: exported and imported by an admin, made in
bulk by an editor.

Import and bulk create queue a maintenance job after judging what the request
can (the agent, the alerts, the size); each request is its own job.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app
from app.models.enum_model import MaintenanceJobKind
from app.models.maintenance_job_model import MaintenanceJob
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.api_token_core_service import ApiTokenCoreService
from fixtures.factories import make_agent, make_alert, make_check, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

BULK_CREATE = "/api/v1/admin/checks/bulk/create"
ONE_CHECK = [{"name": "web", "check_type": "http", "target": "https://x.test"}]


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    limiter.reset()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    ) as c:
        yield c
    limiter.reset()


async def _as(db: AsyncSession, role: str) -> dict[str, str]:
    user = make_user(role=role)
    db.add(user)
    await db.flush()
    _, plaintext = await ApiTokenCoreService.create_token(
        db, user.id, ApiTokenCreate(name=role)
    )
    return {"Authorization": f"Bearer {plaintext}"}


async def _agent(db: AsyncSession, **fields: object) -> UUID:
    agent = make_agent(**fields)
    db.add(agent)
    await db.flush()
    return agent.id


def _export(agent_id: UUID) -> str:
    return f"/api/v1/admin/agents/{agent_id}/checks/export"


def _import(agent_id: UUID) -> str:
    return f"/api/v1/admin/agents/{agent_id}/checks/import"


async def _jobs(db: AsyncSession, kind: MaintenanceJobKind) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(MaintenanceJob)
            .where(MaintenanceJob.kind == kind)
        )
        or 0
    )


# ── Export ──────────────────────────────────────────────────────────────────


async def test_an_admin_exports_the_agent_s_checks(
    db: AsyncSession, client: AsyncClient
) -> None:
    agent_id = await _agent(db, hostname="lab-01")
    db.add(
        make_check(
            agent_id=agent_id,
            display_name="db",
            check_type="http",
            target="https://u:pw@x.test",
            retry_attempts=4,
        )
    )
    await db.flush()

    resp = await client.get(_export(agent_id), headers=await _as(db, "admin"))

    assert resp.status_code == 200, resp.text
    doc = resp.json()
    assert doc["agent_id"] == str(agent_id)
    assert doc["agent_hostname"] == "lab-01"
    assert doc["total_checks"] == 1
    (check,) = doc["checks"]
    assert (check["name"], check["target"], check["retry_attempts"]) == (
        "db",
        "https://u:pw@x.test",
        4,
    )


@pytest.mark.parametrize("role", ["viewer", "editor"])
async def test_only_an_admin_exports_or_imports(
    db: AsyncSession, client: AsyncClient, role: str
) -> None:
    token = await _as(db, role)
    agent_id = await _agent(db)

    assert (await client.get(_export(agent_id), headers=token)).status_code == 403
    resp = await client.post(
        _import(agent_id), json={"checks": ONE_CHECK}, headers=token
    )
    assert resp.status_code == 403
    assert await _jobs(db, MaintenanceJobKind.BULK_CHECK_IMPORT) == 0


async def test_an_unknown_agent_is_not_found(
    db: AsyncSession, client: AsyncClient
) -> None:
    admin = await _as(db, "admin")
    missing = uuid4()

    assert (await client.get(_export(missing), headers=admin)).status_code == 404
    resp = await client.post(
        _import(missing), json={"checks": ONE_CHECK}, headers=admin
    )
    assert resp.status_code == 404, resp.text
    resp = await client.post(
        BULK_CREATE,
        json={"agent_id": str(missing), "items": [{"url": "https://x.test"}]},
        headers=admin,
    )
    assert resp.status_code == 404, resp.text


# ── Import ──────────────────────────────────────────────────────────────────


async def test_an_import_is_queued_and_each_request_is_its_own_job(
    db: AsyncSession, client: AsyncClient
) -> None:
    admin = await _as(db, "admin")
    agent_id = await _agent(db)
    second = [{"name": "api", "check_type": "http", "target": "https://y.test"}]

    first = await client.post(
        _import(agent_id), json={"mode": "replace", "checks": ONE_CHECK}, headers=admin
    )
    again = await client.post(_import(agent_id), json={"checks": second}, headers=admin)

    assert first.status_code == 202, first.text
    assert again.status_code == 202, again.text
    assert first.json()["id"] != again.json()["id"]
    jobs = {
        j.id: j
        for j in await db.scalars(
            select(MaintenanceJob).where(
                MaintenanceJob.kind == MaintenanceJobKind.BULK_CHECK_IMPORT
            )
        )
    }
    stored = jobs[UUID(again.json()["id"])]
    assert stored.params["agent_id"] == str(agent_id)
    assert stored.params["mode"] == "merge"
    assert [c["name"] for c in stored.params["checks"]] == ["api"]
    assert jobs[UUID(first.json()["id"])].params["mode"] == "replace"


@pytest.mark.parametrize(
    "body",
    [
        {"checks": []},
        {"checks": ONE_CHECK, "mode": "overwrite"},
        {"checks": ONE_CHECK, "owner_id": str(uuid4())},
        {"checks": ONE_CHECK * 10_001},
        {"checks": ["web"]},
    ],
    ids=["empty", "unknown-mode", "extra-field", "too-many", "not-a-mapping"],
)
async def test_an_import_that_cannot_run_queues_nothing(
    db: AsyncSession, client: AsyncClient, body: dict
) -> None:
    admin = await _as(db, "admin")

    resp = await client.post(_import(await _agent(db)), json=body, headers=admin)

    assert resp.status_code == 422, resp.text
    assert await _jobs(db, MaintenanceJobKind.BULK_CHECK_IMPORT) == 0


# ── Bulk create ─────────────────────────────────────────────────────────────


async def test_an_editor_queues_a_bulk_create_and_each_is_its_own_job(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    agent_id = await _agent(db)
    alert = make_alert()
    db.add(alert)
    await db.flush()
    body = {
        "agent_id": str(agent_id),
        "items": [{"url": "https://one.test"}, {"url": "https://two.test"}],
        "alert_ids": [str(alert.id), str(alert.id)],
    }

    first = await client.post(BULK_CREATE, json=body, headers=editor)
    again = await client.post(
        BULK_CREATE,
        json={**body, "items": [{"url": "https://three.test"}]},
        headers=editor,
    )

    assert first.status_code == 202, first.text
    assert again.status_code == 202, again.text
    assert first.json()["id"] != again.json()["id"]
    job = await db.get(MaintenanceJob, UUID(first.json()["id"]))
    assert job is not None
    assert [r["url"] for r in job.params["requests"]] == [
        "https://one.test",
        "https://two.test",
    ]
    assert job.params["alert_ids"] == [str(alert.id)]


async def test_a_viewer_cannot_bulk_create(
    db: AsyncSession, client: AsyncClient
) -> None:
    resp = await client.post(
        BULK_CREATE,
        json={"agent_id": str(await _agent(db)), "items": [{"url": "https://x.test"}]},
        headers=await _as(db, "viewer"),
    )

    assert resp.status_code == 403
    assert await _jobs(db, MaintenanceJobKind.BULK_CHECK_CREATE) == 0


@pytest.mark.parametrize(
    ("agent", "body", "says"),
    [
        ({"approval_status": "pending"}, {}, "pending"),
        ({}, {"alert_ids": ["{missing}"]}, "No alert"),
        ({}, {"items": []}, "items"),
        ({}, {"items": [{"url": "https://x.test"}] * 10_001}, "items"),
        ({}, {"owner_id": "x"}, "owner_id"),
    ],
    ids=["unadmitted-agent", "unknown-alert", "no-items", "too-many", "extra-field"],
)
async def test_a_bulk_create_that_cannot_run_queues_nothing(
    db: AsyncSession, client: AsyncClient, agent: dict, body: dict, says: str
) -> None:
    editor = await _as(db, "editor")
    payload: dict = {
        "agent_id": str(await _agent(db, **agent)),
        "items": [{"url": "https://x.test"}],
        **body,
    }
    if payload.get("alert_ids") == ["{missing}"]:
        payload["alert_ids"] = [str(uuid4())]

    resp = await client.post(BULK_CREATE, json=payload, headers=editor)

    assert resp.status_code == 422, resp.text
    assert says in resp.text
    assert await _jobs(db, MaintenanceJobKind.BULK_CHECK_CREATE) == 0
