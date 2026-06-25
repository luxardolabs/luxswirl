"""Integration tests for StatusPageCoreService.

CRUD lifecycle + check/group membership + reordering.
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

_tests_root = str(Path(__file__).resolve().parent.parent)
if _tests_root not in sys.path:
    sys.path.insert(0, _tests_root)

from fixtures.factories import make_agent, make_check  # noqa: E402

from app.core.exceptions import NotFoundException  # noqa: E402
from app.schemas.status_page_schema import StatusPageCreate, StatusPageUpdate  # noqa: E402
from app.services.core.status_page_core_service import StatusPageCoreService  # noqa: E402

pytestmark = pytest.mark.integration


def _create_data(**overrides) -> StatusPageCreate:
    defaults = {
        "name": f"Status Page {uuid4().hex[:6]}",
        "slug": f"sp-{uuid4().hex[:8]}",
        "description": "Test page",
        "is_public": True,
    }
    defaults.update(overrides)
    return StatusPageCreate(**defaults)


async def _agent_with_check(db):
    a = make_agent()
    db.add(a)
    await db.flush()
    c = make_check(agent_id=a.id)
    db.add(c)
    await db.flush()
    return a, c


# ---------------------------------------------------------------------------
# create / get / update / delete
# ---------------------------------------------------------------------------


class TestCRUD:
    async def test_create_status_page(self, db: AsyncSession):
        page = await StatusPageCoreService.create_status_page(db, _create_data())
        assert page.id is not None
        assert page.is_public is True
        # New pages start empty
        assert page.items == [] or page.items is None

    async def test_get_by_id(self, db: AsyncSession):
        page = await StatusPageCoreService.create_status_page(db, _create_data())
        loaded = await StatusPageCoreService.get_status_page_by_id(db, page.id)
        assert loaded.id == page.id

    async def test_get_by_id_missing_raises(self, db: AsyncSession):
        with pytest.raises(NotFoundException):
            await StatusPageCoreService.get_status_page_by_id(db, uuid4())

    async def test_get_by_slug(self, db: AsyncSession):
        page = await StatusPageCoreService.create_status_page(
            db,
            _create_data(slug="findable-slug"),
        )
        loaded = await StatusPageCoreService.get_status_page_by_slug(
            db,
            "findable-slug",
        )
        assert loaded.id == page.id

    async def test_get_by_slug_missing_returns_none(self, db: AsyncSession):
        # By slug returns None (vs by ID which raises)
        assert (await StatusPageCoreService.get_status_page_by_slug(db, "absent-slug")) is None

    async def test_update_name_and_description(self, db: AsyncSession):
        page = await StatusPageCoreService.create_status_page(db, _create_data())
        updated = await StatusPageCoreService.update_status_page(
            db,
            page.id,
            StatusPageUpdate(name="New Name", description="New desc"),
        )
        assert updated.name == "New Name"
        assert updated.description == "New desc"

    async def test_delete_status_page(self, db: AsyncSession):
        page = await StatusPageCoreService.create_status_page(db, _create_data())
        page_id = page.id
        await StatusPageCoreService.delete_status_page(db, page_id)
        with pytest.raises(NotFoundException):
            await StatusPageCoreService.get_status_page_by_id(db, page_id)

    async def test_delete_missing_raises(self, db: AsyncSession):
        with pytest.raises(NotFoundException):
            await StatusPageCoreService.delete_status_page(db, uuid4())


# ---------------------------------------------------------------------------
# list + counts
# ---------------------------------------------------------------------------


class TestListing:
    async def test_list_paginated(self, db: AsyncSession):
        for i in range(5):
            await StatusPageCoreService.create_status_page(
                db,
                _create_data(slug=f"page-{i:02d}"),
            )
        rows, total = await StatusPageCoreService.list_status_pages(
            db,
            offset=1,
            limit=2,
        )
        assert total == 5
        assert len(rows) == 2

    async def test_get_public_count(self, db: AsyncSession):
        await StatusPageCoreService.create_status_page(
            db,
            _create_data(is_public=True),
        )
        await StatusPageCoreService.create_status_page(
            db,
            _create_data(is_public=True),
        )
        await StatusPageCoreService.create_status_page(
            db,
            _create_data(is_public=False),
        )
        assert await StatusPageCoreService.get_public_count(db) == 2

    async def test_get_private_count(self, db: AsyncSession):
        await StatusPageCoreService.create_status_page(
            db,
            _create_data(is_public=True),
        )
        await StatusPageCoreService.create_status_page(
            db,
            _create_data(is_public=False),
        )
        await StatusPageCoreService.create_status_page(
            db,
            _create_data(is_public=False),
        )
        assert await StatusPageCoreService.get_private_count(db) == 2


# ---------------------------------------------------------------------------
# Items: add check / add group / remove / reorder
# ---------------------------------------------------------------------------


class TestItemMembership:
    async def test_add_check_to_page(self, db: AsyncSession):
        _, check = await _agent_with_check(db)
        page = await StatusPageCoreService.create_status_page(db, _create_data())

        updated = await StatusPageCoreService.add_check_to_page(
            db,
            page.id,
            str(check.id),
        )
        assert len(updated.items) == 1
        assert updated.items[0]["type"] == "check"
        assert updated.items[0]["check_id"] == str(check.id)

    async def test_add_group_to_page(self, db: AsyncSession):
        page = await StatusPageCoreService.create_status_page(db, _create_data())
        updated = await StatusPageCoreService.add_group_to_page(
            db,
            page.id,
            name="Production",
            filter={"tags": ["prod"]},
        )
        assert len(updated.items) == 1
        assert updated.items[0]["type"] == "group"
        assert updated.items[0]["name"] == "Production"

    async def test_remove_item_from_page_by_index(self, db: AsyncSession):
        _, check = await _agent_with_check(db)
        page = await StatusPageCoreService.create_status_page(db, _create_data())
        await StatusPageCoreService.add_check_to_page(db, page.id, str(check.id))

        result = await StatusPageCoreService.remove_item_from_page(
            db,
            page.id,
            index=0,
        )
        # Item removed — page is empty
        assert len(result.items) == 0
