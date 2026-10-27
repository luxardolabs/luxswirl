"""Breaks found by attacking the admin API's check routes, kept as tests.

Each one passed before its fix and is refused now: a synthetic check is an
admin's to change in any way, a viewer reads no credential anywhere in a
check, an invalid script or a null required field is a client error, a clone
keeps its dependency, and only an admitted agent takes a check.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app
from app.models.agent_model import Agent
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.agent_assignment_core_service import AgentAssignmentCoreService
from app.services.core.api_token_core_service import ApiTokenCoreService
from fixtures.factories import make_agent, make_check, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

CHECKS = "/api/v1/admin/checks"
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
        db, user.id, ApiTokenCreate(name=role, scopes=["*"])
    )
    return {"Authorization": f"Bearer {plaintext}"}


async def _agent(db: AsyncSession, **kw):
    agent = make_agent(**kw)
    db.add(agent)
    await db.flush()
    return agent


async def _check(db: AsyncSession, agent, **overrides):
    check = make_check(agent_id=agent.id, **overrides)
    db.add(check)
    await db.flush()
    return check


# ---- control -------------------------------------------------------------


async def test_control_standard_dsn_is_masked(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _as(db, "viewer")
    agent = await _agent(db)
    pg = await _check(
        db, agent, check_type="postgres", target="db.internal", connection_string=DSN
    )
    body = (await client.get(f"{CHECKS}/{pg.id}", headers=viewer)).text
    assert "s3cret-pw" not in body


# ---- A: viewer reads a DB password through `target` ------------------------


async def test_viewer_reads_dsn_password_in_target(
    db: AsyncSession, client: AsyncClient
) -> None:
    # The web form's own placeholder for a postgres target is
    # postgres://username:password@host:5432/database, and the agent reads
    # `connection_string or target`.
    viewer = await _as(db, "viewer")
    agent = await _agent(db)
    pg = await _check(db, agent, check_type="postgres", target=DSN)
    one = (await client.get(f"{CHECKS}/{pg.id}", headers=viewer)).text
    listed = (
        await client.get(CHECKS, params={"agent_id": str(agent.id)}, headers=viewer)
    ).text
    assert "s3cret-pw" not in one
    assert "s3cret-pw" not in listed


async def test_viewer_reads_basic_auth_in_http_target(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _as(db, "viewer")
    agent = await _agent(db)
    web = await _check(
        db, agent, check_type="http", target="https://svc:s3cret-pw@api.example/h"
    )
    body = (await client.get(f"{CHECKS}/{web.id}", headers=viewer)).text
    assert "s3cret-pw" not in body


# ---- B: connection_string forms scrub_text does not mask -------------------


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://:s3cret-pw@db.internal:5432/app",  # no user name
        "postgresql://monitor:s3cret pw@db.internal:5432/app",  # space in password
        'postgresql://monitor:s3cret"pw@db.internal:5432/app',  # quote in password
        "host=db.internal user=monitor password='s3cret-pw' dbname=app",  # libpq quoted
    ],
)
async def test_viewer_reads_unmasked_dsn_forms(
    db: AsyncSession, client: AsyncClient, dsn: str
) -> None:
    viewer = await _as(db, "viewer")
    agent = await _agent(db)
    pg = await _check(
        db, agent, check_type="postgres", target="db.internal", connection_string=dsn
    )
    body = (await client.get(f"{CHECKS}/{pg.id}", headers=viewer)).text
    assert "s3cret" not in body


# ---- C: editor spreads a synthetic script to other agents ------------------


async def test_control_editor_cannot_clone_synthetic_to_another_agent(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    a1 = await _agent(db)
    a2 = await _agent(db)
    syn = await _check(
        db,
        a1,
        check_type="synthetic",
        target="https://a.example",
        script_code="await page.goto(target)",
    )
    r = await client.post(
        f"{CHECKS}/{syn.id}/clone", json={"agent_id": str(a2.id)}, headers=editor
    )
    assert r.status_code == 403


async def test_editor_replicates_synthetic_script_onto_another_agent(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    a1 = await _agent(db)
    a2 = await _agent(db)
    syn = await _check(
        db,
        a1,
        check_type="synthetic",
        target="https://a.example",
        script_code="await page.goto(target)",
    )
    r = await client.patch(
        f"{CHECKS}/{syn.id}",
        json={
            "assignment_mode": "replicate",
            "agent_selector": {"agent_ids": [a2.agent_name]},
        },
        headers=editor,
    )
    syn_id, a2_id, status = syn.id, a2.id, r.status_code
    db.expire_all()
    fresh_a2 = await db.get(Agent, a2_id, populate_existing=True)
    served = await AgentAssignmentCoreService.get_checks_for_agent(db, fresh_a2)
    # Same outcome the clone above refuses: admin-only code now runs on a2.
    assert syn_id not in {c.id for c in served}, (
        f"PATCH {status}; a2 now serves the synthetic check"
    )


# ---- D: editor plants an unvalidated script that becomes synthetic ---------


async def test_editor_planted_script_skips_validation_when_type_flips(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    admin = await _as(db, "admin")
    agent = await _agent(db)
    created = await client.post(
        CHECKS,
        json={
            "agent_id": str(agent.id),
            "display_name": "innocent",
            "check_type": "http",
            "target": "https://a.example",
            "script_code": "import os\nos.system('id')",
        },
        headers=editor,
    )
    assert created.status_code == 201, created.text
    # A check that is not synthetic keeps no script, so an admin who later
    # makes it synthetic never inherits a script nobody validated.
    assert created.json()["script_code"] is None
    flipped = await client.patch(
        f"{CHECKS}/{created.json()['id']}",
        json={"check_type": "synthetic"},
        headers=admin,
    )
    assert flipped.status_code == 200
    assert flipped.json()["script_code"] is None


async def test_control_admin_script_with_import_os_is_refused(
    db: AsyncSession, client: AsyncClient
) -> None:
    admin = await _as(db, "admin")
    agent = await _agent(db)
    r = await client.post(
        CHECKS,
        json={
            "agent_id": str(agent.id),
            "display_name": "s",
            "check_type": "synthetic",
            "target": "https://a.example",
            "script_code": "import os\nos.system('id')",
        },
        headers=admin,
    )
    assert r.status_code == 422, r.text


# ---- E: PATCH a NOT NULL field to null -------------------------------------


@pytest.mark.parametrize(
    "field", ["display_name", "target", "check_type", "enabled", "timeout_seconds"]
)
async def test_patch_null_is_a_422_not_a_500(
    db: AsyncSession, client: AsyncClient, field: str
) -> None:
    editor = await _as(db, "editor")
    agent = await _agent(db)
    check = await _check(db, agent)
    r = await client.patch(f"{CHECKS}/{check.id}", json={field: None}, headers=editor)
    assert r.status_code == 422, (r.status_code, r.text[:300])


# ---- F: clone drops the dependency -----------------------------------------


async def test_clone_keeps_the_source_dependency(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    agent = await _agent(db)
    parent = await _check(db, agent, display_name="parent")
    child = await _check(db, agent, display_name="child", depends_on_check_id=parent.id)
    r = await client.post(f"{CHECKS}/{child.id}/clone", headers=editor)
    assert r.status_code == 201, r.text
    assert r.json()["depends_on_check_id"] == str(parent.id)


async def test_clone_override_of_dependency_is_honoured(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    agent = await _agent(db)
    parent = await _check(db, agent, display_name="parent")
    src = await _check(db, agent, display_name="src")
    r = await client.post(
        f"{CHECKS}/{src.id}/clone",
        json={"depends_on_check_id": str(parent.id)},
        headers=editor,
    )
    assert r.status_code == 201, r.text
    assert r.json()["depends_on_check_id"] == str(parent.id)


# ---- G: mass assignment silently ignored -----------------------------------


async def test_patch_agent_id_is_refused_not_ignored(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    a1 = await _agent(db)
    a2 = await _agent(db)
    check = await _check(db, a1)
    r = await client.patch(
        f"{CHECKS}/{check.id}", json={"agent_id": str(a2.id)}, headers=editor
    )
    assert r.status_code == 422, (r.status_code, r.json().get("agent_id"))


# ---- H: write onto an agent the web UI never offers ------------------------


@pytest.mark.parametrize("status", ["archived", "pending", "rejected"])
async def test_create_on_unadmitted_agent_is_refused(
    db: AsyncSession, client: AsyncClient, status: str
) -> None:
    editor = await _as(db, "editor")
    agent = await _agent(db, approval_status=status)
    r = await client.post(
        CHECKS,
        json={
            "agent_id": str(agent.id),
            "display_name": "x",
            "check_type": "ping",
            "target": "127.0.0.1",
        },
        headers=editor,
    )
    assert r.status_code >= 400, r.status_code


# ---- I: data integrity controls (expected to pass) -------------------------


async def test_control_delete_parent_nulls_child_dependency(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    agent = await _agent(db)
    parent = await _check(db, agent, display_name="parent")
    child = await _check(db, agent, display_name="child", depends_on_check_id=parent.id)
    r = await client.delete(f"{CHECKS}/{parent.id}", headers=editor)
    assert r.status_code == 204
    got = await client.get(f"{CHECKS}/{child.id}", headers=editor)
    assert got.status_code == 200 and got.json()["depends_on_check_id"] is None


async def test_control_page_beyond_last_and_empty(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _as(db, "viewer")
    agent = await _agent(db)
    empty = (
        await client.get(CHECKS, params={"agent_id": str(agent.id)}, headers=viewer)
    ).json()
    assert (empty["total"], empty["items"], empty["has_next"]) == (0, [], False)
    await _check(db, agent)
    far = (
        await client.get(
            CHECKS, params={"agent_id": str(agent.id), "page": 9}, headers=viewer
        )
    ).json()
    assert far["items"] == [] and far["has_next"] is False


async def test_control_editor_ssrf_via_clone_and_patch(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    agent = await _agent(db)
    check = await _check(db, agent, check_type="http", target="https://a.example")
    meta = "http://169.254.169.254/latest/meta-data/"
    c = await client.post(
        f"{CHECKS}/{check.id}/clone", json={"target": meta}, headers=editor
    )
    p = await client.patch(
        f"{CHECKS}/{check.id}", json={"target": meta}, headers=editor
    )
    dns = await _check(db, agent, check_type="dns", target="example.com")
    n = await client.patch(
        f"{CHECKS}/{dns.id}", json={"nameserver": "169.254.169.254"}, headers=editor
    )
    assert [x.status_code for x in (c, p, n)] == [c.status_code] * 3
    assert c.status_code in (400, 422), c.text


async def test_control_editor_cannot_patch_into_or_inside_synthetic(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    agent = await _agent(db)
    http = await _check(db, agent, check_type="http", target="https://a.example")
    syn = await _check(
        db,
        agent,
        check_type="synthetic",
        target="https://a.example",
        script_code="await page.goto(target)",
    )
    to_syn = await client.patch(
        f"{CHECKS}/{http.id}",
        json={"check_type": "synthetic", "script_code": "x=1"},
        headers=editor,
    )
    in_syn = await client.patch(
        f"{CHECKS}/{syn.id}", json={"script_code": "x=2"}, headers=editor
    )
    clone_to_syn = await client.post(
        f"{CHECKS}/{http.id}/clone",
        json={"check_type": "synthetic", "script_code": "x=3"},
        headers=editor,
    )
    assert [r.status_code for r in (to_syn, in_syn, clone_to_syn)] == [403] * 3
