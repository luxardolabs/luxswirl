"""An agent's artifact upload answers the stored metadata, never the bytes.

An upload for a check that does not exist is refused (404).
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.db.database import get_db
from app.main import app
from app.models.check_artifact_model import CheckArtifact
from app.models.enum_model import CheckArtifactType
from fixtures.factories import make_agent, make_check, make_check_result
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

PNG = b"\x89PNG-not-really-but-bytes"


@pytest_asyncio.fixture
async def client(db: AsyncSession, monkeypatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings.security, "auth_enabled", False)

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        yield db
        await db.flush()

    app.dependency_overrides[get_db] = _override_get_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest_asyncio.fixture
async def artifact(db: AsyncSession) -> CheckArtifact:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(agent_id=agent.id)
    db.add(check)
    await db.flush()
    result = make_check_result(check_id=check.id, agent_id=agent.id)
    db.add(result)
    await db.flush()
    row = CheckArtifact(
        id=uuid4(),
        check_id=check.id,
        check_result_id=result.id,
        check_result_timestamp=result.timestamp,
        artifact_type=CheckArtifactType.SCREENSHOT,
        content_type="image/png",
        filename="shot.png",
        size_bytes=len(PNG),
        data=PNG,
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    db.add(row)
    await db.flush()
    return row


async def test_upload_answers_metadata_and_refuses_an_unknown_check(
    client: AsyncClient, artifact: CheckArtifact
) -> None:
    payload = {
        "check_id": str(artifact.check_id),
        "check_result_id": str(artifact.check_result_id),
        "check_result_timestamp": artifact.check_result_timestamp.isoformat(),
        "artifact_type": "screenshot",
        "content_type": "image/png",
        "filename": "second.png",
        "data_base64": base64.b64encode(PNG).decode(),
    }

    created = await client.post("/api/v1/artifacts", json=payload)
    unknown = await client.post(
        "/api/v1/artifacts", json={**payload, "check_id": str(uuid4())}
    )

    assert created.status_code == 201, created.text
    assert created.json()["size_bytes"] == len(PNG)
    assert "data" not in created.json()
    assert unknown.status_code == 404
