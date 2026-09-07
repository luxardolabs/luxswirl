"""The status page's whole layout lives in two JSONB columns.

`items` and `config` are free-form JSON, so nothing in the type system notices
if the DTO that now crosses the seam drops a key, flattens a nested group, or
coerces a value. The dashboard renderer builds every group, filter and sort
from exactly those structures, so this pins that the schema hands them back
byte-for-byte.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.schemas.status_page_schema import StatusPageCreate, StatusPageInDB
from app.services.core.status_page_core_service import StatusPageCoreService
from sqlalchemy.ext.asyncio import AsyncSession

ITEMS = [
    {"type": "check", "check_id": "9f1c0e2a-0000-4000-8000-000000000001"},
    {
        "type": "group",
        "name": "Edge",
        "sort_by": "latency",
        "sort_direction": "desc",
        "filter": {"check_type": "http", "tags": ["prod", "eu"]},
    },
    {
        "type": "group",
        "name": "Core",
        "sort_by": "manual",
        "checks": [
            "9f1c0e2a-0000-4000-8000-000000000002",
            "9f1c0e2a-0000-4000-8000-000000000003",
        ],
    },
]

CONFIG = {
    "theme": "dark",
    "show_uptime": True,
    "refresh_interval": 30,
    "nested": {"banner": {"text": "", "enabled": False}},
}


async def _create(db: AsyncSession, slug: str) -> StatusPageInDB:
    return await StatusPageCoreService.create_status_page(
        db,
        StatusPageCreate(
            name="Round trip",
            slug=slug,
            description=None,
            is_public=True,
            items=ITEMS,
            config=CONFIG,
        ),
    )


class TestJsonbSurvivesTheSeam:
    async def test_create_returns_the_layout_unchanged(self, db: AsyncSession):
        page = await _create(db, "round-trip-create")

        assert page.items == ITEMS
        assert page.config == CONFIG

    async def test_read_back_returns_the_layout_unchanged(self, db: AsyncSession):
        created = await _create(db, "round-trip-read")

        page = await StatusPageCoreService.get_status_page(db, created.id)

        assert page.items == ITEMS
        assert page.config == CONFIG

    async def test_slug_lookup_returns_the_layout_unchanged(self, db: AsyncSession):
        await _create(db, "round-trip-slug")

        page = await StatusPageCoreService.get_page_by_slug(db, "round-trip-slug")

        assert page is not None
        assert page.items == ITEMS
        assert page.config == CONFIG

    async def test_listing_returns_the_layout_unchanged(self, db: AsyncSession):
        created = await _create(db, "round-trip-list")

        pages, _ = await StatusPageCoreService.list_status_pages(db, limit=100)

        mine = next(p for p in pages if p.id == created.id)
        assert mine.items == ITEMS
        assert mine.config == CONFIG

    async def test_unknown_slug_is_none_not_a_raise(self, db: AsyncSession):
        assert await StatusPageCoreService.get_page_by_slug(db, "no-such-page") is None

    async def test_a_page_with_no_config_reads_back_as_none(self, db: AsyncSession):
        """config is nullable; an empty dict and NULL are different states and
        the renderer branches on it."""
        created = await StatusPageCoreService.create_status_page(
            db,
            StatusPageCreate(
                name="Bare",
                slug="round-trip-bare",
                description=None,
                is_public=True,
                items=[],
                config=None,
            ),
        )

        page = await StatusPageCoreService.get_status_page(db, created.id)

        assert page.config is None
        assert page.items == []
