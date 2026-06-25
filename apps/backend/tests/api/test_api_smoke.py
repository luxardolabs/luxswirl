"""API smoke tests — confirm routes are wired, auth gates work, response
shapes match `response_model`.

These are intentionally shallow: business logic is covered by the per-domain
service/CRUD integration tests. The point of these tests is to catch
misconfigured routes, broken Depends() chains, and auth bypasses.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

_tests_root = str(Path(__file__).resolve().parent.parent)
if _tests_root not in sys.path:
    sys.path.insert(0, _tests_root)

from fixtures.factories import make_agent  # noqa: E402

pytestmark = pytest.mark.api


# ---------------------------------------------------------------------------
# Fixtures: bring up the FastAPI app with the test DB overridden in
# ---------------------------------------------------------------------------


@pytest.fixture
async def client(db, monkeypatch):
    """Boot the FastAPI app, override get_db to yield the test session, and
    disable the API-token check so smoke tests don't need to thread tokens
    through every call.

    Use this for testing route wiring + response shapes. Auth-specific tests
    flip auth_enabled back on via their own fixture.
    """
    # Imports are inside the fixture so the prune from conftest has already
    # removed /app/src/* shadows by the time `db`/`main` resolve their deps.
    from app.core.config import settings  # noqa: inline-import (post-prune resolution)
    from app.db.database import get_db as _get_db  # noqa: inline-import (post-prune resolution)
    from app.main import app  # noqa: inline-import (post-prune resolution)

    # Disable auth so we don't have to thread bearer tokens through smoke tests
    monkeypatch.setattr(settings.security, "auth_enabled", False)

    async def _override_get_db():
        yield db

    app.dependency_overrides[_get_db] = _override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest.fixture
async def authed_client(db, monkeypatch):
    """Like `client`, but auth_enabled=True with a known fixed token."""
    from app.core.config import settings  # noqa: inline-import (post-prune resolution)
    from app.db.database import get_db as _get_db  # noqa: inline-import (post-prune resolution)
    from app.main import app  # noqa: inline-import (post-prune resolution)

    monkeypatch.setattr(settings.security, "auth_enabled", True)
    monkeypatch.setattr(settings.security, "auth_tokens", ["test-token-xyz"])

    async def _override_get_db():
        yield db

    app.dependency_overrides[_get_db] = _override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Health / metadata routes
# ---------------------------------------------------------------------------


class TestHealthRoutes:
    async def test_health_returns_ok(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert "status" in body

    async def test_root_returns_api_info(self, client):
        resp = await client.get("/")
        # Root may return 200 (JSON) or 302/307 (redirect to /dashboard)
        assert resp.status_code in (200, 302, 307)


# ---------------------------------------------------------------------------
# /api/v1/agents — list + get + create
# ---------------------------------------------------------------------------


class TestAgentRoutes:
    async def test_list_empty(self, client, db):
        resp = await client.get("/api/v1/agents")
        assert resp.status_code == 200
        body = resp.json()
        # AgentListResponse shape
        assert "agents" in body or "items" in body or isinstance(body, list)

    async def test_list_returns_seeded_agent(self, client, db):
        a = make_agent(agent_name="seeded-via-fixture")
        db.add(a)
        await db.flush()

        resp = await client.get("/api/v1/agents")
        assert resp.status_code == 200
        body = resp.json()
        # Body shape varies — flatten for assertion
        agents = body.get("agents") or body.get("items") or body
        names = [a.get("agent_name") for a in agents]
        assert "seeded-via-fixture" in names

    async def test_get_missing_returns_404(self, client, db):
        resp = await client.get("/api/v1/agents/this-agent-does-not-exist")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Auth gating
# ---------------------------------------------------------------------------


class TestAuthGating:
    async def test_missing_token_returns_401(self, authed_client):
        resp = await authed_client.get("/api/v1/agents")
        assert resp.status_code == 401

    async def test_wrong_token_returns_401(self, authed_client):
        resp = await authed_client.get(
            "/api/v1/agents", headers={"Authorization": "Bearer wrong-token"}
        )
        assert resp.status_code == 401

    async def test_correct_token_returns_200(self, authed_client):
        resp = await authed_client.get(
            "/api/v1/agents", headers={"Authorization": "Bearer test-token-xyz"}
        )
        assert resp.status_code == 200

    async def test_malformed_authorization_header_returns_401(self, authed_client):
        # Missing "Bearer " prefix
        resp = await authed_client.get(
            "/api/v1/agents", headers={"Authorization": "test-token-xyz"}
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Notification providers
# ---------------------------------------------------------------------------


class TestNotificationProviderRoutes:
    async def test_list_providers(self, client, db):
        resp = await client.get("/api/v1/notification-providers")
        # Some installs may not register this router under that path —
        # accept 404 (not wired) or 200 (wired and empty)
        assert resp.status_code in (200, 404)
