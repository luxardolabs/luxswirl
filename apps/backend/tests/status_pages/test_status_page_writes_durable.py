"""Every status-page write survives its owner's commit.

Durability locks for moving StatusPageCoreService session writes into crud
(`fw.no_session_mutation_outside_crud`). Each test owns the session, calls the
real service method, commits, and reads back on a SEPARATE connection. The
items list is JSON edited in place, so these also prove each edit is seen as a
change and written, not just held on the instance.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from app.models.status_page_model import StatusPage
from app.schemas.status_page_schema import StatusPageCreate, StatusPageUpdate
from app.services.core.status_page_core_service import StatusPageCoreService
from fixtures.factories import make_agent, make_check
from sqlalchemy import select

pytestmark = pytest.mark.integration


async def _stored(maker, page_id: UUID) -> StatusPage | None:
    async with maker() as other:  # separate connection: only committed rows
        return await other.scalar(select(StatusPage).where(StatusPage.id == page_id))


async def _items(maker, page_id: UUID) -> list[dict[str, Any]]:
    page = await _stored(maker, page_id)
    assert page is not None
    return page.items


async def _page(maker, items: list[dict[str, Any]] | None = None) -> UUID:
    async with maker() as s:
        page = await StatusPageCoreService.create_status_page(
            s,
            StatusPageCreate(
                name="durable", slug=f"sp-{uuid4().hex[:8]}", items=items or []
            ),
        )
        await s.commit()
    return page.id


async def _checks(maker, n: int) -> list[UUID]:
    agent = make_agent()
    checks = [make_check(agent_id=agent.id) for _ in range(n)]
    async with maker() as s:
        s.add(agent)
        await s.flush()
        s.add_all(checks)
        await s.commit()
    return [c.id for c in checks]


async def _page_with_group(maker) -> UUID:
    page_id = await _page(maker)
    async with maker() as s:
        await StatusPageCoreService.add_group_to_page(
            s, page_id, "Production", filter={"check_type": "http"}
        )
        await s.commit()
    return page_id


async def test_create_update_and_delete_are_committed(worker_db) -> None:
    page_id = await _page(worker_db)
    assert await _stored(worker_db, page_id) is not None

    async with worker_db() as s:
        await StatusPageCoreService.update_status_page(
            s, page_id, StatusPageUpdate(name="renamed")
        )
        await s.commit()
    stored = await _stored(worker_db, page_id)
    assert stored is not None and stored.name == "renamed"

    async with worker_db() as s:
        await StatusPageCoreService.delete_status_page(s, page_id)
        await s.commit()
    assert await _stored(worker_db, page_id) is None


async def test_an_added_check_is_committed(worker_db) -> None:
    (check_id,) = await _checks(worker_db, 1)
    page_id = await _page(worker_db)

    async with worker_db() as s:
        await StatusPageCoreService.add_check_to_page(s, page_id, check_id)
        await s.commit()

    assert [i["check_id"] for i in await _items(worker_db, page_id)] == [str(check_id)]


async def test_an_added_group_is_committed(worker_db) -> None:
    page_id = await _page_with_group(worker_db)

    (item,) = await _items(worker_db, page_id)
    assert item["type"] == "group" and item["name"] == "Production"


async def test_a_removed_item_is_committed(worker_db) -> None:
    page_id = await _page_with_group(worker_db)

    async with worker_db() as s:
        await StatusPageCoreService.remove_item_from_page(s, page_id, 0)
        await s.commit()

    assert await _items(worker_db, page_id) == []


async def _two_check_page(maker) -> tuple[UUID, list[UUID]]:
    ids = await _checks(maker, 2)
    page_id = await _page(maker)
    for check_id in ids:
        async with maker() as s:
            await StatusPageCoreService.add_check_to_page(s, page_id, check_id)
            await s.commit()
    return page_id, ids


async def test_a_moved_item_is_committed(worker_db) -> None:
    page_id, (first, second) = await _two_check_page(worker_db)

    async with worker_db() as s:
        await StatusPageCoreService.reorder_item(s, page_id, 0, 1)
        await s.commit()

    assert [i["check_id"] for i in await _items(worker_db, page_id)] == [
        str(second),
        str(first),
    ]


async def test_a_full_reorder_is_committed(worker_db) -> None:
    page_id, (first, second) = await _two_check_page(worker_db)

    async with worker_db() as s:
        await StatusPageCoreService.reorder_all_items(
            s,
            page_id,
            [{"oldIndex": 1, "type": "check"}, {"oldIndex": 0, "type": "check"}],
        )
        await s.commit()

    assert [i["check_id"] for i in await _items(worker_db, page_id)] == [
        str(second),
        str(first),
    ]


async def test_a_renamed_group_is_committed(worker_db) -> None:
    page_id = await _page_with_group(worker_db)

    async with worker_db() as s:
        await StatusPageCoreService.rename_group(s, page_id, 0, "Staging")
        await s.commit()

    (item,) = await _items(worker_db, page_id)
    assert item["name"] == "Staging"


async def test_new_group_filters_are_committed(worker_db) -> None:
    page_id = await _page_with_group(worker_db)

    async with worker_db() as s:
        await StatusPageCoreService.update_group_filters(
            s, page_id, 0, {"check_type": "tcp"}
        )
        await s.commit()

    (item,) = await _items(worker_db, page_id)
    assert item["filter"]["check_type"] == "tcp"


async def test_a_new_group_sort_is_committed(worker_db) -> None:
    page_id = await _page_with_group(worker_db)

    async with worker_db() as s:
        await StatusPageCoreService.update_group_sort(s, page_id, 0, "name", "desc")
        await s.commit()

    (item,) = await _items(worker_db, page_id)
    assert (item.get("sort_by"), item.get("sort_direction")) == ("name", "desc")
