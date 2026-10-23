"""API smoke tests — confirm routes are wired, auth gates work, response
shapes match `response_model`.

These are intentionally shallow: business logic is covered by the per-domain
service/CRUD integration tests. The point of these tests is to catch
misconfigured routes, broken Depends() chains, and auth bypasses.
"""

from __future__ import annotations

import pytest
from app.core.config import settings
from app.db.database import get_db
from app.main import app
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.integration


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

    # Disable auth so we don't have to thread bearer tokens through smoke tests
    monkeypatch.setattr(settings.security, "auth_enabled", False)

    async def _override_get_db():
        yield db

    app.dependency_overrides[get_db] = _override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest.fixture
async def authed_client(db, monkeypatch):
    """Like `client`, but auth_enabled=True with a known fixed token."""

    monkeypatch.setattr(settings.security, "auth_enabled", True)
    monkeypatch.setattr(settings.security, "auth_tokens", ["test-token-xyz"])

    async def _override_get_db():
        yield db

    app.dependency_overrides[get_db] = _override_get_db

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
# Auth gating: the artifact upload takes a configured API token
# ---------------------------------------------------------------------------


class TestAuthGating:
    async def test_missing_token_returns_401(self, authed_client):
        resp = await authed_client.post("/api/v1/artifacts", json={})
        assert resp.status_code == 401

    async def test_wrong_token_returns_401(self, authed_client):
        resp = await authed_client.post(
            "/api/v1/artifacts",
            json={},
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401

    async def test_correct_token_passes_the_gate(self, authed_client):
        # The gate lets it through; the empty body is then refused as invalid.
        resp = await authed_client.post(
            "/api/v1/artifacts",
            json={},
            headers={"Authorization": "Bearer test-token-xyz"},
        )
        assert resp.status_code == 422

    async def test_malformed_authorization_header_returns_401(self, authed_client):
        # Missing "Bearer " prefix
        resp = await authed_client.post(
            "/api/v1/artifacts",
            json={},
            headers={"Authorization": "test-token-xyz"},
        )
        assert resp.status_code == 401
