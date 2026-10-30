"""The status-page item migration brings stored items into the admin API's shapes.

The web editor once stored a group's filter and checks as the browser sent
them, so a page could hold a group that breaks its render (manage and public)
and that the editor cannot open to fix. Revision e1b41fd38554 normalizes every
page's items, and no group comes out showing a check it did not show before.
The test suite's schema is already at head, so this runs the revision's
`migrate` directly against rows shaped like an older version's.
"""

from __future__ import annotations

import importlib.util
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.main import app
from app.models.status_page_model import StatusPage
from app.services.core.status_page_core_service import StatusPageCoreService
from fixtures.factories import make_agent, make_check
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

_REVISION = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "015_status_page_item_shapes.py"
)


def _revision() -> ModuleType:
    spec = importlib.util.spec_from_file_location("status_page_item_shapes", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _migrate(db: AsyncSession) -> None:
    revision = _revision()
    await db.run_sync(lambda session: revision.migrate(session.connection()))
    db.expire_all()


@pytest_asyncio.fixture
async def client(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://test",
    ) as c:
        yield c


async def _stored(db: AsyncSession, items: list[Any]) -> StatusPage:
    page = StatusPage(name="legacy", slug=f"legacy-{uuid4().hex[:8]}", items=items)
    db.add(page)
    await db.flush()
    return page


async def _items(db: AsyncSession, page_id: UUID) -> Any:
    stored = await StatusPageCoreService.get_status_page(db, page_id)
    assert stored is not None
    return stored.items


def _group(**fields: Any) -> dict[str, Any]:
    return {"type": "group", "name": "g", "order": 0, **fields}


def _shaped(order: int, **fields: Any) -> dict[str, Any]:
    return {
        "type": "group",
        "name": "g",
        "order": order,
        "collapsed": False,
        "sort_by": "manual",
        "sort_direction": "asc",
        **fields,
    }


@pytest.mark.parametrize(
    "legacy",
    [
        _group(filter=None),
        _group(checks=None),
        _group(filter={"agent_id": "nope"}),
        _group(filter="prod"),
        _group(checks=5),
        _group(checks=[[1]]),
        _group(sort_by=["name"], sort_direction={"a": 1}, checks=[]),
        _group(filter={"check_type": ["http"]}),
    ],
)
async def test_a_group_that_broke_the_render_renders_after_the_migration(
    client: AsyncClient, db: AsyncSession, legacy: dict[str, Any]
) -> None:
    slug = (await _stored(db, [legacy])).slug

    await _migrate(db)

    assert (await client.get(f"/status/{slug}")).status_code == 200


async def test_each_item_is_brought_to_the_admin_apis_shape(
    db: AsyncSession,
) -> None:
    upper = str(uuid4()).upper()
    agent_id = str(uuid4())
    kept = str(uuid4())
    page_id = (
        await _stored(
            db,
            [
                {"type": "check", "check_id": upper, "order": 5},
                {"type": "check", "order": 6},
                {"type": "nope", "order": 7},
                "not an item",
                _group(
                    name="",
                    collapsed="yes",
                    sort_by="bogus",
                    sort_direction="sideways",
                    filter={"agent_id": agent_id, "check_type": "nope"},
                ),
                _group(checks=[kept, kept.upper(), "not-a-uuid", 5]),
                _group(filter=None, checks=[kept]),
                _group(
                    filter={
                        "agent_id": agent_id.upper(),
                        "tags": ["prod", ""],
                        "search": "",
                        "unknown": 1,
                    }
                ),
            ],
        )
    ).id

    await _migrate(db)

    assert await _items(db, page_id) == [
        {"type": "check", "check_id": upper.lower(), "order": 0},
        {**_shaped(1, checks=[]), "name": "Group"},
        _shaped(2, checks=[kept]),
        _shaped(3, checks=[kept]),
        _shaped(4, filter={"agent_id": agent_id, "tags": ["prod", ""], "search": ""}),
    ]


@pytest.mark.parametrize(
    "legacy",
    [
        _group(filter={"tags": ["prod"], "search": "x" * 300}),
        _group(filter={"tags": ["prod\n"]}),
        _group(filter={"tags": ["prod"], "agent_id": "web01"}),
        _group(filter={"tags": ["prod"], "check_type": "nope"}),
        _group(filter={"tags": "prod"}),
        _group(filter={"tags": [""]}),
        _group(filter=None, checks=["MINE"]),
        _group(filter={"tags": ["prod"], "agent_id": "{OTHER}"}),
        _group(filter={"tags": ["prod"], "agent_id": "urn:uuid:OTHER"}),
    ],
)
async def test_no_group_shows_a_check_it_did_not_show_before(
    client: AsyncClient, db: AsyncSession, legacy: dict[str, Any]
) -> None:
    mine, other = make_agent(), make_agent()
    db.add_all([mine, other])
    await db.flush()
    own = make_check(agent_id=mine.id, display_name="minecheckkk", tags=["prod"])
    secret = make_check(agent_id=other.id, display_name="secretzzcheck", tags=["prod"])
    db.add_all([own, secret])
    await db.flush()
    if legacy.get("checks") == ["MINE"]:
        legacy = {**legacy, "checks": [str(own.id)]}
    agent_id = (legacy.get("filter") or {}).get("agent_id", "")
    if "OTHER" in agent_id:
        # A form the database refused: the group never showed the other agent.
        legacy = {
            **legacy,
            "filter": {
                **legacy["filter"],
                "agent_id": agent_id.replace("OTHER", str(other.id)),
            },
        }
    slug = (await _stored(db, [legacy])).slug

    await _migrate(db)

    resp = await client.get(f"/status/{slug}")
    assert resp.status_code == 200
    assert "secretzzcheck" not in resp.text


@pytest.mark.parametrize(
    "legacy",
    [
        _group(filter={"agent_id": "", "tags": ["prod"]}),
        _group(filter={"check_type": "", "tags": ["prod"]}),
        _group(filter={"tags": ""}),
        _group(filter={"tags": {}}),
        _group(filter={"search": 0, "tags": ["prod"]}),
        _group(checks={"MINE": True}),
    ],
)
async def test_a_group_keeps_showing_the_checks_it_showed(
    client: AsyncClient, db: AsyncSession, legacy: dict[str, Any]
) -> None:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    own = make_check(agent_id=agent.id, display_name="minecheckkk", tags=["prod"])
    db.add(own)
    await db.flush()
    if legacy.get("checks") == {"MINE": True}:
        legacy = {**legacy, "checks": {str(own.id): True}}
    slug = (await _stored(db, [legacy])).slug

    await _migrate(db)

    resp = await client.get(f"/status/{slug}")
    assert resp.status_code == 200
    assert "minecheckkk" in resp.text


async def test_a_page_whose_items_are_not_a_list_is_emptied(db: AsyncSession) -> None:
    page_id = (await _stored(db, [])).id
    await db.execute(
        text('UPDATE status_pages SET items = \'{"not": "a list"}\' WHERE id = :id'),
        {"id": page_id},
    )

    await _migrate(db)

    assert await _items(db, page_id) == []


async def test_a_page_already_in_shape_is_left_as_it_is(db: AsyncSession) -> None:
    check_id = str(uuid4())
    items = [
        {"type": "check", "check_id": check_id, "order": 0},
        {
            "type": "group",
            "name": "Core",
            "order": 1,
            "collapsed": True,
            "sort_by": "name",
            "sort_direction": "desc",
            "checks": [check_id],
        },
        {
            "type": "group",
            "name": "Prod",
            "order": 2,
            "collapsed": False,
            "sort_by": "manual",
            "sort_direction": "asc",
            "filter": {"check_type": "http", "tags": ["prod"], "search": ""},
        },
    ]
    page = await _stored(db, items)
    page_id, before = page.id, page.updated_at

    await _migrate(db)

    stored = await StatusPageCoreService.get_status_page(db, page_id)
    assert stored is not None
    assert stored.items == items
    assert stored.updated_at == before


async def test_the_migration_runs_twice_to_the_same_result(db: AsyncSession) -> None:
    page_id = (
        await _stored(
            db,
            [_group(filter=None), _group(checks=None), _group(filter={"tags": "x"})],
        )
    ).id

    await _migrate(db)
    once = await _items(db, page_id)
    await _migrate(db)

    assert await _items(db, page_id) == once
