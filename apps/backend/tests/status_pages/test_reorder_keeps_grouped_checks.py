"""A status-page reorder never loses a check a container group names.

The editor saves an order by sending every group's checks as the manage page
drew them, so a grouped check the page cannot describe (its agent archived)
is still drawn, as "Check #id", and the server refuses a reorder that drops
a check that still exists. A check that no longer exists may be dropped.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.main import app
from app.models.status_page_model import StatusPage
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.status_page_core_service import StatusPageCoreService
from fixtures.factories import make_agent, make_check, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

HX = {"HX-Request": "true"}


@pytest_asyncio.fixture
async def client(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    user = make_user(role="editor")
    db.add(user)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, user.id)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as c:
        c.cookies.set(settings.security.session_cookie_name, token)
        yield c


async def _check_ids(db: AsyncSession, n: int, **agent: Any) -> list[str]:
    owner = make_agent(**agent)
    db.add(owner)
    await db.flush()
    checks = [make_check(agent_id=owner.id) for _ in range(n)]
    db.add_all(checks)
    await db.flush()
    return [str(c.id) for c in checks]


async def _page(db: AsyncSession, items: list[dict[str, Any]]) -> UUID:
    for order, item in enumerate(items):
        item["order"] = order
    page = StatusPage(name="page", slug=f"p-{uuid4().hex[:8]}", items=items)
    db.add(page)
    await db.flush()
    return page.id


async def _stored(db: AsyncSession, page_id: UUID) -> list[dict[str, Any]]:
    page = await StatusPageCoreService.get_status_page(db, page_id)
    assert page is not None
    return page.items


async def _drawn(client: AsyncClient, page_id: UUID) -> list[dict[str, Any]]:
    """The reorder the editor sends for the manage page as drawn, unchanged."""
    resp = await client.get(
        app.url_path_for("status_page_items.index", status_page_id=str(page_id)),
        headers=HX,
    )
    assert resp.status_code == 200, resp.text[:300]
    html = resp.text
    tops = list(
        re.finditer(
            r'data-item-index="(\d+)" data-item-type="(group|check)"'
            r'(?: data-check-id="([^"]*)"| data-rename-url)',
            html,
        )
    )
    drawn: list[dict[str, Any]] = []
    for n, top in enumerate(tops):
        index, kind, check_id = int(top.group(1)), top.group(2), top.group(3)
        if kind == "group":
            end = tops[n + 1].start() if n + 1 < len(tops) else len(html)
            checks = re.findall(
                r'group-check dashboard-item" data-item-type="check" '
                r'data-check-id="([^"]*)"',
                html[top.end() : end],
            )
            drawn.append({"oldIndex": index, "type": "group", "checks": checks})
        else:
            drawn.append({"oldIndex": index, "type": "check", "checkId": check_id})
    return drawn


async def _reorder(
    client: AsyncClient, page_id: UUID, items: list[dict[str, Any]]
) -> int:
    resp = await client.post(
        app.url_path_for("status_page_items.reorder", status_page_id=str(page_id)),
        data={"items_json": json.dumps(items)},
        headers=HX,
    )
    return resp.status_code


@pytest.mark.parametrize("sort_by", ["manual", "name"])
async def test_a_grouped_check_of_an_archived_agent_survives_a_reorder(
    client: AsyncClient, db: AsyncSession, sort_by: str
) -> None:
    [archived] = await _check_ids(db, 1, approval_status="archived")
    live, top = await _check_ids(db, 2)
    page_id = await _page(
        db,
        [
            {
                "type": "group",
                "name": "g",
                "checks": [archived, live],
                "sort_by": sort_by,
            },
            {"type": "check", "check_id": top},
        ],
    )

    drawn = await _drawn(client, page_id)
    drawn.reverse()

    assert await _reorder(client, page_id, drawn) == 200
    [moved, group] = await _stored(db, page_id)
    assert moved["check_id"] == top
    assert sorted(group["checks"]) == sorted([archived, live])


async def test_a_reorder_cannot_drop_a_grouped_check(db: AsyncSession) -> None:
    kept, dropped = await _check_ids(db, 2)
    page_id = await _page(
        db, [{"type": "group", "name": "g", "checks": [kept, dropped]}]
    )

    with pytest.raises(ValueError, match="leaves out"):
        await StatusPageCoreService.reorder_all_items(
            db, page_id, [{"type": "group", "oldIndex": 0, "checks": [kept]}]
        )
    assert (await _stored(db, page_id))[0]["checks"] == [kept, dropped]


async def test_a_grouped_check_may_move_to_another_group(db: AsyncSession) -> None:
    moved, other = await _check_ids(db, 2)
    page_id = await _page(
        db,
        [
            {"type": "group", "name": "a", "checks": [moved, other]},
            {"type": "group", "name": "b", "checks": []},
        ],
    )

    await StatusPageCoreService.reorder_all_items(
        db,
        page_id,
        [
            {"type": "group", "oldIndex": 0, "checks": [other]},
            {"type": "group", "oldIndex": 1, "checks": [moved]},
        ],
    )

    assert [i["checks"] for i in await _stored(db, page_id)] == [[other], [moved]]


async def test_a_check_that_no_longer_exists_may_be_dropped(db: AsyncSession) -> None:
    [kept] = await _check_ids(db, 1)
    gone = str(uuid4())
    page_id = await _page(db, [{"type": "group", "name": "g", "checks": [kept, gone]}])

    await StatusPageCoreService.reorder_all_items(
        db, page_id, [{"type": "group", "oldIndex": 0, "checks": [kept]}]
    )

    assert (await _stored(db, page_id))[0]["checks"] == [kept]
