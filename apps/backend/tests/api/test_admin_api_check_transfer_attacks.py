"""Check transfer on the admin API and the web form, against bad input.

A field a request does not take is refused, never dropped; a viewer polling an
import job reads no secret; an export keeps a 0 as 0; the web import form never
answers a bad file with a 500, and queues nothing for it.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from uuid import UUID

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app
from app.models.enum_model import MaintenanceJobKind
from app.models.maintenance_job_model import MaintenanceJob
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.api_token_core_service import ApiTokenCoreService
from fixtures.factories import make_agent, make_check, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

BULK_CREATE = "/api/v1/admin/checks/bulk/create"
SECRET = "pw-SECRET-123"


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


async def _jobs(db: AsyncSession, kind: MaintenanceJobKind) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(MaintenanceJob)
            .where(MaintenanceJob.kind == kind)
        )
        or 0
    )


async def test_bulk_create_item_with_an_unknown_field_is_refused(
    db: AsyncSession, client: AsyncClient
) -> None:
    """`interval` is not an item's field (`interval_seconds` is): a 422."""
    editor = await _as(db, "editor")
    resp = await client.post(
        BULK_CREATE,
        json={
            "agent_id": str(await _agent(db)),
            "items": [{"url": "https://x.test", "interval": 5}],
        },
        headers=editor,
    )
    assert resp.status_code == 422, resp.text


async def test_control_bulk_create_top_level_unknown_field_is_refused(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    resp = await client.post(
        BULK_CREATE,
        json={
            "agent_id": str(await _agent(db)),
            "items": [{"url": "https://x.test"}],
            "bogus": 1,
        },
        headers=editor,
    )
    assert resp.status_code == 422, resp.text


async def test_import_entry_with_a_field_import_does_not_take_is_refused(
    db: AsyncSession, client: AsyncClient
) -> None:
    """An entry's `connection_string` is not a field the import takes: a 422."""
    admin = await _as(db, "admin")
    agent_id = await _agent(db)
    resp = await client.post(
        f"/api/v1/admin/agents/{agent_id}/checks/import",
        json={
            "checks": [
                {
                    "name": "pg",
                    "check_type": "postgres",
                    "target": "db.test",
                    "connection_string": f"postgresql://m:{SECRET}@db.test/app",
                    "query": "SELECT 1",
                }
            ]
        },
        headers=admin,
    )
    assert resp.status_code == 422, resp.text


async def test_viewer_reading_an_import_job_sees_no_params(
    db: AsyncSession, client: AsyncClient
) -> None:
    admin = await _as(db, "admin")
    viewer = await _as(db, "viewer")
    agent_id = await _agent(db)
    resp = await client.post(
        f"/api/v1/admin/agents/{agent_id}/checks/import",
        json={
            "checks": [
                {
                    "name": "db",
                    "check_type": "mysql",
                    "target": f"mysql://m:{SECRET}@db.test/app",
                }
            ]
        },
        headers=admin,
    )
    assert resp.status_code == 202, resp.text
    job = await client.get(
        f"/api/v1/admin/maintenance-jobs/{resp.json()['id']}", headers=viewer
    )
    assert job.status_code == 200
    assert SECRET not in job.text


async def test_export_keeps_retry_attempts_zero_over_the_api(
    db: AsyncSession, client: AsyncClient
) -> None:
    """A retry count of 0 is exported as 0."""
    admin = await _as(db, "admin")
    agent_id = await _agent(db)
    db.add(
        make_check(
            agent_id=agent_id,
            display_name="web",
            check_type="http",
            target="https://x.test",
            retry_attempts=0,
        )
    )
    await db.flush()
    resp = await client.get(
        f"/api/v1/admin/agents/{agent_id}/checks/export", headers=admin
    )
    assert resp.status_code == 200
    assert resp.json()["checks"][0]["retry_attempts"] == 0, resp.json()


# ── Web import form ─────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def web(monkeypatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings.security, "auth_enabled", False)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    ) as c:
        yield c


@pytest.mark.parametrize(
    ("mode", "content"),
    [
        ("overwrite", json.dumps({"checks": [{"name": "a"}]})),
        ("merge", json.dumps([1, 2])),
        ("merge", json.dumps({"checks": {"a": 1}})),
        ("merge", json.dumps({"checks": ["x"] * 10_001})),
        ("merge", b"\xff\xfe not utf8"),
        ("merge", json.dumps("checks")),
    ],
    ids=["bad-mode", "list", "checks-dict", "too-many", "not-utf8", "string"],
)
async def test_web_import_bad_input_is_never_a_500_and_queues_nothing(
    db: AsyncSession, web: AsyncClient, mode: str, content: str | bytes
) -> None:
    agent_id = await _agent(db)
    url = app.url_path_for("web_import_checks", agent_id=str(agent_id))
    resp = await web.post(
        url,
        data={"mode": mode},
        files={"file": ("c.json", content, "application/json")},
    )
    assert resp.status_code < 500, (resp.status_code, resp.text[:500])
    assert resp.status_code != 200 or "error" in resp.text.lower(), resp.text[:500]
    assert await _jobs(db, MaintenanceJobKind.BULK_CHECK_IMPORT) == 0
