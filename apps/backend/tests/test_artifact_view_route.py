"""GET /artifacts/{id}/view: signed-in users only, and a header the filename cannot break.

Artifacts are synthetic-check screenshots and Playwright traces, which record
the pages a check drove (login forms included). The route served them to anyone
holding the UUID, and spliced the agent-supplied filename into
Content-Disposition unescaped.
"""

from __future__ import annotations

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
from fixtures.factories import make_agent, make_check, make_user
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


async def _artifact(db: AsyncSession) -> CheckArtifact:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(agent_id=agent.id, check_type="synthetic")
    db.add(check)
    await db.flush()
    now = utc_now()
    artifact = CheckArtifact(
        id=uuid4(),
        check_id=check.id,
        check_result_id=uuid4(),
        check_result_timestamp=now,
        artifact_type=CheckArtifactType.SCREENSHOT,
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


async def test_an_anonymous_request_gets_no_artifact_bytes(
    client: AsyncClient, db: AsyncSession
) -> None:
    artifact = await _artifact(db)

    resp = await client.get(f"/artifacts/{artifact.id}/view")

    assert resp.status_code in (302, 303, 307, 401), resp.status_code
    assert b"secret" not in resp.content


async def test_a_signed_in_user_gets_it_with_a_safe_disposition(
    client: AsyncClient, db: AsyncSession
) -> None:
    artifact = await _artifact(db)
    user = make_user(role="viewer")
    db.add(user)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, user.id)
    client.cookies.set(settings.security.session_cookie_name, token)

    resp = await client.get(f"/artifacts/{artifact.id}/view")

    assert resp.status_code == 200
    assert resp.content == b"secret"
    disposition = resp.headers["content-disposition"]
    assert disposition.count("filename=") == 1
    assert 'filename="evil.html"' not in disposition
