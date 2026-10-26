"""API smoke tests — confirm routes are wired, auth gates work, response
shapes match `response_model`.

These are intentionally shallow: business logic is covered by the per-domain
service/CRUD integration tests. The point of these tests is to catch
misconfigured routes, broken Depends() chains, and auth bypasses.
"""

from __future__ import annotations

import base64
from uuid import uuid4

import pytest
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.main import app
from app.services.core.agent_core_service import AgentCoreService
from fixtures.factories import make_agent, make_check
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures: the FastAPI app as it is (its sessions join the test's transaction)
# ---------------------------------------------------------------------------


@pytest.fixture
async def client(db, monkeypatch):
    """Boot the FastAPI app as it is, and
    disable the API-token check so smoke tests don't need to thread tokens
    through every call.

    Use this for testing route wiring + response shapes. Auth-specific tests
    flip auth_enabled back on via their own fixture.
    """

    # Disable auth so we don't have to thread bearer tokens through smoke tests
    monkeypatch.setattr(settings.security, "auth_enabled", False)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def authed_client(db, monkeypatch):
    """Like `client`, but with authentication ON."""

    monkeypatch.setattr(settings.security, "auth_enabled", True)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def upload(db):
    """A well-formed artifact upload for an approved agent's own check, and
    the agent's key: the gate is the only thing left to refuse it."""
    agent = make_agent(approval_status="pending")
    db.add(agent)
    await db.flush()
    _, key = await AgentCoreService.approve_agent(db, agent.id)
    check = make_check(agent_id=agent.id, check_type="synthetic")
    db.add(check)
    await db.flush()
    payload = {
        "agent_id": str(agent.id),
        "check_id": str(check.id),
        "check_result_id": str(uuid4()),
        "check_result_timestamp": utc_now().isoformat(),
        "artifact_type": "screenshot",
        "content_type": "image/png",
        "filename": "shot.png",
        "data_base64": base64.b64encode(b"png").decode(),
    }
    return payload, key


# ---------------------------------------------------------------------------
# Health / metadata routes
# ---------------------------------------------------------------------------


class TestHealthRoutes:
    async def test_health_returns_ok(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert "status" in body

    async def test_health_names_the_build_commit(self, client, monkeypatch):
        """`make smoke` proves the deployed stack runs THIS build by finding the
        commit in /health; without it every dev deploy's smoke fails."""
        monkeypatch.setattr(settings, "build_commit", "abc1234")
        resp = await client.get("/health")
        assert resp.json()["commit"] == "abc1234"

    async def test_root_returns_api_info(self, client):
        resp = await client.get("/")
        # Root may return 200 (JSON) or 302/307 (redirect to /dashboard)
        assert resp.status_code in (200, 302, 307)

    async def test_api_root_describes_the_api(self, client):
        resp = await client.get(app.url_path_for("api_root"))
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Auth gating: the artifact upload takes the uploading agent's own key
# ---------------------------------------------------------------------------


class TestAuthGating:
    async def test_missing_token_returns_401(self, authed_client, upload):
        payload, _ = upload
        resp = await authed_client.post("/api/v1/agent/artifacts", json=payload)
        assert resp.status_code == 401

    async def test_wrong_token_returns_401(self, authed_client, upload):
        payload, _ = upload
        resp = await authed_client.post(
            "/api/v1/agent/artifacts",
            json=payload,
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401

    async def test_the_agent_key_passes_the_gate(self, authed_client, upload):
        payload, key = upload
        resp = await authed_client.post(
            "/api/v1/agent/artifacts",
            json=payload,
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 201, resp.text

    async def test_malformed_authorization_header_returns_401(
        self, authed_client, upload
    ):
        payload, key = upload
        # Missing "Bearer " prefix
        resp = await authed_client.post(
            "/api/v1/agent/artifacts",
            json=payload,
            headers={"Authorization": key},
        )
        assert resp.status_code == 401
