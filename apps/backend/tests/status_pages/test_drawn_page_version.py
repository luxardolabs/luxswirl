"""An editor edit that names an item by its position applies only to the page it was drawn from.

The editor's remove, reorder, rename, group-filter and group-sort routes name
an item by its index. Each request carries the page version (`updated_at`) the
browser drew, in X-Page-Version; the server refuses one the page has since
moved past (409, a toast, nothing changed), so a request queued behind another
change, or made by a second editor, cannot change the item that now sits at
that index.
"""

from __future__ import annotations

import json
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
from httpx import ASGITransport, AsyncClient, Response
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


async def _check_id(db: AsyncSession) -> str:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(agent_id=agent.id)
    db.add(check)
    await db.flush()
    return str(check.id)


async def _page(db: AsyncSession) -> UUID:
    """A check X, then groups G1 (filtered) and G2 (a container)."""
    x = await _check_id(db)
    items: list[dict[str, Any]] = [
        {"type": "check", "check_id": x, "order": 0},
        {"type": "group", "name": "G1", "filter": {}, "order": 1},
        {"type": "group", "name": "G2", "checks": [], "order": 2},
    ]
    page = StatusPage(name="page", slug=f"p-{uuid4().hex[:8]}", items=items)
    db.add(page)
    await db.flush()
    return page.id


async def _version(db: AsyncSession, page_id: UUID) -> str:
    page = await StatusPageCoreService.get_status_page(db, page_id)
    assert page is not None
    return page.updated_at.isoformat()


async def _items(db: AsyncSession, page_id: UUID) -> list[dict[str, Any]]:
    page = await StatusPageCoreService.get_status_page(db, page_id)
    assert page is not None
    return page.items


def _edit(client: AsyncClient, page_id: UUID, action: str, version: str | None) -> Any:
    """One positional edit of item 1 (G1), as the editor sends it."""
    pid = str(page_id)
    headers = {**HX, **({"X-Page-Version": version} if version is not None else {})}
    url = app.url_path_for
    if action == "remove":
        return client.delete(
            url("status_page_items.delete", status_page_id=pid, item_index=1),
            headers=headers,
        )
    if action == "reorder":
        order = [
            {"oldIndex": 1, "type": "group", "checks": []},
            {"oldIndex": 0, "type": "check"},
            {"oldIndex": 2, "type": "group", "checks": []},
        ]
        return client.post(
            url("status_page_items.reorder", status_page_id=pid),
            data={"items_json": json.dumps(order)},
            headers=headers,
        )
    if action == "rename":
        return client.patch(
            url("status_page_groups.rename", status_page_id=pid, item_index=1),
            data={"name": "renamed"},
            headers=headers,
        )
    if action == "filters":
        return client.patch(
            url("status_page_groups.update_filters", status_page_id=pid, item_index=1),
            data={"filter_json": json.dumps({"search": "web"})},
            headers=headers,
        )
    return client.patch(
        url("status_page_groups.sort", status_page_id=pid, item_index=1),
        data={"sort_by": "name", "sort_direction": "asc"},
        headers=headers,
    )


ACTIONS = ["remove", "reorder", "rename", "filters", "sort"]


@pytest.mark.parametrize("action", ACTIONS)
async def test_an_edit_on_the_current_page_applies(
    client: AsyncClient, db: AsyncSession, action: str
) -> None:
    page_id = await _page(db)
    before = await _items(db, page_id)

    resp: Response = await _edit(client, page_id, action, await _version(db, page_id))

    assert resp.status_code == 200, resp.text[:300]
    assert await _items(db, page_id) != before


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("version", ["stale", "missing"])
async def test_an_edit_on_an_older_page_is_refused_and_changes_nothing(
    client: AsyncClient, db: AsyncSession, action: str, version: str
) -> None:
    page_id = await _page(db)
    drawn = await _version(db, page_id)
    # Another editor's change moves the page on.
    await StatusPageCoreService.rename_group(db, page_id, 2, "moved on")
    before = await _items(db, page_id)

    resp: Response = await _edit(
        client, page_id, action, drawn if version == "stale" else None
    )

    assert resp.status_code == 409
    assert resp.headers.get("HX-Reswap") == "none"
    assert json.loads(resp.headers["HX-Trigger"])["showToast"]["type"] == "error"
    assert await _items(db, page_id) == before


async def test_a_rename_queued_behind_a_remove_cannot_rename_the_next_group(
    client: AsyncClient, db: AsyncSession
) -> None:
    page_id = await _page(db)
    drawn = await _version(db, page_id)

    removed = await client.delete(
        app.url_path_for(
            "status_page_items.delete", status_page_id=str(page_id), item_index=0
        ),
        headers={**HX, "X-Page-Version": drawn},
    )
    renamed = await _edit(client, page_id, "rename", drawn)

    assert removed.status_code == 200
    assert renamed.status_code == 409
    assert [i.get("name") for i in await _items(db, page_id)] == ["G1", "G2"]


async def test_an_unauthenticated_edit_is_refused_before_the_page_is_read(
    db: AsyncSession,
) -> None:
    page_id = await _page(db)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as anonymous:
        resp = await _edit(anonymous, page_id, "rename", None)

    assert resp.status_code != 409
    assert [i.get("name") for i in await _items(db, page_id)][1:] == ["G1", "G2"]
