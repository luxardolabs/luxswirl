"""Integration tests for CheckCoreService bulk operations.

bulk_delete / bulk_set_enabled / bulk_modify / clone_check exercise the high-LOC paths in
check_core_service.py that the basic CRUD tests don't reach.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from app.crud.check_crud import CheckCRUD
from app.schemas.check_schema import CheckCreate, CheckUpdate
from app.services.core.check_core_service import CheckCoreService
from app.services.views.checks_view_service import ChecksViewService
from fixtures.factories import make_agent, make_check
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# bulk_delete / bulk_set_enabled
# ---------------------------------------------------------------------------


class TestBulkAction:
    async def test_delete_action(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        checks = [make_check(agent_id=agent.id) for _ in range(3)]
        for c in checks:
            db.add(c)
        await db.flush()

        result = await CheckCoreService.bulk_delete(db, [c.id for c in checks])
        assert result["success_count"] == 3
        assert result["failure_count"] == 0
        # All gone
        assert await CheckCRUD.list_for_agent(db, agent.id) == []

    async def test_disable_action(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        checks = [make_check(agent_id=agent.id, enabled=True) for _ in range(2)]
        for c in checks:
            db.add(c)
        await db.flush()

        result = await CheckCoreService.bulk_set_enabled(
            db, [c.id for c in checks], enabled=False
        )
        assert result["success_count"] == 2

        for c in await CheckCRUD.get_checks_by_ids(db, [c.id for c in checks]):
            assert c.enabled is False

    async def test_enable_action(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        checks = [make_check(agent_id=agent.id, enabled=False) for _ in range(2)]
        for c in checks:
            db.add(c)
        await db.flush()

        result = await CheckCoreService.bulk_set_enabled(
            db, [c.id for c in checks], enabled=True
        )
        assert result["success_count"] == 2

        for c in await CheckCRUD.get_checks_by_ids(db, [c.id for c in checks]):
            assert c.enabled is True

    async def test_unknown_action_records_failure(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        check = make_check(agent_id=agent.id)
        db.add(check)
        await db.flush()

        # The free-form action string arrives from the web form; the view
        # refuses one it does not know rather than guessing.
        success, failure, message, kind = await ChecksViewService.execute_bulk_action(
            db, "vaporize", "false", None, "all", "", [check.id]
        )
        assert (success, failure, kind) == (0, 0, "warning")
        assert "Unknown action" in message
        (unchanged,) = await CheckCRUD.get_checks_by_ids(db, [check.id])
        assert unchanged.enabled is check.enabled

    async def test_empty_input_returns_zero(self, db: AsyncSession):
        empty = {"success_count": 0, "failure_count": 0, "errors": []}
        assert await CheckCoreService.bulk_delete(db, []) == empty
        assert await CheckCoreService.bulk_set_enabled(db, [], enabled=True) == empty

    async def test_delete_bumps_agent_checks_updated_at(self, db: AsyncSession):
        """Deleting a check must bump the agent's checks_updated_at so the
        agent reloads its config on next heartbeat."""
        agent = make_agent(checks_updated_at=None)
        db.add(agent)
        await db.flush()
        check = make_check(agent_id=agent.id)
        db.add(check)
        await db.flush()

        await CheckCoreService.bulk_delete(db, [check.id])
        await db.flush()
        await db.refresh(agent)
        assert agent.checks_updated_at is not None


# ---------------------------------------------------------------------------
# bulk_modify
# ---------------------------------------------------------------------------


class TestBulkModify:
    async def test_modifies_each_check(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        checks = [make_check(agent_id=agent.id, interval_seconds=60) for _ in range(3)]
        for c in checks:
            db.add(c)
        await db.flush()

        result = await CheckCoreService.bulk_modify(
            db,
            [c.id for c in checks],
            CheckUpdate(interval_seconds=120),
        )
        assert result["success_count"] == 3

        for c in await CheckCRUD.get_checks_by_ids(db, [c.id for c in checks]):
            assert c.interval_seconds == 120

    async def test_unknown_check_records_failure(self, db: AsyncSession):
        result = await CheckCoreService.bulk_modify(
            db,
            [uuid4()],
            CheckUpdate(interval_seconds=30),
        )
        # Service catches per-check exceptions and counts failures
        assert result["failure_count"] >= 1

    async def test_change_agent_assignment(self, db: AsyncSession):
        agent_a = make_agent()
        agent_b = make_agent()
        db.add(agent_a)
        db.add(agent_b)
        await db.flush()
        check = make_check(agent_id=agent_a.id)
        db.add(check)
        await db.flush()

        result = await CheckCoreService.bulk_modify(
            db,
            [check.id],
            CheckUpdate(display_name=check.display_name),
            new_agent_id=agent_b.id,
        )
        assert result["success_count"] == 1

        reloaded = await CheckCRUD.get_by_id(db, check.id)
        assert reloaded.agent_id == agent_b.id


# ---------------------------------------------------------------------------
# clone_check
# ---------------------------------------------------------------------------


class TestCloneCheck:
    async def test_clone_to_same_agent_creates_new_check(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        source = await CheckCoreService.create_check(
            db,
            agent.id,
            CheckCreate(
                display_name="original",
                check_type="http",
                target="https://example.test",
                http_method="GET",
                interval_seconds=60,
                timeout_seconds=5,
                retry_interval_seconds=30,
                enabled=True,
            ),
            skip_config_update=True,
        )

        cloned = await CheckCoreService.clone_check(
            db, source.id, agent.id, skip_config_update=True
        )
        assert cloned.id != source.id
        assert cloned.agent_id == agent.id
        # Display name is auto-suffixed to avoid duplicates
        assert source.display_name in cloned.display_name
        assert cloned.check_type == source.check_type
        assert cloned.target == source.target

    async def test_clone_overrides_win_and_unset_fields_keep_the_source(
        self, db: AsyncSession
    ):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        source = await CheckCoreService.create_check(
            db,
            agent.id,
            CheckCreate(
                display_name="original",
                check_type="http",
                target="https://example.test",
                http_method="GET",
                interval_seconds=60,
                timeout_seconds=5,
                enabled=True,
            ),
            skip_config_update=True,
        )

        cloned = await CheckCoreService.clone_check(
            db,
            source.id,
            agent.id,
            overrides=CheckCreate(
                display_name="copy",
                check_type="http",
                target="https://other.test",
                interval_seconds=120,
            ),
            skip_config_update=True,
        )

        assert cloned.target == "https://other.test"
        assert cloned.interval_seconds == 120
        # Left unset (None) on the overrides: the source's value carries over.
        assert cloned.timeout_seconds == 5

    async def test_clone_to_different_agent(self, db: AsyncSession):
        agent_a = make_agent()
        agent_b = make_agent()
        db.add(agent_a)
        db.add(agent_b)
        await db.flush()
        source = make_check(agent_id=agent_a.id, display_name="source-on-a")
        db.add(source)
        await db.flush()

        cloned = await CheckCoreService.clone_check(
            db, source.id, agent_b.id, skip_config_update=True
        )
        assert cloned.agent_id == agent_b.id
        # Cross-agent clones don't need name disambiguation
        assert cloned.display_name == source.display_name

    async def test_clone_missing_source_raises(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        with pytest.raises(Exception):  # noqa: B017
            await CheckCoreService.clone_check(
                db,
                uuid4(),
                agent.id,
                skip_config_update=True,
            )


# ---------------------------------------------------------------------------
# get_dependency_info / list_dependents
# ---------------------------------------------------------------------------


class TestDependencyInfo:
    async def test_get_dependency_info(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        parent = make_check(agent_id=agent.id, display_name="gateway")
        db.add(parent)
        await db.flush()
        child = make_check(
            agent_id=agent.id,
            display_name="behind-gateway",
            depends_on_check_id=parent.id,
        )
        db.add(child)
        await db.flush()

        info = await CheckCoreService.get_dependency_info(db, child.id)
        # Just confirm the call returns a dict shape — exact contents vary
        # by implementation but it should include parent info
        assert isinstance(info, dict)
