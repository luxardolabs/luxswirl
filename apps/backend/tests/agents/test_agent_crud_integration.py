"""Integration tests for AgentCRUD against a real TimescaleDB."""

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
from app.crud.agent_crud import AgentCRUD  # noqa: E402

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# list_all / get_by_id_with_checks / get_by_name_with_checks
# ---------------------------------------------------------------------------


class TestListAll:
    async def test_returns_all_agents(self, db: AsyncSession):
        for _ in range(3):
            db.add(make_agent())
        await db.flush()
        rows = await AgentCRUD.list_all(db)
        assert len(rows) == 3

    async def test_empty_returns_empty_list(self, db: AsyncSession):
        assert await AgentCRUD.list_all(db) == []


class TestGetByIdWithChecks:
    async def test_loads_agent_with_checks_eager(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        db.add(make_check(agent_id=agent.id))
        db.add(make_check(agent_id=agent.id))
        await db.flush()

        loaded = await AgentCRUD.get_by_id_with_checks(db, agent.id)
        assert loaded is not None
        assert loaded.id == agent.id
        # Eager-loaded — accessing checks must not trigger lazy load
        assert len(loaded.checks) == 2

    async def test_missing_returns_none(self, db: AsyncSession):
        assert await AgentCRUD.get_by_id_with_checks(db, uuid4()) is None


class TestGetByNameWithChecks:
    async def test_returns_by_name(self, db: AsyncSession):
        agent = make_agent(agent_name="my-named-agent")
        db.add(agent)
        await db.flush()

        loaded = await AgentCRUD.get_by_name_with_checks(db, "my-named-agent")
        assert loaded is not None
        assert loaded.id == agent.id

    async def test_missing_name_returns_none(self, db: AsyncSession):
        assert await AgentCRUD.get_by_name_with_checks(db, "nonexistent") is None


# ---------------------------------------------------------------------------
# Pending / approved
# ---------------------------------------------------------------------------


class TestPendingFilters:
    async def test_count_pending(self, db: AsyncSession):
        db.add(make_agent(approval_status="pending"))
        db.add(make_agent(approval_status="pending"))
        db.add(make_agent(approval_status="active"))
        db.add(make_agent(approval_status="rejected"))
        await db.flush()

        assert await AgentCRUD.count_pending(db) == 2

    async def test_list_pending(self, db: AsyncSession):
        active = make_agent(approval_status="active")
        pending1 = make_agent(approval_status="pending")
        pending2 = make_agent(approval_status="pending")
        for a in (active, pending1, pending2):
            db.add(a)
        await db.flush()

        rows = await AgentCRUD.list_pending(db)
        ids = {r.id for r in rows}
        assert ids == {pending1.id, pending2.id}


class TestGetAdmittedAgents:
    async def test_excludes_only_pending_and_rejected(self, db: AsyncSession):
        """`get_admitted_agents` returns everything that's been triaged —
        active, paused, disabled — and excludes only pending and rejected.
        ("Admitted" means "passed admin review," not "currently running")."""
        db.add(make_agent(approval_status="active"))
        db.add(make_agent(approval_status="paused"))
        db.add(make_agent(approval_status="disabled"))
        db.add(make_agent(approval_status="pending"))
        db.add(make_agent(approval_status="rejected"))
        await db.flush()

        rows = await AgentCRUD.get_admitted_agents(db)
        statuses = {a.approval_status for a in rows}
        assert statuses == {"active", "paused", "disabled"}
        assert "pending" not in statuses
        assert "rejected" not in statuses


# ---------------------------------------------------------------------------
# last_seen / stale queries
# ---------------------------------------------------------------------------


class TestSeenSince:
    async def test_list_seen_since(self, db: AsyncSession):
        now = utc_now()
        recent = make_agent(last_seen=now)
        stale = make_agent(last_seen=now - timedelta(hours=2))
        db.add(recent)
        db.add(stale)
        await db.flush()

        cutoff = now - timedelta(minutes=30)
        rows = await AgentCRUD.list_seen_since(db, cutoff)
        ids = {r.id for r in rows}
        assert recent.id in ids
        assert stale.id not in ids

    async def test_count_seen_since(self, db: AsyncSession):
        now = utc_now()
        db.add(make_agent(last_seen=now))
        db.add(make_agent(last_seen=now))
        db.add(make_agent(last_seen=now - timedelta(hours=5)))
        await db.flush()

        count = await AgentCRUD.count_seen_since(db, now - timedelta(hours=1))
        assert count == 2


class TestStaleAgents:
    async def test_get_stale_agent_names(self, db: AsyncSession):
        now = utc_now()
        fresh = make_agent(agent_name="fresh-one", last_seen=now)
        stale1 = make_agent(agent_name="stale-one", last_seen=now - timedelta(days=2))
        stale2 = make_agent(agent_name="stale-two", last_seen=now - timedelta(days=3))
        for a in (fresh, stale1, stale2):
            db.add(a)
        await db.flush()

        cutoff = now - timedelta(days=1)
        names = await AgentCRUD.get_stale_agent_names(db, cutoff)
        assert set(names) == {"stale-one", "stale-two"}

    async def test_delete_stale_agents(self, db: AsyncSession):
        now = utc_now()
        keep = make_agent(last_seen=now)
        drop1 = make_agent(last_seen=now - timedelta(days=5))
        drop2 = make_agent(last_seen=now - timedelta(days=10))
        for a in (keep, drop1, drop2):
            db.add(a)
        await db.flush()

        count = await AgentCRUD.delete_stale_agents(db, now - timedelta(days=1))
        assert count == 2

        remaining = await AgentCRUD.list_all(db)
        assert len(remaining) == 1
        assert remaining[0].id == keep.id


# ---------------------------------------------------------------------------
# Pagination + filtering
# ---------------------------------------------------------------------------


class TestListFilteredPaginated:
    async def test_pagination_and_total(self, db: AsyncSession):
        for i in range(7):
            db.add(make_agent(agent_name=f"agent-{i:02d}"))
        await db.flush()

        rows, total = await AgentCRUD.list_filtered_paginated(db, offset=2, limit=3)
        assert total == 7
        assert len(rows) == 3

    async def test_exclude_pending_filter(self, db: AsyncSession):
        db.add(make_agent(approval_status="active"))
        db.add(make_agent(approval_status="pending"))
        db.add(make_agent(approval_status="rejected"))
        db.add(make_agent(approval_status="paused"))
        await db.flush()

        rows, total = await AgentCRUD.list_filtered_paginated(db, exclude_pending=True)
        # exclude_pending drops pending + rejected (per CRUD impl)
        statuses = {a.approval_status for a in rows}
        assert "pending" not in statuses
        assert "rejected" not in statuses
        assert "active" in statuses
        assert "paused" in statuses
        assert total == 2

    async def test_search_matches_agent_name(self, db: AsyncSession):
        db.add(make_agent(agent_name="prod-server-1"))
        db.add(make_agent(agent_name="prod-server-2"))
        db.add(make_agent(agent_name="staging-1"))
        await db.flush()

        rows, total = await AgentCRUD.list_filtered_paginated(db, search="prod")
        assert total == 2
        assert all("prod" in a.agent_name for a in rows)


# ---------------------------------------------------------------------------
# get_name_lastseen_pairs
# ---------------------------------------------------------------------------


class TestGetNameLastseenPairs:
    async def test_returns_name_and_lastseen(self, db: AsyncSession):
        now = utc_now()
        db.add(make_agent(agent_name="a-one", last_seen=now))
        db.add(make_agent(agent_name="a-two", last_seen=now - timedelta(hours=1)))
        await db.flush()

        pairs = await AgentCRUD.get_name_lastseen_pairs(db)
        d = {name: ts for name, ts in pairs if name}
        assert "a-one" in d
        assert "a-two" in d
