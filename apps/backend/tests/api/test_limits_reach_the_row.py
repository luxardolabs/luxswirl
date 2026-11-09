"""A length limit holds for what is stored, not only for what was sent.

Metrics were measured as compact JSON with characters as they are, then stored
by `json.dumps` with every non-ASCII character escaped: 60,000 emoji passed the
65,535-character limit and were stored as 720,012 characters. And the encrypted
check columns hold the Fernet token of the UTF-8 bytes, so a 416-character
Cyrillic target passed the 512-character limit and failed at the column with a
500. Both are now measured as the row holds them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.encrypted_types import EncryptedString, encrypted_length
from app.core.rate_limit import limiter
from app.main import app
from app.models.check_model import Check
from app.models.check_result_model import CheckResult
from app.schemas.api_token_schema import ApiTokenCreate
from app.schemas.base_schema import ENCRYPTED_COLUMN_CHARS
from app.schemas.check_result_schema import MAX_METRICS_CHARS
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.api_token_core_service import ApiTokenCoreService
from cryptography.fernet import Fernet
from fixtures.factories import make_agent, make_check, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def client(db: AsyncSession, monkeypatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    limiter.reset()
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url=f"https://{settings.allowed_hosts[0]}",
    ) as c:
        yield c
    limiter.reset()


@pytest_asyncio.fixture
async def approved(db: AsyncSession) -> tuple[UUID, str]:
    agent = make_agent(approval_status="pending")
    db.add(agent)
    await db.flush()
    _, key = await AgentCoreService.approve_agent(db, agent.id, actor_is_admin=True)
    return agent.id, key


async def _admin(db: AsyncSession) -> dict[str, str]:
    user = make_user(role="admin")
    db.add(user)
    await db.flush()
    _, plaintext = await ApiTokenCoreService.create_token(
        db, user.id, ApiTokenCreate(name="adm", scopes=["*"])
    )
    return {"Authorization": f"Bearer {plaintext}"}


def _wire(check_id: UUID, **kw: Any) -> dict[str, Any]:
    return {
        "check_id": str(check_id),
        "display_name": "probe",
        "check_type": "http",
        "target": "https://example.com",
        "timestamp": utc_now().isoformat(),
        "success": True,
        "latency_ms": 5.0,
        **kw,
    }


async def test_stored_metrics_are_held_to_the_limit(
    client: AsyncClient, db: AsyncSession, approved: tuple[UUID, str]
) -> None:
    agent_id, key = approved
    check = make_check(agent_id=agent_id, check_type="http", target="https://a")
    db.add(check)
    await db.flush()
    metrics = {"blob": "\U0001f600" * 60_000}

    resp = await client.post(
        "/api/v1/agent/reports",
        json={"agent_id": str(agent_id), "checks": [_wire(check.id, metrics=metrics)]},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 202, resp.text
    stored = await db.scalar(
        select(CheckResult.metrics).where(CheckResult.check_id == check.id)
    )
    assert stored is not None
    assert len(stored) <= MAX_METRICS_CHARS, f"stored metrics are {len(stored)} chars"


@pytest.mark.parametrize("route", ["create", "update"])
async def test_a_multibyte_target_the_column_cannot_hold_is_refused(
    client: AsyncClient, db: AsyncSession, approved: tuple[UUID, str], route: str
) -> None:
    agent_id, _ = approved
    headers = await _admin(db)
    target = "http://10.1.2.3/" + "ж" * 400  # 416 characters, 816 bytes
    if route == "create":
        resp = await client.post(
            "/api/v1/admin/checks",
            json={
                "agent_id": str(agent_id),
                "display_name": "cyr",
                "check_type": "http",
                "target": target,
            },
            headers=headers,
        )
    else:
        check = make_check(agent_id=agent_id, check_type="http", target="http://a")
        db.add(check)
        await db.flush()
        resp = await client.patch(
            f"/api/v1/admin/checks/{check.id}", json={"target": target}, headers=headers
        )

    assert resp.status_code == 422, f"{resp.status_code} {resp.text[:300]}"


async def test_a_long_connection_string_is_not_held_to_the_target_column(
    client: AsyncClient, db: AsyncSession, approved: tuple[UUID, str]
) -> None:
    """A connection string is stored in check_config (unbounded encrypted text),
    so the 1,000-character limit it always had still holds, and an export of a
    check that has one still works."""
    agent_id, _ = approved
    headers = await _admin(db)
    dsn = "postgresql://monitor:" + "p" * 750 + "@db.internal:5432/app"

    created = await client.post(
        "/api/v1/admin/checks",
        json={
            "agent_id": str(agent_id),
            "display_name": "long dsn",
            "check_type": "postgres",
            "target": "db.internal",
            "connection_string": dsn,
            "query": "SELECT 1",
        },
        headers=headers,
    )
    exported = await client.get(
        f"/api/v1/admin/agents/{agent_id}/checks/export", headers=headers
    )

    assert created.status_code == 201, created.text[:300]
    assert exported.status_code == 200, exported.text[:300]


@pytest.mark.unit
def test_the_column_limit_is_the_models() -> None:
    column_type = Check.__table__.c["target"].type
    assert isinstance(column_type, EncryptedString)
    assert column_type.impl.length == ENCRYPTED_COLUMN_CHARS


@pytest.mark.unit
@pytest.mark.parametrize("text", ["", "a", "a" * 15, "a" * 16, "ж" * 343, "😀" * 171])
def test_the_measured_length_is_the_tokens(text: str) -> None:
    assert encrypted_length(text) == len(
        Fernet(Fernet.generate_key()).encrypt(text.encode())
    )
