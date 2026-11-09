"""Artifact bytes: editors and admins only, and a header the filename cannot break.

Artifacts are synthetic-check screenshots and Playwright traces, which record
the pages a check drove (login forms included), and bytes cannot be masked. So
a viewer, who reads every other credential masked, gets neither the bytes, the
trace viewer, nor a link to either in the check panel. The route also spliced
the agent-supplied filename into Content-Disposition unescaped.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.main import app
from app.models.check_artifact_model import CheckArtifact
from app.models.enum_model import CheckArtifactType
from app.services.core.auth_core_service import AuthCoreService
from fixtures.factories import make_agent, make_check, make_check_result, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

HOSTILE = 'shot.png"; filename="evil.html'


@pytest_asyncio.fixture
async def client(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as c:
        yield c


async def _artifact(
    db: AsyncSession, artifact_type: CheckArtifactType = CheckArtifactType.SCREENSHOT
) -> CheckArtifact:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(agent_id=agent.id, check_type="synthetic")
    db.add(check)
    await db.flush()
    result = make_check_result(
        check_id=check.id,
        agent_id=agent.id,
        metrics=json.dumps({"synthetic": {"status": "success"}}),
    )
    db.add(result)
    await db.flush()
    now = utc_now()
    artifact = CheckArtifact(
        id=uuid4(),
        check_id=check.id,
        check_result_id=result.id,
        check_result_timestamp=result.timestamp,
        artifact_type=artifact_type,
        content_type="image/png",
        filename=HOSTILE,
        size_bytes=6,
        data=b"secret",
        created_at=now,
        updated_at=now,
    )
    db.add(artifact)
    await db.flush()
    return artifact


async def _sign_in(client: AsyncClient, db: AsyncSession, role: str) -> None:
    user = make_user(role=role)
    db.add(user)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, user.id)
    client.cookies.set(settings.security.session_cookie_name, token)


async def test_an_anonymous_request_gets_no_artifact_bytes(
    client: AsyncClient, db: AsyncSession
) -> None:
    artifact = await _artifact(db)

    resp = await client.get(f"/artifacts/{artifact.id}/view")

    assert resp.status_code in (302, 303, 307, 401), resp.status_code
    assert b"secret" not in resp.content


async def test_a_viewer_gets_no_artifact_bytes(
    client: AsyncClient, db: AsyncSession
) -> None:
    artifact = await _artifact(db)
    await _sign_in(client, db, "viewer")

    resp = await client.get(f"/artifacts/{artifact.id}/view")

    assert resp.status_code in (302, 303, 307, 403), resp.status_code
    assert b"secret" not in resp.content


async def test_a_viewer_gets_no_trace_viewer(
    client: AsyncClient, db: AsyncSession
) -> None:
    artifact = await _artifact(db, CheckArtifactType.TRACE)
    await _sign_in(client, db, "viewer")

    resp = await client.get(f"/artifacts/{artifact.id}/viewer")

    assert resp.status_code in (302, 303, 307, 403), resp.status_code
    assert f"/artifacts/{artifact.id}/view" not in resp.text


@pytest.mark.parametrize("role", ["editor", "admin"])
async def test_an_editor_or_admin_gets_it_with_a_safe_disposition(
    client: AsyncClient, db: AsyncSession, role: str
) -> None:
    artifact = await _artifact(db)
    await _sign_in(client, db, role)

    resp = await client.get(f"/artifacts/{artifact.id}/view")

    assert resp.status_code == 200
    assert resp.content == b"secret"
    disposition = resp.headers["content-disposition"]
    assert disposition.count("filename=") == 1
    assert 'filename="evil.html"' not in disposition


@pytest.mark.parametrize(
    ("role", "linked"), [("viewer", False), ("editor", True), ("admin", True)]
)
async def test_the_check_panel_links_artifacts_for_editors_only(
    client: AsyncClient, db: AsyncSession, role: str, linked: bool
) -> None:
    screenshot = await _artifact(db)
    await _sign_in(client, db, role)

    resp = await client.get(f"/check/{screenshot.check_id}")

    assert resp.status_code == 200
    assert (f"/artifacts/{screenshot.id}/" in resp.text) is linked
