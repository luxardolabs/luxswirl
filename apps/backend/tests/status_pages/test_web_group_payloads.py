"""The web status-page editor's group payloads are held to the admin API's shapes.

The editor sends JSON in form fields: a group's filter, and the reordered
items with each container group's checks. Stored as sent, a group of the
wrong shape breaks every later render of its page, so each is validated as
the admin API validates a group (GroupFilter, a list of check UUIDs, a
GroupName) and stored in the same form.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.main import app
from app.models.check_model import Check
from app.schemas.status_page_schema import StatusPageCreate
from app.services.core.status_page_core_service import StatusPageCoreService
from fixtures.factories import make_agent, make_check
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def _page(db: AsyncSession, filter: dict[str, Any] | None) -> Any:
    page = await StatusPageCoreService.create_status_page(
        db, StatusPageCreate(name="page", slug=f"p-{uuid4().hex[:8]}")
    )
    await StatusPageCoreService.add_group_to_page(db, page.id, "group", filter)
    return page


async def _check_ids(db: AsyncSession, n: int = 1) -> list[str]:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    checks = [make_check(agent_id=agent.id) for _ in range(n)]
    db.add_all(checks)
    await db.flush()
    return [str(c.id) for c in checks]


@pytest_asyncio.fixture
async def client(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as c:
        yield c


async def _renders(client: AsyncClient, page: Any) -> None:
    """The page's public view renders: a page is public unless made private."""
    resp = await client.get(f"/status/{page.slug}")
    assert resp.status_code == 200, resp.text[:500]


@pytest.mark.parametrize(
    "filter",
    [
        {"agent_id": "not-a-uuid"},
        {"tags": "prod"},
        {"search": 5},
        {"check_type": "nope"},
        {"unknown": 1},
        {"tags": ["a\x00b"]},
    ],
)
async def test_a_group_filter_of_the_wrong_shape_is_refused(
    db: AsyncSession, filter: dict[str, Any]
) -> None:
    page = await _page(db, {})

    with pytest.raises(ValueError):
        await StatusPageCoreService.update_group_filters(db, page.id, 0, filter)
    with pytest.raises(ValueError):
        await StatusPageCoreService.add_group_to_page(db, page.id, "other", filter)


async def test_the_editors_group_filter_is_stored_as_the_admin_api_stores_it(
    client: AsyncClient, db: AsyncSession
) -> None:
    agent_id = str(uuid4())
    page = await _page(db, {})

    await StatusPageCoreService.update_group_filters(
        db,
        page.id,
        0,
        {"agent_id": agent_id, "check_type": "http", "tags": ["prod"], "search": "web"},
    )

    stored = await StatusPageCoreService.get_status_page(db, page.id)
    assert stored is not None
    assert stored.items[0]["filter"] == {
        "agent_id": agent_id,
        "check_type": "http",
        "tags": ["prod"],
        "search": "web",
    }
    await _renders(client, page)


@pytest.mark.parametrize(
    "item",
    [
        {"type": "group", "oldIndex": 0, "checks": 5},
        {"type": "group", "oldIndex": 0, "checks": [[1]]},
        {"type": "group", "oldIndex": 0, "checks": ["not-a-uuid"]},
        {"type": "group", "oldIndex": "a", "checks": []},
        {"type": "nope", "oldIndex": 0},
        {"type": "group", "oldIndex": 0, "checks": [], "extra": 1},
    ],
)
async def test_a_reordered_item_of_the_wrong_shape_is_refused(
    db: AsyncSession, item: dict[str, Any]
) -> None:
    page = await _page(db, None)

    with pytest.raises(ValueError):
        await StatusPageCoreService.reorder_all_items(db, page.id, [item])


