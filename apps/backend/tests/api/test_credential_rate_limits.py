"""Credential routes refuse past their limit, not just declare one.

`POST /api/v1/agents/{id}/recover-key` bcrypt-checks a submitted registration
key on every call. Unlimited, one address could guess keys forever; it now
shares the registration limit (5/hour per IP), and the sixth attempt is a 429
whatever the key, because a guess that fails still counts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app
from httpx import ASGITransport, AsyncClient
from limits import parse
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def client(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    limiter.reset()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            yield c
    finally:
        # The limiter's store outlives the test: leave it empty for the next one.
        limiter.reset()


async def test_key_recovery_refuses_once_the_limit_is_spent(
    client: AsyncClient,
) -> None:
    allowed = parse(settings.security.registration_rate_limit).amount
    url = f"/api/v1/agents/{uuid4()}/recover-key"
    headers = {"Authorization": "Bearer not-a-real-registration-key"}

    statuses = [
        (await client.post(url, headers=headers)).status_code
        for _ in range(allowed + 1)
    ]

    assert 429 not in statuses[:allowed], statuses
    assert statuses[allowed] == 429, statuses
