"""Bulk check actions, background-job status and dependents on the admin API.

A bulk action is judged whole before it runs or is queued: an unknown id, or a
synthetic check in an editor's selection, refuses the request and changes
nothing. Delete and modify run as maintenance jobs, whose outcome (counts and
first errors) is read back from `/maintenance-jobs/{id}`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.background.maintenance_handlers import bulk_check_delete, bulk_check_modify
from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app
from app.models.check_model import Check
from app.models.maintenance_job_model import MaintenanceJob
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.api_token_core_service import ApiTokenCoreService
from fixtures.factories import make_agent, make_check, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

CHECKS = "/api/v1/admin/checks"
JOBS = "/api/v1/admin/maintenance-jobs"
DSN = "postgresql://monitor:s3cret-pw@db.internal:5432/app"


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


async def _agent(db: AsyncSession):
    agent = make_agent()
    db.add(agent)
    await db.flush()
    return agent


async def _check(db: AsyncSession, agent, **overrides) -> Check:
    check = make_check(agent_id=agent.id, **overrides)
    db.add(check)
    await db.flush()
    return check


async def _synthetic(db: AsyncSession, agent) -> Check:
    return await _check(
        db,
        agent,
        check_type="synthetic",
        target="https://a.example",
        script_code="await page.goto('https://a.example')",
    )


async def _enabled(db: AsyncSession, *checks: Check) -> list[bool]:
    ids = [c.id for c in checks]
    db.expire_all()
    rows = {
        c.id: c.enabled
        for c in (await db.scalars(select(Check).where(Check.id.in_(ids)))).all()
    }
    return [rows[i] for i in ids]


async def _jobs(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(MaintenanceJob)) or 0)


async def _run(db: AsyncSession, handler, job_id: str) -> None:
    job = await db.get(MaintenanceJob, UUID(job_id))
    assert job is not None
    await handler.handle(db, job)
    await db.flush()


# ── Role denial ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/bulk/enable", "ids"),
        ("POST", "/bulk/disable", "ids"),
        ("POST", "/bulk/delete", "ids"),
        ("POST", "/bulk/modify", "modify"),
        ("PUT", "/{id}/dependents", "dependents"),
    ],
)
async def test_a_viewer_cannot_write(
    db: AsyncSession, client: AsyncClient, method: str, path: str, body: str
) -> None:
    viewer = await _as(db, "viewer")
    check = await _check(db, await _agent(db))
    payload = {
        "ids": {"check_ids": [str(check.id)]},
        "modify": {"check_ids": [str(check.id)], "interval_seconds": 30},
        "dependents": {"dependent_ids": []},
    }[body]

    resp = await client.request(
        method, CHECKS + path.format(id=check.id), json=payload, headers=viewer
    )

    assert resp.status_code == 403, resp.text
    assert await _enabled(db, check) == [True]
    assert await _jobs(db) == 0


async def test_no_token_is_refused(client: AsyncClient) -> None:
    resp = await client.post(
        f"{CHECKS}/bulk/enable", json={"check_ids": [str(uuid4())]}
    )

    assert resp.status_code == 401


# ── Enable / disable ─────────────────────────────────────────────────────────


async def test_an_editor_disables_and_enables_checks_now(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    agent = await _agent(db)
    a, b, untouched = (
        await _check(db, agent),
        await _check(db, agent),
        await _check(db, agent),
    )

    off = await client.post(
        f"{CHECKS}/bulk/disable",
        json={"check_ids": [str(a.id), str(b.id), str(a.id)]},
        headers=editor,
    )

    assert off.status_code == 200, off.text
    assert off.json() == {"success_count": 2, "failure_count": 0, "errors": []}
    assert await _enabled(db, a, b, untouched) == [False, False, True]

    on = await client.post(
        f"{CHECKS}/bulk/enable", json={"check_ids": [str(a.id)]}, headers=editor
    )

    assert on.json()["success_count"] == 1
    assert await _enabled(db, a, b) == [True, False]


async def test_an_unknown_id_refuses_the_whole_selection(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    check = await _check(db, await _agent(db))
    selection = {"check_ids": [str(check.id), str(uuid4())]}

    disable = await client.post(
        f"{CHECKS}/bulk/disable", json=selection, headers=editor
    )
    delete = await client.post(f"{CHECKS}/bulk/delete", json=selection, headers=editor)

    assert (disable.status_code, delete.status_code) == (422, 422)
    assert await _enabled(db, check) == [True]
    assert await _jobs(db) == 0


@pytest.mark.parametrize(
    "selection",
    [{"check_ids": []}, {"check_ids": ["not-a-uuid"]}, {"ids": []}, {}],
)
async def test_a_malformed_selection_is_refused(
    db: AsyncSession, client: AsyncClient, selection: dict[str, object]
) -> None:
    editor = await _as(db, "editor")

    resp = await client.post(f"{CHECKS}/bulk/enable", json=selection, headers=editor)

    assert resp.status_code == 422


async def test_a_selection_over_the_cap_is_refused(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    too_many = [str(uuid4()) for _ in range(10_001)]

    resp = await client.post(
        f"{CHECKS}/bulk/delete", json={"check_ids": too_many}, headers=editor
    )

    assert resp.status_code == 422
    assert await _jobs(db) == 0


# ── Synthetic checks are an admin's ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "extra"),
    [
        ("/bulk/enable", {}),
        ("/bulk/disable", {}),
        ("/bulk/delete", {}),
        ("/bulk/modify", {"interval_seconds": 30}),
    ],
)
async def test_an_editor_selection_naming_a_synthetic_check_is_refused(
    db: AsyncSession, client: AsyncClient, path: str, extra: dict[str, object]
) -> None:
    editor = await _as(db, "editor")
    agent = await _agent(db)
    plain = await _check(db, agent)
    synthetic = await _synthetic(db, agent)

    resp = await client.post(
        CHECKS + path,
        json={"check_ids": [str(plain.id), str(synthetic.id)], **extra},
        headers=editor,
    )

    assert resp.status_code == 403, resp.text
    assert await _enabled(db, plain, synthetic) == [True, True]
    assert await _jobs(db) == 0


async def test_an_admin_disables_a_synthetic_check(
    db: AsyncSession, client: AsyncClient
) -> None:
    admin = await _as(db, "admin")
    synthetic = await _synthetic(db, await _agent(db))

    resp = await client.post(
        f"{CHECKS}/bulk/disable",
        json={"check_ids": [str(synthetic.id)]},
        headers=admin,
    )

    assert resp.json()["success_count"] == 1
    assert await _enabled(db, synthetic) == [False]


# ── Delete and modify run as jobs ────────────────────────────────────────────


async def test_bulk_delete_queues_a_job_whose_outcome_is_read_back(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    viewer = await _as(db, "viewer")
    agent = await _agent(db)
    a, b, kept = (
        await _check(db, agent),
        await _check(db, agent),
        await _check(db, agent),
    )
    a_id, b_id, kept_id = a.id, b.id, kept.id

    queued = await client.post(
        f"{CHECKS}/bulk/delete",
        json={"check_ids": [str(a_id), str(b_id)]},
        headers=editor,
    )

    assert queued.status_code == 202, queued.text
    job = queued.json()
    assert (job["kind"], job["status"]) == ("bulk_check_delete", "queued")

    await _run(db, bulk_check_delete, job["id"])
    polled = await client.get(f"{JOBS}/{job['id']}", headers=viewer)

    assert polled.status_code == 200
    progress = polled.json()["progress"]
    assert (progress["success_count"], progress["failure_count"]) == (2, 0)
    assert progress["message"] == "Deleted 2 of 2 check(s)"
    remaining = set(
        (await db.scalars(select(Check.id).where(Check.agent_id == agent.id))).all()
    )
    assert remaining == {kept_id}


async def test_bulk_modify_queues_a_job_that_changes_the_checks(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    agent = await _agent(db)
    a, b = await _check(db, agent), await _check(db, agent)
    ids = [a.id, b.id]

    queued = await client.post(
        f"{CHECKS}/bulk/modify",
        json={
            "check_ids": [str(i) for i in ids],
            "interval_seconds": 300,
            "retry_attempts": 2,
        },
        headers=editor,
    )

    assert queued.status_code == 202, queued.text
    await _run(db, bulk_check_modify, queued.json()["id"])
    db.expire_all()
    changed = (await db.scalars(select(Check).where(Check.id.in_(ids)))).all()
    assert {(c.interval_seconds, c.retry_attempts) for c in changed} == {(300, 2)}
    progress = (
        await client.get(f"{JOBS}/{queued.json()['id']}", headers=editor)
    ).json()["progress"]
    assert progress["message"] == "Modified 2 of 2 check(s)"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"interval_seconds": None},
        {"interval_seconds": 0},
        {"timeout_seconds": 301},
        {"enabled": False},
        {"script_code": "x"},
    ],
)
async def test_a_bulk_modify_must_change_something_it_allows(
    db: AsyncSession, client: AsyncClient, body: dict[str, object]
) -> None:
    editor = await _as(db, "editor")
    check = await _check(db, await _agent(db))

    resp = await client.post(
        f"{CHECKS}/bulk/modify",
        json={"check_ids": [str(check.id)], **body},
        headers=editor,
    )

    assert resp.status_code == 422, resp.text
    assert await _jobs(db) == 0


@pytest.mark.parametrize("field", ["agent_id", "alert_id"])
async def test_a_bulk_modify_to_an_unknown_agent_or_alert_is_refused(
    db: AsyncSession, client: AsyncClient, field: str
) -> None:
    editor = await _as(db, "editor")
    check = await _check(db, await _agent(db))

    resp = await client.post(
        f"{CHECKS}/bulk/modify",
        json={"check_ids": [str(check.id)], field: str(uuid4())},
        headers=editor,
    )

    assert resp.status_code == 404, resp.text
    assert await _jobs(db) == 0


async def test_an_unknown_job_is_404(db: AsyncSession, client: AsyncClient) -> None:
    viewer = await _as(db, "viewer")

    resp = await client.get(f"{JOBS}/{uuid4()}", headers=viewer)

    assert resp.status_code == 404


# ── Dependents ───────────────────────────────────────────────────────────────


async def test_dependents_are_replaced_and_listed(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    viewer = await _as(db, "viewer")
    agent = await _agent(db)
    parent = await _check(db, agent)
    plain = await _check(db, agent)
    pg = await _check(
        db, agent, check_type="postgres", target="db.internal", connection_string=DSN
    )

    attached = await client.put(
        f"{CHECKS}/{parent.id}/dependents",
        json={"dependent_ids": [str(plain.id), str(pg.id)]},
        headers=editor,
    )
    listed = await client.get(f"{CHECKS}/{parent.id}/dependents", headers=viewer)

    assert attached.json() == {"added": 2, "removed": 0}
    assert {c["id"] for c in listed.json()} == {str(plain.id), str(pg.id)}
    assert "s3cret-pw" not in listed.text

    detached = await client.put(
        f"{CHECKS}/{parent.id}/dependents",
        json={"dependent_ids": [str(plain.id)]},
        headers=editor,
    )

    assert detached.json() == {"added": 0, "removed": 1}
    after = await client.get(f"{CHECKS}/{parent.id}/dependents", headers=viewer)
    assert [c["id"] for c in after.json()] == [str(plain.id)]


async def test_dependents_of_an_unknown_check_are_404(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    missing = uuid4()

    listed = await client.get(f"{CHECKS}/{missing}/dependents", headers=editor)
    replaced = await client.put(
        f"{CHECKS}/{missing}/dependents", json={"dependent_ids": []}, headers=editor
    )

    assert (listed.status_code, replaced.status_code) == (404, 404)


async def test_an_editor_cannot_attach_a_synthetic_dependent(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    agent = await _agent(db)
    parent = await _check(db, agent)
    synthetic = await _synthetic(db, agent)
    synthetic_id = synthetic.id

    resp = await client.put(
        f"{CHECKS}/{parent.id}/dependents",
        json={"dependent_ids": [str(synthetic_id)]},
        headers=editor,
    )

    assert resp.status_code == 403, resp.text
    db.expire_all()
    parent_of = await db.scalar(
        select(Check.depends_on_check_id).where(Check.id == synthetic_id)
    )
    assert parent_of is None


@pytest.mark.parametrize("approval_status", ["pending", "rejected", "archived"])
async def test_a_bulk_move_to_an_agent_that_takes_no_checks_is_refused(
    db: AsyncSession, client: AsyncClient, approval_status: str
) -> None:
    editor = await _as(db, "editor")
    check = await _check(db, await _agent(db))
    target = make_agent(approval_status=approval_status)
    db.add(target)
    await db.flush()

    resp = await client.post(
        f"{CHECKS}/bulk/modify",
        json={"check_ids": [str(check.id)], "agent_id": str(target.id)},
        headers=editor,
    )

    assert resp.status_code == 422, resp.text
    assert await _jobs(db) == 0


async def test_the_admin_openapi_names_the_new_schemas_plainly(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _as(db, "viewer")

    doc = (await client.get("/api/v1/admin/openapi.json", headers=viewer)).json()

    names = set(doc["components"]["schemas"])
    assert not [n for n in names if "__" in n], sorted(names)
    assert {
        "AdminBulkChecks",
        "AdminBulkModify",
        "BulkCheckResult",
        "MaintenanceJobResponse",
        "AdminCheckDependents",
        "DependentsChange",
    } <= names
    assert f"{JOBS}/{{job_id}}" in doc["paths"]
