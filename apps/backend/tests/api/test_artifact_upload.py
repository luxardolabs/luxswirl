"""Artifact upload authenticates as the agent, and only for that agent's checks.

POST /api/v1/agent/artifacts takes the uploading agent's OWN key, exactly like
POST /api/v1/agent/reports: the registration key, another agent's key, or no key at
all are refused before anything is written. The artifact's check must be one
the assignment model gives that agent (its manual check, or a replicate /
distribute check its selector matches); another agent's check is refused.

Auth is ON in every test here.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.rate_limit import limiter
from app.crud.artifact_crud import ArtifactCRUD
from app.main import app
from app.models.agent_model import Agent
from app.models.check_model import Check
from app.schemas.registration_key_schema import RegistrationKeyCreate
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.check_result_core_service import CheckResultCoreService
from app.services.core.registration_key_core_service import RegistrationKeyCoreService
from fixtures.factories import make_agent, make_check, make_check_result
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

PNG = b"\x89PNG-not-really-but-bytes"


@pytest_asyncio.fixture
async def client(db: AsyncSession, monkeypatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    limiter.reset()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _approved_agent(db: AsyncSession, **overrides: Any) -> tuple[Agent, str]:
    """An agent the operator approved, and the key it was issued."""
    agent = make_agent(approval_status="pending", **overrides)
    db.add(agent)
    await db.flush()
    _, key = await AgentCoreService.approve_agent(db, agent.id)
    return agent, key


async def _check(db: AsyncSession, agent_id: UUID, **overrides: Any) -> Check:
    check = make_check(agent_id=agent_id, check_type="synthetic", **overrides)
    db.add(check)
    await db.flush()
    return check


def _payload(agent_id: UUID, check_id: UUID, **overrides: Any) -> dict[str, Any]:
    return {
        "agent_id": str(agent_id),
        "check_id": str(check_id),
        "check_result_id": str(uuid4()),
        "check_result_timestamp": "2026-10-22T10:00:00+00:00",
        "artifact_type": "screenshot",
        "content_type": "image/png",
        "filename": "shot.png",
        "data_base64": base64.b64encode(PNG).decode(),
        **overrides,
    }


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _stored_for(db: AsyncSession, check_id: UUID) -> list[UUID]:
    return await ArtifactCRUD.get_recent_ids_for_check(db, check_id, 100)


async def test_the_agent_key_uploads_for_its_own_check(
    client: AsyncClient, db: AsyncSession
) -> None:
    agent, key = await _approved_agent(db)
    check = await _check(db, agent.id)

    resp = await client.post(
        "/api/v1/agent/artifacts",
        json=_payload(agent.id, check.id),
        headers=_bearer(key),
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["size_bytes"] == len(PNG)
    assert "data" not in resp.json()
    assert len(await _stored_for(db, check.id)) == 1


async def test_the_registration_key_is_refused(
    client: AsyncClient, db: AsyncSession
) -> None:
    agent, _ = await _approved_agent(db)
    check = await _check(db, agent.id)
    _, reg_key = await RegistrationKeyCoreService.create_key(
        db, RegistrationKeyCreate(name="artifact-tests"), created_by="tests"
    )

    resp = await client.post(
        "/api/v1/agent/artifacts",
        json=_payload(agent.id, check.id),
        headers=_bearer(reg_key),
    )

    assert resp.status_code == 401, resp.text
    assert await _stored_for(db, check.id) == []


async def test_no_key_is_refused(client: AsyncClient, db: AsyncSession) -> None:
    agent, _ = await _approved_agent(db)
    check = await _check(db, agent.id)

    resp = await client.post(
        "/api/v1/agent/artifacts", json=_payload(agent.id, check.id)
    )

    assert resp.status_code == 401, resp.text
    assert await _stored_for(db, check.id) == []


async def test_another_agents_check_is_refused(
    client: AsyncClient, db: AsyncSession
) -> None:
    mine, my_key = await _approved_agent(db)
    theirs, _ = await _approved_agent(db)
    their_check = await _check(db, theirs.id)

    resp = await client.post(
        "/api/v1/agent/artifacts",
        json=_payload(mine.id, their_check.id),
        headers=_bearer(my_key),
    )

    assert resp.status_code == 403, resp.text
    assert await _stored_for(db, their_check.id) == []


async def test_claiming_to_be_another_agent_with_my_key_is_refused(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Agent A's key cannot pass as agent B by putting B's id in the body."""
    _, my_key = await _approved_agent(db)
    theirs, _ = await _approved_agent(db)
    their_check = await _check(db, theirs.id)

    resp = await client.post(
        "/api/v1/agent/artifacts",
        json=_payload(theirs.id, their_check.id),
        headers=_bearer(my_key),
    )

    assert resp.status_code == 401, resp.text
    assert await _stored_for(db, their_check.id) == []