async def test_the_editors_reorder_is_stored_and_renders(
    client: AsyncClient, db: AsyncSession
) -> None:
    page = await _page(db, None)
    await StatusPageCoreService.update_group_sort(db, page.id, 0, "name", "asc")
    [check_id] = await _check_ids(db)

    await StatusPageCoreService.reorder_all_items(
        db,
        page.id,
        [
            {
                "oldIndex": 0,
                "newIndex": 0,
                "type": "group",
                "checks": [check_id, check_id],
            }
        ],
    )

    stored = await StatusPageCoreService.get_status_page(db, page.id)
    assert stored is not None
    assert stored.items[0]["checks"] == [check_id]
    await _renders(client, page)


@pytest.mark.parametrize("name", ["", "x" * 256, "a\nb"])
async def test_a_group_name_the_admin_api_refuses_is_refused(
    db: AsyncSession, name: str
) -> None:
    page = await _page(db, None)

    with pytest.raises(ValueError):
        await StatusPageCoreService.rename_group(db, page.id, 0, name)
    with pytest.raises(ValueError):
        await StatusPageCoreService.add_group_to_page(db, page.id, name, None)


async def test_a_reorder_cannot_hold_more_items_than_a_page_takes(
    db: AsyncSession,
) -> None:
    page = await _page(db, None)

    with pytest.raises(ValueError, match="at most 500"):
        await StatusPageCoreService.reorder_all_items(
            db, page.id, [{"type": "group", "oldIndex": i} for i in range(501)]
        )


async def test_a_reorder_cannot_repeat_an_item(db: AsyncSession) -> None:
    page = await _page(db, None)

    with pytest.raises(ValueError):
        await StatusPageCoreService.reorder_all_items(
            db,
            page.id,
            [{"type": "group", "oldIndex": 0}, {"type": "group", "oldIndex": 0}],
        )


async def test_a_reorder_cannot_name_more_checks_than_a_page_takes(
    db: AsyncSession,
) -> None:
    page = await _page(db, None)
    for _ in range(10):
        await StatusPageCoreService.add_group_to_page(db, page.id, "group", None)

    # 11 groups of 1000 checks: within the item cap, past the checks cap.
    with pytest.raises(ValueError, match="at most 10000 checks"):
        await StatusPageCoreService.reorder_all_items(
            db,
            page.id,
            [
                {
                    "type": "group",
                    "oldIndex": i,
                    "checks": [str(uuid4()) for _ in range(1000)],
                }
                for i in range(11)
            ],
        )


async def test_a_check_dragged_out_of_a_group_stays_on_the_page(
    client: AsyncClient, db: AsyncSession
) -> None:
    page = await _page(db, None)
    [check_id] = await _check_ids(db)

    await StatusPageCoreService.reorder_all_items(
        db,
        page.id,
        [
            {"oldIndex": 0, "newIndex": 0, "type": "group", "checks": []},
            {"oldIndex": None, "newIndex": 1, "type": "check", "checkId": check_id},
        ],
    )

    stored = await StatusPageCoreService.get_status_page(db, page.id)
    assert stored is not None
    assert [i["type"] for i in stored.items] == ["group", "check"]
    assert stored.items[1]["check_id"] == check_id
    await _renders(client, page)


async def test_a_reorder_cannot_leave_out_a_group(db: AsyncSession) -> None:
    page = await _page(db, None)
    await StatusPageCoreService.add_group_to_page(db, page.id, "dropped", {})

    with pytest.raises(ValueError, match="leaves out"):
        await StatusPageCoreService.reorder_all_items(
            db, page.id, [{"type": "group", "oldIndex": 0, "checks": []}]
        )


async def test_a_check_dragged_into_a_group_leaves_its_own_place(
    db: AsyncSession,
) -> None:
    page = await _page(db, None)
    [check_id] = await _check_ids(db)
    await StatusPageCoreService.add_check_to_page(db, page.id, check_id)

    await StatusPageCoreService.reorder_all_items(
        db, page.id, [{"type": "group", "oldIndex": 0, "checks": [check_id]}]
    )

    stored = await StatusPageCoreService.get_status_page(db, page.id)
    assert stored is not None
    assert stored.items == [stored.items[0]]
    assert stored.items[0]["checks"] == [check_id]


