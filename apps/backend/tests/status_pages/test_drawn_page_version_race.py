"""Two editors' positional edits of one page are serialised: each applies to the page it saw, or is refused.

The version check takes the page's row lock in the edit's own transaction, so
another editor's edit cannot commit between the check and the write; it waits
for the lock, then finds the page moved on and is refused.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
from app.core.config import settings
from app.core.exceptions import StalePageException
from app.main import app
from app.models.status_page_model import StatusPage
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.status_page_core_service import StatusPageCoreService
from app.services.views.status_pages_view_service import StatusPagesViewService
from fixtures.factories import make_agent, make_check, make_user
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select

pytestmark = pytest.mark.integration

HX = {"HX-Request": "true"}


async def _seed(maker: Any) -> tuple[str, UUID]:
    async with maker() as s:
        user = make_user(role="editor")
        s.add(user)
        agent = make_agent()
        s.add(agent)
        await s.flush()
        check = make_check(agent_id=agent.id)
        s.add(check)
        await s.flush()
        _, token = await AuthCoreService().create_session(s, user.id)
        items: list[dict[str, Any]] = [
            {"type": "check", "check_id": str(check.id), "order": 0},
            {"type": "group", "name": "G1", "filter": {}, "order": 1},
            {"type": "group", "name": "G2", "checks": [], "order": 2},
        ]
        page = StatusPage(name="p", slug=f"p-{uuid4().hex[:8]}", items=items)
        s.add(page)
        await s.commit()
        return token, page.id


async def _stored(maker: Any, page_id: UUID) -> StatusPage:
    async with maker() as s:
        page = await s.scalar(select(StatusPage).where(StatusPage.id == page_id))
        assert page is not None
        return page


async def _rename_group_1(token: str, page_id: UUID, drawn: str) -> Response:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as c:
        c.cookies.set(settings.security.session_cookie_name, token)
        return await c.patch(
            app.url_path_for(
                "status_page_groups.rename", status_page_id=str(page_id), item_index=1
            ),
            data={"name": "renamed"},
            headers={**HX, "X-Page-Version": drawn},
        )


async def test_an_edit_racing_another_applies_to_the_page_it_saw(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    token, page_id = await _seed(worker_db)
    drawn_page = await _stored(worker_db, page_id)
    drawn = drawn_page.updated_at

    async def other_editor_removes_item_0() -> None:
        # Drawn from the same page; its check waits for this edit's lock.
        async with worker_db() as other:
            await StatusPageCoreService.require_drawn_version(other, page_id, drawn)
            await StatusPageCoreService.remove_item_from_page(other, page_id, 0)
            await other.commit()

    other: list[asyncio.Task[None]] = []
    original = StatusPagesViewService.require_drawn_version

    async def check_then_race(db: Any, pid: UUID, version: Any) -> None:
        await original(db, pid, version)
        other.append(asyncio.create_task(other_editor_removes_item_0()))
        await asyncio.sleep(0.3)  # the other edit reaches the lock and waits

    monkeypatch.setattr(
        StatusPagesViewService, "require_drawn_version", staticmethod(check_then_race)
    )

    resp = await _rename_group_1(token, page_id, drawn.isoformat())

    assert resp.status_code == 200
    with pytest.raises(StalePageException):
        await other[0]
    names = [
        i.get("name") or i["type"] for i in (await _stored(worker_db, page_id)).items
    ]
    assert names == ["check", "renamed", "G2"]


async def test_an_edit_after_another_committed_is_refused(worker_db: Any) -> None:
    token, page_id = await _seed(worker_db)
    drawn = (await _stored(worker_db, page_id)).updated_at.isoformat()
    async with worker_db() as other:
        await StatusPageCoreService.remove_item_from_page(other, page_id, 0)
        await other.commit()

    resp = await _rename_group_1(token, page_id, drawn)

    assert resp.status_code == 409
    names = [i.get("name") for i in (await _stored(worker_db, page_id)).items]
    assert names == ["G1", "G2"]
