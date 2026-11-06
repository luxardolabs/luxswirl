"""Search matches a check's target as it is shown, with its credentials masked.

A viewer reads a target masked, and a search that matched the stored value would
answer whether a guess at the password is right, one character at a time. Each
surface here is one a viewer reaches.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.rate_limit import limiter
from app.crud.check_crud import CheckCRUD
from app.main import app
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.api_token_core_service import ApiTokenCoreService
from app.services.core.auth_core_service import AuthCoreService
from app.services.views.status_view_service import StatusViewService
from fixtures.factories import make_agent, make_check, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

SECRET = "s3cret-Pw9"
TARGETS = [
    f"postgresql://monitor:{SECRET}@db.internal:5432/app",
    f"https://db.internal/status?token={SECRET}",
    f"host=db.internal user=monitor password={SECRET} dbname=app",
    f"Server=db.internal;Uid=monitor;Pwd={SECRET};Database=app",
]
# What a guess at each part of a credential looks like. A username is in the masked
# userinfo too.
GUESSES = [SECRET, "s3cret", f"monitor:{SECRET[:3]}"]


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    limiter.reset()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    ) as c:
        yield c


async def _check(db: AsyncSession, target: str) -> None:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    db.add(
        make_check(
            agent_id=agent.id,
            display_name="the-check",
            check_type="postgres",
            target=target,
        )
    )
    await db.flush()


async def _token_headers(db: AsyncSession, role: str) -> dict[str, str]:
    user = make_user(role=role)
    db.add(user)
    await db.flush()
    _, plaintext = await ApiTokenCoreService.create_token(
        db, user.id, ApiTokenCreate(name=role, scopes=["*"])
    )
    return {"Authorization": f"Bearer {plaintext}"}


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("guess", GUESSES)
async def test_no_search_finds_a_check_by_its_credential(
    db: AsyncSession, target: str, guess: str
) -> None:
    await _check(db, target)

    listed, _ = await CheckCRUD.list_paginated(db, search=guess)
    health, _ = await CheckCRUD.list_page_with_health(db, search=guess)
    _, dashboard = await StatusViewService.get_all_checks_status(db=db, search=guess)

    assert (list(listed), list(health), dashboard) == ([], [], 0)


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("found_by", ["db.internal", "the-check"])
async def test_the_rest_of_the_target_and_the_name_are_still_found(
    db: AsyncSession, target: str, found_by: str
) -> None:
    await _check(db, target)

    listed, _ = await CheckCRUD.list_paginated(db, search=found_by)
    _, dashboard = await StatusViewService.get_all_checks_status(db=db, search=found_by)

    assert (len(listed), dashboard) == (1, 1)


@pytest.mark.parametrize("role", ["viewer", "editor", "admin"])
async def test_the_admin_api_answers_a_right_guess_as_a_wrong_one(
    client: AsyncClient, db: AsyncSession, role: str
) -> None:
    await _check(db, TARGETS[0])
    headers = await _token_headers(db, role)

    right = await client.get(
        "/api/v1/admin/checks", params={"search": SECRET[:6]}, headers=headers
    )
    host = await client.get(
        "/api/v1/admin/checks", params={"search": "db.internal"}, headers=headers
    )

    assert right.status_code == host.status_code == 200, right.text
    assert right.json()["items"] == []
    assert len(host.json()["items"]) == 1


async def test_the_dashboard_answers_a_right_guess_as_a_wrong_one(
    client: AsyncClient, db: AsyncSession
) -> None:
    await _check(db, TARGETS[0])
    viewer = make_user(role="viewer")
    db.add(viewer)
    await db.flush()
    _, session = await AuthCoreService().create_session(db, viewer.id)
    client.cookies.set(settings.security.session_cookie_name, session)

    right = await client.get("/dashboard", params={"search": SECRET[:6]})
    host = await client.get("/dashboard", params={"search": "db.internal"})

    assert right.status_code == host.status_code == 200
    assert "the-check" not in right.text
    assert "the-check" in host.text