@pytest.mark.parametrize("where", ["group", "own place"])
async def test_a_reorder_cannot_name_a_check_that_does_not_exist(
    db: AsyncSession, where: str
) -> None:
    page = await _page(db, None)
    unknown = str(uuid4())
    items: list[dict[str, Any]] = [{"type": "group", "oldIndex": 0, "checks": []}]
    if where == "group":
        items[0]["checks"] = [unknown]
    else:
        items.append({"type": "check", "oldIndex": None, "checkId": unknown})

    with pytest.raises(ValueError, match="No check has the id"):
        await StatusPageCoreService.reorder_all_items(db, page.id, items)


async def test_adding_a_check_that_does_not_exist_is_refused(
    db: AsyncSession,
) -> None:
    page = await _page(db, None)

    with pytest.raises(ValueError, match="No check has the id"):
        await StatusPageCoreService.add_check_to_page(db, page.id, uuid4())

    stored = await StatusPageCoreService.get_status_page(db, page.id)
    assert stored is not None
    assert [i["type"] for i in stored.items] == ["group"]


async def test_adding_a_check_puts_it_on_the_page(db: AsyncSession) -> None:
    page = await _page(db, None)
    [check_id] = await _check_ids(db)

    await StatusPageCoreService.add_check_to_page(db, page.id, UUID(check_id))

    stored = await StatusPageCoreService.get_status_page(db, page.id)
    assert stored is not None
    assert stored.items[-1]["check_id"] == check_id


async def test_a_page_keeps_reordering_after_a_check_it_names_is_gone(
    db: AsyncSession,
) -> None:
    page = await _page(db, None)
    [gone] = await _check_ids(db)
    await StatusPageCoreService.add_check_to_page(db, page.id, UUID(gone))
    check = await db.get(Check, UUID(gone))
    assert check is not None
    await db.delete(check)
    await db.flush()

    await StatusPageCoreService.reorder_all_items(
        db,
        page.id,
        [
            {"type": "check", "oldIndex": 1, "checkId": gone},
            {"type": "group", "oldIndex": 0, "checks": []},
        ],
    )


async def test_a_page_cannot_be_given_more_items_than_a_reorder_takes(
    db: AsyncSession,
) -> None:
    page = await StatusPageCoreService.create_status_page(
        db,
        StatusPageCreate(
            name="full",
            slug=f"full-{uuid4().hex[:8]}",
            items=[
                {"type": "check", "check_id": str(uuid4()), "order": i}
                for i in range(500)
            ],
        ),
    )

    with pytest.raises(ValueError, match="at most 500"):
        await StatusPageCoreService.add_check_to_page(db, page.id, uuid4())
    with pytest.raises(ValueError, match="at most 500"):
        await StatusPageCoreService.add_group_to_page(db, page.id, "one more", None)


@pytest.mark.parametrize("check_id", [None, "not-a-uuid"])
async def test_a_new_check_item_needs_a_check_uuid(
    db: AsyncSession, check_id: str | None
) -> None:
    page = await _page(db, None)

    with pytest.raises(ValueError):
        await StatusPageCoreService.reorder_all_items(
            db, page.id, [{"type": "check", "oldIndex": None, "checkId": check_id}]
        )


async def test_a_stored_check_item_moves_whatever_id_the_page_echoes(
    db: AsyncSession,
) -> None:
    page = await _page(db, None)

    await StatusPageCoreService.reorder_all_items(
        db,
        page.id,
        [{"type": "group", "oldIndex": 0, "checks": [], "checkId": "legacy"}],
    )


@pytest.mark.parametrize(
    ("sort_by", "sort_direction"),
    [("bogus", "asc"), ("name", "sideways"), ("a\x00b", "asc")],
)
async def test_a_group_sort_the_admin_api_refuses_is_refused(
    db: AsyncSession, sort_by: str, sort_direction: str
) -> None:
    page = await _page(db, None)

    with pytest.raises(ValueError):
        await StatusPageCoreService.update_group_sort(
            db, page.id, 0, sort_by, sort_direction
        )
