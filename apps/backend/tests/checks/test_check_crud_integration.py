"""Integration tests for CheckCRUD against a real TimescaleDB.

Exercises every public method on `CheckCRUD`. Pattern mirrors
`tests/alerts/test_alert_crud_integration.py`.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

_tests_root = str(Path(__file__).resolve().parent.parent)
if _tests_root not in sys.path:
    sys.path.insert(0, _tests_root)

from fixtures.factories import make_agent, make_check  # noqa: E402

from app.core.datetime_utils import utc_now  # noqa: E402
from app.crud.check_crud import CheckCRUD  # noqa: E402

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# get_by_id
# ---------------------------------------------------------------------------


class TestGetById:
    async def test_returns_check_with_agent_loaded(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        check = make_check(agent_id=agent.id)
        db.add(check)
        await db.flush()

        loaded = await CheckCRUD.get_by_id(db, check.id)
        assert loaded is not None
        assert loaded.id == check.id
        # Agent was eagerly loaded — accessing it must not trigger a lazy load
        assert loaded.agent is not None
        assert loaded.agent.id == agent.id

    async def test_missing_returns_none(self, db: AsyncSession):
        assert await CheckCRUD.get_by_id(db, uuid4()) is None

    async def test_include_script_code_round_trips_value(self, db: AsyncSession):
        """script_code is a deferred column; the include_script_code flag
        undefers it. We verify the value round-trips correctly when requested.
        (We don't test the "deferred by default" half because SQLAlchemy's
        identity map keeps the freshly-inserted instance cached, making it
        impossible to demonstrate without a separate session — that's testing
        SQLAlchemy machinery, not our wrapper.)
        """
        agent = make_agent()
        db.add(agent)
        await db.flush()
        check = make_check(
            agent_id=agent.id,
            check_type="synthetic",
            script_code="print('hello')",
        )
        db.add(check)
        await db.flush()

        loaded = await CheckCRUD.get_by_id(db, check.id, include_script_code=True)
        assert loaded is not None
        assert loaded.script_code == "print('hello')"


# ---------------------------------------------------------------------------
# list_paginated — filters
# ---------------------------------------------------------------------------


class TestListPaginated:
    async def test_no_filters_returns_all(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        for _ in range(3):
            db.add(make_check(agent_id=agent.id))
        await db.flush()

        rows, total = await CheckCRUD.list_paginated(db)
        assert total == 3
        assert len(rows) == 3

    async def test_agent_id_filter(self, db: AsyncSession):
        a1 = make_agent()
        a2 = make_agent()
        db.add(a1)
        db.add(a2)
        await db.flush()
        for _ in range(2):
            db.add(make_check(agent_id=a1.id))
        db.add(make_check(agent_id=a2.id))
        await db.flush()

        rows, total = await CheckCRUD.list_paginated(db, agent_id=a1.id)
        assert total == 2
        assert all(c.agent_id == a1.id for c in rows)

    async def test_check_type_filter(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        db.add(make_check(agent_id=agent.id, check_type="ping"))
        db.add(make_check(agent_id=agent.id, check_type="http"))
        db.add(make_check(agent_id=agent.id, check_type="http"))
        await db.flush()

        rows, total = await CheckCRUD.list_paginated(db, check_type="http")
        assert total == 2
        assert all(c.check_type == "http" for c in rows)

    async def test_enabled_only_filter(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        db.add(make_check(agent_id=agent.id, enabled=True))
        db.add(make_check(agent_id=agent.id, enabled=False))
        await db.flush()

        rows, total = await CheckCRUD.list_paginated(db, enabled_only=True)
        assert total == 1
        assert rows[0].enabled is True

    async def test_tag_filter_uses_array_containment(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        db.add(make_check(agent_id=agent.id, tags=["prod", "critical"]))
        db.add(make_check(agent_id=agent.id, tags=["staging"]))
        db.add(make_check(agent_id=agent.id, tags=None))
        await db.flush()

        rows, total = await CheckCRUD.list_paginated(db, tag="prod")
        assert total == 1
        assert "prod" in rows[0].tags

    async def test_search_matches_display_name_or_target(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        db.add(make_check(agent_id=agent.id, display_name="db-primary"))
        db.add(make_check(agent_id=agent.id, display_name="cache", target="redis-host"))
        db.add(make_check(agent_id=agent.id, display_name="other", target="other.example"))
        await db.flush()

        # Match on display_name
        rows, _ = await CheckCRUD.list_paginated(db, search="primary")
        assert len(rows) == 1
        assert rows[0].display_name == "db-primary"

        # Match on target
        rows, _ = await CheckCRUD.list_paginated(db, search="redis")
        assert len(rows) == 1
        assert rows[0].target == "redis-host"

    async def test_exclude_internal_filter(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        db.add(make_check(agent_id=agent.id, check_type="internal"))
        db.add(make_check(agent_id=agent.id, check_type="ping"))
        await db.flush()

        rows, total = await CheckCRUD.list_paginated(db, exclude_internal=True)
        assert total == 1
        assert rows[0].check_type == "ping"

    async def test_pagination_applies_offset_and_limit(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        for i in range(5):
            db.add(make_check(agent_id=agent.id, display_name=f"check-{i:02d}"))
        await db.flush()

        rows, total = await CheckCRUD.list_paginated(db, offset=2, limit=2)
        assert total == 5  # total ignores pagination
        assert len(rows) == 2

    async def test_ordering_is_stable(self, db: AsyncSession):
        """Order: agent_id, display_name (covered by LUXSWIRL status-view fix
        — same dedup principle: stable ordering across refreshes)."""
        agent = make_agent()
        db.add(agent)
        await db.flush()
        db.add(make_check(agent_id=agent.id, display_name="zebra"))
        db.add(make_check(agent_id=agent.id, display_name="alpha"))
        db.add(make_check(agent_id=agent.id, display_name="middle"))
        await db.flush()

        rows, _ = await CheckCRUD.list_paginated(db)
        names = [r.display_name for r in rows]
        assert names == sorted(names)


# ---------------------------------------------------------------------------
# list_for_agent / get_with_agent_by_ids / get_checks_by_ids
# ---------------------------------------------------------------------------


class TestListForAgent:
    async def test_returns_only_agent_checks_sorted(self, db: AsyncSession):
        a1 = make_agent()
        a2 = make_agent()
        db.add(a1)
        db.add(a2)
        await db.flush()
        db.add(make_check(agent_id=a1.id, display_name="b"))
        db.add(make_check(agent_id=a1.id, display_name="a"))
        db.add(make_check(agent_id=a2.id, display_name="z"))
        await db.flush()

        rows = await CheckCRUD.list_for_agent(db, a1.id)
        assert len(rows) == 2
        assert [r.display_name for r in rows] == ["a", "b"]
        assert all(r.agent_id == a1.id for r in rows)

    async def test_empty_agent_returns_empty_list(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        rows = await CheckCRUD.list_for_agent(db, agent.id)
        assert rows == []


class TestGetWithAgentByIds:
    async def test_empty_input_returns_empty(self, db: AsyncSession):
        rows = await CheckCRUD.get_with_agent_by_ids(db, [])
        assert rows == []

    async def test_returns_only_requested_ids(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        kept = make_check(agent_id=agent.id)
        skipped = make_check(agent_id=agent.id)
        db.add(kept)
        db.add(skipped)
        await db.flush()

        rows = await CheckCRUD.get_with_agent_by_ids(db, [kept.id])
        ids = [r.id for r in rows]
        assert kept.id in ids
        assert skipped.id not in ids


class TestGetChecksByIds:
    async def test_returns_with_agent_loaded(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        check = make_check(agent_id=agent.id)
        db.add(check)
        await db.flush()

        rows = await CheckCRUD.get_checks_by_ids(db, [check.id])
        assert len(rows) == 1
        assert rows[0].agent.id == agent.id

    async def test_empty_input_returns_empty(self, db: AsyncSession):
        assert await CheckCRUD.get_checks_by_ids(db, []) == []


# ---------------------------------------------------------------------------
# bulk_delete_by_ids
# ---------------------------------------------------------------------------


class TestBulkDelete:
    async def test_empty_input_returns_zero(self, db: AsyncSession):
        assert await CheckCRUD.bulk_delete_by_ids(db, []) == 0

    async def test_deletes_and_returns_rowcount(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        checks = [make_check(agent_id=agent.id) for _ in range(3)]
        for c in checks:
            db.add(c)
        await db.flush()

        count = await CheckCRUD.bulk_delete_by_ids(db, [c.id for c in checks[:2]])
        assert count == 2
        # Verify only one remains
        remaining = await CheckCRUD.list_for_agent(db, agent.id)
        assert len(remaining) == 1
        assert remaining[0].id == checks[2].id

    async def test_nonexistent_ids_dont_error(self, db: AsyncSession):
        # Passing ids that don't exist returns zero, doesn't raise
        count = await CheckCRUD.bulk_delete_by_ids(db, [uuid4(), uuid4()])
        assert count == 0


# ---------------------------------------------------------------------------
# bulk_set_enabled
# ---------------------------------------------------------------------------


class TestBulkSetEnabled:
    async def test_empty_input_returns_zero(self, db: AsyncSession):
        assert await CheckCRUD.bulk_set_enabled(db, [], True) == 0

    async def test_disables_then_reenables(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        checks = [make_check(agent_id=agent.id, enabled=True) for _ in range(3)]
        for c in checks:
            db.add(c)
        await db.flush()
        ids = [c.id for c in checks]

        # Disable all 3
        assert await CheckCRUD.bulk_set_enabled(db, ids, False) == 3
        db.expire_all()
        for c in await CheckCRUD.get_checks_by_ids(db, ids):
            assert c.enabled is False

        # Re-enable all 3
        assert await CheckCRUD.bulk_set_enabled(db, ids, True) == 3
        db.expire_all()
        for c in await CheckCRUD.get_checks_by_ids(db, ids):
            assert c.enabled is True


# ---------------------------------------------------------------------------
# Dependency counts
# ---------------------------------------------------------------------------


class TestCountDependents:
    async def test_no_dependents(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        parent = make_check(agent_id=agent.id)
        db.add(parent)
        await db.flush()
        assert await CheckCRUD.count_dependents(db, parent.id) == 0

    async def test_counts_children(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        parent = make_check(agent_id=agent.id)
        db.add(parent)
        await db.flush()
        for _ in range(4):
            db.add(make_check(agent_id=agent.id, depends_on_check_id=parent.id))
        await db.flush()

        assert await CheckCRUD.count_dependents(db, parent.id) == 4

    async def test_bulk_groups_by_parent(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        p1 = make_check(agent_id=agent.id)
        p2 = make_check(agent_id=agent.id)
        p3 = make_check(agent_id=agent.id)  # no children
        for p in (p1, p2, p3):
            db.add(p)
        await db.flush()
        for _ in range(2):
            db.add(make_check(agent_id=agent.id, depends_on_check_id=p1.id))
        db.add(make_check(agent_id=agent.id, depends_on_check_id=p2.id))
        await db.flush()

        counts = await CheckCRUD.count_dependents_bulk(db, [p1.id, p2.id, p3.id])
        assert counts.get(p1.id) == 2
        assert counts.get(p2.id) == 1
        # p3 had no children — query returns no row → not in dict
        assert p3.id not in counts

    async def test_bulk_empty_input(self, db: AsyncSession):
        assert await CheckCRUD.count_dependents_bulk(db, []) == {}


# ---------------------------------------------------------------------------
# count_for_agents_seen_since
# ---------------------------------------------------------------------------


class TestCountForAgentsSeenSince:
    async def test_only_recent_agents_counted(self, db: AsyncSession):
        now = utc_now()
        recent_agent = make_agent(last_seen=now)
        stale_agent = make_agent(last_seen=now - timedelta(days=10))
        db.add(recent_agent)
        db.add(stale_agent)
        await db.flush()
        db.add(make_check(agent_id=recent_agent.id))
        db.add(make_check(agent_id=recent_agent.id))
        db.add(make_check(agent_id=stale_agent.id))
        await db.flush()

        cutoff = now - timedelta(hours=1)
        count = await CheckCRUD.count_for_agents_seen_since(db, cutoff)
        assert count == 2  # only recent_agent's checks


# ---------------------------------------------------------------------------
# Distinct types and tags
# ---------------------------------------------------------------------------


class TestDistinctCheckTypes:
    async def test_returns_sorted_unique(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        for t in ("http", "ping", "http", "tcp", "ping"):
            db.add(make_check(agent_id=agent.id, check_type=t))
        await db.flush()

        types = await CheckCRUD.get_distinct_check_types(db)
        assert types == ["http", "ping", "tcp"]

    async def test_empty_when_no_checks(self, db: AsyncSession):
        assert await CheckCRUD.get_distinct_check_types(db) == []


class TestGetAllCheckTags:
    async def test_collects_and_sorts(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        db.add(make_check(agent_id=agent.id, tags=["prod", "critical"]))
        db.add(make_check(agent_id=agent.id, tags=["staging", "prod"]))
        db.add(make_check(agent_id=agent.id, tags=None))
        await db.flush()

        tags = await CheckCRUD.get_all_check_tags(db)
        assert tags == ["critical", "prod", "staging"]


class TestGetAllTagsCombined:
    async def test_merges_check_and_agent_array_tags(self, db: AsyncSession):
        # Both agents and checks use PostgreSQL arrays now (LUXSWIRL-176).
        # The combined helper must merge, dedupe, and sort across both.
        agent_with_tags = make_agent(tags=["ops", "alpha"])
        agent_without = make_agent(tags=None)
        db.add(agent_with_tags)
        db.add(agent_without)
        await db.flush()
        db.add(make_check(agent_id=agent_with_tags.id, tags=["prod", "alpha"]))
        await db.flush()

        tags = await CheckCRUD.get_all_tags_combined(db)
        assert tags == ["alpha", "ops", "prod"]


# ---------------------------------------------------------------------------
# list_all — sanity check
# ---------------------------------------------------------------------------


class TestListAll:
    async def test_returns_every_check_unfiltered(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        for enabled in (True, False, True):
            db.add(make_check(agent_id=agent.id, enabled=enabled))
        await db.flush()

        rows = await CheckCRUD.list_all(db)
        assert len(rows) == 3