async def test_an_agent_without_its_own_key_is_refused(
    client: AsyncClient, db: AsyncSession
) -> None:
    """A pending agent has no key yet, so it uploads nothing (as for reports)."""
    agent = make_agent(approval_status="pending")
    db.add(agent)
    await db.flush()
    check = await _check(db, agent.id)

    resp = await client.post(
        "/api/v1/agent/artifacts",
        json=_payload(agent.id, check.id),
        headers=_bearer("anything"),
    )

    assert resp.status_code == 403, resp.text


async def test_a_paused_agent_is_refused_even_with_its_key(
    client: AsyncClient, db: AsyncSession
) -> None:
    agent, key = await _approved_agent(db)
    check = await _check(db, agent.id)
    agent.approval_status = "paused"
    await db.flush()

    resp = await client.post(
        "/api/v1/agent/artifacts",
        json=_payload(agent.id, check.id),
        headers=_bearer(key),
    )

    assert resp.status_code == 403, resp.text
    assert await _stored_for(db, check.id) == []


async def test_an_unknown_agent_or_check_is_not_found(
    client: AsyncClient, db: AsyncSession
) -> None:
    agent, key = await _approved_agent(db)

    unknown_agent = await client.post(
        "/api/v1/agent/artifacts", json=_payload(uuid4(), uuid4()), headers=_bearer(key)
    )
    unknown_check = await client.post(
        "/api/v1/agent/artifacts",
        json=_payload(agent.id, uuid4()),
        headers=_bearer(key),
    )

    assert unknown_agent.status_code == 404, unknown_agent.text
    assert unknown_check.status_code == 404, unknown_check.text


async def test_a_replicate_check_selecting_the_agent_is_its_own(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Assignment follows the model, not the placeholder `agent_id` column."""
    placeholder, _ = await _approved_agent(db, tags=["role:db"])
    agent, key = await _approved_agent(db, tags=["role:web"])
    selected = await _check(
        db,
        placeholder.id,
        assignment_mode="replicate",
        agent_selector={"tags": ["role:web"]},
    )
    not_selected = await _check(
        db,
        placeholder.id,
        assignment_mode="replicate",
        agent_selector={"tags": ["role:db"]},
    )

    ok = await client.post(
        "/api/v1/agent/artifacts",
        json=_payload(agent.id, selected.id),
        headers=_bearer(key),
    )
    refused = await client.post(
        "/api/v1/agent/artifacts",
        json=_payload(agent.id, not_selected.id),
        headers=_bearer(key),
    )

    assert ok.status_code == 201, ok.text
    assert refused.status_code == 403, refused.text
    assert await _stored_for(db, not_selected.id) == []


async def test_an_artifact_cannot_be_pinned_on_another_checks_result(
    client: AsyncClient, db: AsyncSession
) -> None:
    """A's artifact naming B's result id never shows on B's result.

    The artifact is stored against A's own check; the listing for a result is
    scoped to that result's check, so it cannot be smuggled onto B's page.
    """
    mine, my_key = await _approved_agent(db)
    theirs, _ = await _approved_agent(db)
    my_check = await _check(db, mine.id)
    their_check = await _check(db, theirs.id)
    their_result = make_check_result(check_id=their_check.id, agent_id=theirs.id)
    db.add(their_result)
    await db.flush()

    resp = await client.post(
        "/api/v1/agent/artifacts",
        json=_payload(
            mine.id,
            my_check.id,
            check_result_id=str(their_result.id),
            check_result_timestamp=their_result.timestamp.isoformat(),
        ),
        headers=_bearer(my_key),
    )
    listed = await CheckResultCoreService.get_artifacts_for_result(
        db, their_check.id, their_result.id, their_result.timestamp
    )

    assert resp.status_code == 201, resp.text
    assert listed == []
