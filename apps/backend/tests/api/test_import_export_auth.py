"""The import/export API must be behind the same Bearer token as every other
v1 route.

All three routes shipped with no authenticating dependency, so anyone who could
reach the API could read an agent's entire check inventory — internal hostnames,
ports and targets — and create or overwrite its checks. Every sibling v1 route
returns 401 to an anonymous caller; these returned 200.
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

_tests_root = str(Path(__file__).resolve().parent.parent)
if _tests_root not in sys.path:
    sys.path.insert(0, _tests_root)

from fixtures.factories import make_agent  # noqa: E402

pytestmark = pytest.mark.api

TOKEN = "test-token-xyz"


@pytest.fixture
async def authed_client(db, monkeypatch):
    """The app with the API token check ON and a known token."""
    from app.core.config import settings  # noqa: inline-import (post-prune resolution)
    from app.db.database import get_db as _get_db  # noqa: inline-import (post-prune resolution)
    from app.main import app  # noqa: inline-import (post-prune resolution)

    monkeypatch.setattr(settings.security, "auth_enabled", True)
    monkeypatch.setattr(settings.security, "auth_tokens", [TOKEN])

    async def _override_get_db():
        yield db

    app.dependency_overrides[_get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


AGENT_ID = UUID("00000000-0000-0000-0000-0000000000ab")


def _routes(agent_id=AGENT_ID):
    """(method, url, json) for each import/export route."""
    return [
        ("GET", f"/api/v1/import-export/export/{agent_id}", None),
        ("GET", "/api/v1/import-export/export-agent-config", None),
        (
            "POST",
            "/api/v1/import-export/import",
            {"agent_id": str(agent_id), "overwrite": False, "checks": []},
        ),
    ]


@pytest.mark.parametrize("method,url,body", _routes(), ids=lambda v: str(v)[:40])
async def test_anonymous_is_rejected(authed_client, method, url, body):
    resp = await authed_client.request(method, url, json=body)
    assert resp.status_code == 401, f"{method} {url} answered an anonymous caller"


@pytest.mark.parametrize("method,url,body", _routes(), ids=lambda v: str(v)[:40])
async def test_a_wrong_token_is_rejected(authed_client, method, url, body):
    resp = await authed_client.request(
        method, url, json=body, headers={"Authorization": "Bearer not-the-token"}
    )
    assert resp.status_code == 401


async def test_a_valid_token_is_accepted(db, authed_client):
    """The gate must not have broken the route for a legitimate caller."""
    agent = make_agent()
    db.add(agent)
    await db.flush()

    resp = await authed_client.get(
        f"/api/v1/import-export/export/{agent.id}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert resp.status_code == 200
    assert resp.json()["agent_id"] == str(agent.id)


async def test_import_reports_its_result(db, authed_client):
    """The success log used to splat `created` into LogRecord's reserved field,
    so every successful import raised and came back a 500."""
    agent = make_agent()
    db.add(agent)
    await db.flush()

    resp = await authed_client.post(
        "/api/v1/import-export/import",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={
            "agent_id": str(agent.id),
            "overwrite": False,
            "checks": [{"name": "probe", "check_type": "ping", "target": "127.0.0.1"}],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["created"] == 1
