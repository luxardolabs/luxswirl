"""Agents on the admin API: read by any token, changed by an editor.

The rules are the web UI's, through the same core service: a change that moves
or removes a synthetic check is an admin's, a wrong-state action is refused,
a delete runs as a maintenance job, and an agent key is shown once and never
stored or returned again.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import bcrypt
import pytest
import pytest_asyncio
from app.background.maintenance_handlers import agent_delete
from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app
from app.models.agent_model import Agent
from app.models.check_model import Check
from app.models.maintenance_job_model import MaintenanceJob
from app.models.user_model import User
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.api_token_core_service import ApiTokenCoreService
from fixtures.factories import make_agent, make_check, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

AGENTS = "/api/v1/admin/agents"


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    limiter.reset()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    ) as c:
        yield c
    limiter.reset()


async def _user(db: AsyncSession, role: str) -> tuple[UUID, dict[str, str]]:
    user = make_user(role=role)
    db.add(user)
    await db.flush()
    _, plaintext = await ApiTokenCoreService.create_token(
        db, user.id, ApiTokenCreate(name=role)
    )
    return user.id, {"Authorization": f"Bearer {plaintext}"}


async def _as(db: AsyncSession, role: str) -> dict[str, str]:
    return (await _user(db, role))[1]


async def _agent(db: AsyncSession, **overrides) -> Agent:
    agent = make_agent(**overrides)
    db.add(agent)
    await db.flush()
    return agent


async def _status(db: AsyncSession, agent_id: UUID) -> str:
    db.expire_all()
    return str(
        await db.scalar(select(Agent.approval_status).where(Agent.id == agent_id))
    )


async def _synthetic_on(db: AsyncSession, agent: Agent) -> Check:
    check = make_check(
        agent_id=agent.id,
        check_type="synthetic",
        target="https://a.example",
        script_code="await page.goto('https://a.example')",
    )
    db.add(check)
    await db.flush()
    return check


# ── Reads ────────────────────────────────────────────────────────────────────


async def test_no_token_is_refused(client: AsyncClient) -> None:
    assert (await client.get(AGENTS)).status_code == 401


async def test_the_list_is_a_page_that_leaves_archived_agents_out(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _as(db, "viewer")
    tag = uuid4().hex[:8]
    live = [await _agent(db, agent_name=f"live-{tag}-{n}") for n in range(3)]
    gone = await _agent(db, agent_name=f"gone-{tag}", approval_status="archived")

    first = await client.get(
        AGENTS, params={"search": tag, "per_page": 2}, headers=viewer
    )
    archived = await client.get(
        AGENTS, params={"search": tag, "archived": True}, headers=viewer
    )

    assert first.status_code == 200, first.text
    body = first.json()
    assert (body["total"], body["has_next"], len(body["items"])) == (3, True, 2)
    assert {a["id"] for a in archived.json()["items"]} == {str(gone.id)}
    assert str(live[0].id) not in archived.text


async def test_an_agent_reads_without_its_key_hash(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _as(db, "viewer")
    key_hash = bcrypt.hashpw(b"k", bcrypt.gensalt(rounds=4)).decode()
    agent = await _agent(db, api_key_hash=key_hash)

    resp = await client.get(f"{AGENTS}/{agent.id}", headers=viewer)

    assert resp.status_code == 200, resp.text
    assert resp.json()["has_api_key"] is True
    assert key_hash not in resp.text
    assert "api_key_hash" not in resp.json()


async def test_an_unknown_agent_is_404(db: AsyncSession, client: AsyncClient) -> None:
    viewer = await _as(db, "viewer")

    assert (await client.get(f"{AGENTS}/{uuid4()}", headers=viewer)).status_code == 404


# ── Writes are an editor's ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("PATCH", "", {"agent_name": "x"}),
        ("DELETE", "", None),
        ("POST", "/approve", None),
        ("POST", "/reject", None),
        ("POST", "/pause", None),
        ("POST", "/resume", None),
        ("POST", "/disable", None),
        ("POST", "/enable", None),
        ("POST", "/archive", None),
        ("POST", "/restore", None),
        ("POST", "/force-reload", None),
        ("POST", "/key", None),
        ("POST", "/key/regenerate", None),
        ("DELETE", "/key", None),
    ],
)
async def test_a_viewer_cannot_change_an_agent(
    db: AsyncSession,
    client: AsyncClient,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    viewer = await _as(db, "viewer")
    agent = await _agent(db, approval_status="pending")

    resp = await client.request(
        method, f"{AGENTS}/{agent.id}{path}", json=body, headers=viewer
    )

    assert resp.status_code == 403, resp.text
    assert await _status(db, agent.id) == "pending"


async def test_an_editor_renames_and_reconfigures_an_agent(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    agent = await _agent(db)

    resp = await client.patch(
        f"{AGENTS}/{agent.id}",
        json={"agent_name": "edge-01", "tags": ["edge"], "heartbeat_interval": 30},
        headers=editor,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["agent_name"], body["tags"], body["heartbeat_interval"]) == (
        "edge-01",
        ["edge"],
        30,
    )


@pytest.mark.parametrize(
    "body",
    [
        {"version": "9.9.9"},
        {"ip_address": "10.0.0.9"},
        {"agent_run_id": "x"},
        {"api_key_hash": "x"},
        {"approval_status": "active"},
        {"heartbeat_interval": 0},
    ],
)
async def test_an_update_changes_only_what_the_edit_form_does(
    db: AsyncSession, client: AsyncClient, body: dict[str, object]
) -> None:
    editor = await _as(db, "editor")
    agent = await _agent(db, approval_status="pending")

    resp = await client.patch(f"{AGENTS}/{agent.id}", json=body, headers=editor)

    assert resp.status_code == 422, resp.text
    assert await _status(db, agent.id) == "pending"


async def test_an_editor_retag_that_moves_a_synthetic_check_is_refused(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    admin = await _as(db, "admin")
    agent = await _agent(db, tags=["role:db"])
    other = await _agent(db, tags=["role:browser"])
    check = make_check(
        agent_id=other.id,
        check_type="synthetic",
        target="https://a.example",
        script_code="x = 1",
        assignment_mode="replicate",
        agent_selector={"tags": ["role:browser"]},
    )
    db.add(check)
    await db.flush()

    refused = await client.patch(
        f"{AGENTS}/{agent.id}", json={"tags": ["role:browser"]}, headers=editor
    )
    allowed = await client.patch(
        f"{AGENTS}/{agent.id}", json={"tags": ["role:browser"]}, headers=admin
    )

    assert refused.status_code == 403, refused.text
    assert allowed.status_code == 200, allowed.text


# ── Lifecycle ────────────────────────────────────────────────────────────────


async def test_an_agent_walks_its_lifecycle(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    agent = await _agent(db, approval_status="pending")
    url = f"{AGENTS}/{agent.id}"

    steps = [
        ("approve", None, "active"),
        ("pause", {"reason": "maintenance window"}, "paused"),
        ("resume", None, "active"),
        ("disable", {"reason": "decommissioning"}, "disabled"),
        ("enable", None, "active"),
        ("archive", None, "archived"),
        ("restore", None, "disabled"),
    ]
    for action, body, expected in steps:
        resp = await client.post(f"{url}/{action}", json=body, headers=editor)
        assert resp.status_code == 200, (action, resp.text)
        assert resp.json()["approval_status"] == expected, action


async def test_a_reason_is_kept_with_the_status(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    agent = await _agent(db, approval_status="pending")

    resp = await client.post(
        f"{AGENTS}/{agent.id}/reject", json={"reason": "unknown host"}, headers=editor
    )

    assert resp.json()["status_reason"] == "unknown host"


@pytest.mark.parametrize(
    ("status", "action"),
    [
        ("active", "approve"),
        ("active", "resume"),
        ("active", "enable"),
        ("pending", "pause"),
        ("active", "restore"),
    ],
)
async def test_a_wrong_state_action_is_refused(
    db: AsyncSession, client: AsyncClient, status: str, action: str
) -> None:
    editor = await _as(db, "editor")
    agent = await _agent(db, approval_status=status)

    resp = await client.post(f"{AGENTS}/{agent.id}/{action}", headers=editor)

    assert resp.status_code == 422, resp.text
    assert await _status(db, agent.id) == status


async def test_approval_hands_out_no_key(db: AsyncSession, client: AsyncClient) -> None:
    editor = await _as(db, "editor")
    agent = await _agent(db, approval_status="pending")

    resp = await client.post(f"{AGENTS}/{agent.id}/approve", headers=editor)

    assert resp.status_code == 200, resp.text
    assert resp.json()["has_api_key"] is True
    assert "luxswirl_ak_" not in resp.text


async def test_an_editor_approval_that_starts_a_synthetic_check_is_refused(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    agent = await _agent(db, approval_status="pending")
    await _synthetic_on(db, agent)

    resp = await client.post(f"{AGENTS}/{agent.id}/approve", headers=editor)

    assert resp.status_code == 403, resp.text
    assert await _status(db, agent.id) == "pending"


# ── Keys ─────────────────────────────────────────────────────────────────────


async def test_a_key_is_shown_once_and_only_its_hash_kept(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    agent_id = (await _agent(db)).id

    first = await client.post(f"{AGENTS}/{agent_id}/key", headers=editor)
    again = await client.post(f"{AGENTS}/{agent_id}/key", headers=editor)
    rotated = await client.post(f"{AGENTS}/{agent_id}/key/regenerate", headers=editor)

    assert first.status_code == 201, first.text
    key = first.json()["api_key"]
    assert key.startswith("luxswirl_ak_")
    assert again.status_code == 400, again.text
    new_key = rotated.json()["api_key"]
    assert new_key != key
    db.expire_all()
    stored = await db.scalar(select(Agent.api_key_hash).where(Agent.id == agent_id))
    assert stored is not None and new_key not in stored
    assert bcrypt.checkpw(new_key.encode(), stored.encode())
    assert key not in (await client.get(f"{AGENTS}/{agent_id}", headers=editor)).text


async def test_a_revoked_key_is_cleared(db: AsyncSession, client: AsyncClient) -> None:
    editor = await _as(db, "editor")
    agent = await _agent(db)
    await client.post(f"{AGENTS}/{agent.id}/key", headers=editor)

    resp = await client.delete(f"{AGENTS}/{agent.id}/key", headers=editor)

    assert resp.status_code == 204
    after = await client.get(f"{AGENTS}/{agent.id}", headers=editor)
    assert after.json()["has_api_key"] is False


# ── Delete runs as a job ─────────────────────────────────────────────────────


async def _agents_named(db: AsyncSession, agent_id: UUID) -> int:
    db.expire_all()
    return int(
        await db.scalar(
            select(func.count()).select_from(Agent).where(Agent.id == agent_id)
        )
        or 0
    )


async def test_a_delete_queues_a_job_that_deletes_the_agent(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    agent = await _agent(db)
    agent_id = agent.id

    queued = await client.delete(f"{AGENTS}/{agent_id}", headers=editor)

    assert queued.status_code == 202, queued.text
    assert queued.json()["kind"] == "agent_delete"
    job = await db.get(MaintenanceJob, UUID(queued.json()["id"]))
    await agent_delete.handle(db, job)
    assert await _agents_named(db, agent_id) == 0


async def test_an_editor_delete_of_an_agent_running_a_synthetic_check_is_refused(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    agent = await _agent(db)
    await _synthetic_on(db, agent)

    resp = await client.delete(f"{AGENTS}/{agent.id}", headers=editor)

    assert resp.status_code == 403, resp.text
    assert (
        int(await db.scalar(select(func.count()).select_from(MaintenanceJob)) or 0) == 0
    )


async def test_a_delete_whose_owner_was_demoted_changes_nothing(
    db: AsyncSession, client: AsyncClient
) -> None:
    owner_id, editor = await _user(db, "editor")
    agent = await _agent(db)
    agent_id = agent.id
    queued = await client.delete(f"{AGENTS}/{agent_id}", headers=editor)
    await db.execute(update(User).where(User.id == owner_id).values(role="viewer"))

    job = await db.get(MaintenanceJob, UUID(queued.json()["id"]))
    with pytest.raises(Exception, match="no longer an active editor or admin"):
        await agent_delete.handle(db, job)

    assert await _agents_named(db, agent_id) == 1


async def test_a_delete_of_an_unknown_agent_is_404(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")

    resp = await client.delete(f"{AGENTS}/{uuid4()}", headers=editor)

    assert resp.status_code == 404
