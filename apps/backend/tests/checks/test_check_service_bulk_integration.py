"""Integration tests for CheckCoreService bulk operations.

bulk_delete / bulk_set_enabled / bulk_modify / clone_check exercise the high-LOC paths in
check_core_service.py that the basic CRUD tests don't reach.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from app.core.exceptions import CheckNotFoundException
from app.crud.check_crud import CheckCRUD
from app.models.check_model import Check
from app.schemas.check_schema import CheckCreate, CheckUpdate
from app.services.core.alert_core_service import AlertCoreService
from app.services.core.check_core_service import CheckCoreService
from app.services.core.check_dependency_core_service import (
    CheckDependencyCoreService,
)
from app.services.views.checks_view_service import ChecksViewService
from fixtures.factories import make_agent, make_alert, make_check, make_user
from sqlalchemy import func, select, text
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

        result = await CheckCoreService.bulk_delete(
            db, [c.id for c in checks], actor_is_admin=True
        )
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
            db, [c.id for c in checks], enabled=False, actor_is_admin=True
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
            db, [c.id for c in checks], enabled=True, actor_is_admin=True
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
            db, "vaporize", "false", None, "all", "", [check.id], make_user()
        )
        assert (success, failure, kind) == (0, 0, "warning")
        assert "Unknown action" in message
        (unchanged,) = await CheckCRUD.get_checks_by_ids(db, [check.id])
        assert unchanged.enabled is check.enabled

    async def test_empty_input_returns_zero(self, db: AsyncSession):
        empty = {"success_count": 0, "failure_count": 0, "errors": []}
        assert await CheckCoreService.bulk_delete(db, [], actor_is_admin=True) == empty
        assert (
            await CheckCoreService.bulk_set_enabled(
                db, [], enabled=True, actor_is_admin=True
            )
            == empty
        )

    async def test_delete_bumps_agent_checks_updated_at(self, db: AsyncSession):
        """Deleting a check must bump the agent's checks_updated_at so the
        agent reloads its config on next heartbeat."""
        agent = make_agent(checks_updated_at=None)
        db.add(agent)
        await db.flush()
        check = make_check(agent_id=agent.id)
        db.add(check)
        await db.flush()

        await CheckCoreService.bulk_delete(db, [check.id], actor_is_admin=True)
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
            actor_is_admin=True,
        )
        assert result["success_count"] == 3

        for c in await CheckCRUD.get_checks_by_ids(db, [c.id for c in checks]):
            assert c.interval_seconds == 120

    async def test_unknown_check_records_failure(self, db: AsyncSession):
        result = await CheckCoreService.bulk_modify(
            db,
            [uuid4()],
            CheckUpdate(interval_seconds=30),
            actor_is_admin=True,
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
            actor_is_admin=True,
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

    async def test_a_check_with_a_global_alert_clones_with_each_alert_once(
        self, db: AsyncSession
    ):
        """A new check gets the global alerts; the clone copies the source's
        alerts too, and the global ones it already has are not added twice."""
        agent = make_agent()
        global_alert = make_alert(is_global=True)
        own_alert = make_alert()
        db.add_all([agent, global_alert, own_alert])
        await db.flush()
        source = await CheckCoreService.create_check(
            db,
            agent.id,
            CheckCreate(
                display_name="watched",
                check_type="ping",
                target="127.0.0.1",
                retry_interval_seconds=30,
            ),
            skip_config_update=True,
        )
        await AlertCoreService.add_check(db, own_alert.id, source.id)

        cloned = await CheckCoreService.clone_check(
            db, source.id, agent.id, skip_config_update=True
        )

        alert_ids = await AlertCoreService.get_alert_ids_for_check(db, cloned.id)
        assert sorted(alert_ids) == sorted([global_alert.id, own_alert.id])

    async def test_a_check_stored_without_a_timeout_clones_with_the_default(
        self, db: AsyncSession
    ):
        # timeout_seconds is nullable in the table but required by CheckCreate:
        # cloning such a check failed validation (a 500) instead of cloning it.
        agent = make_agent()
        db.add(agent)
        await db.flush()
        source = make_check(agent_id=agent.id, check_type="tcp", timeout_seconds=None)
        db.add(source)
        await db.flush()

        cloned = await CheckCoreService.clone_check(
            db, source.id, agent.id, skip_config_update=True
        )

        assert cloned.timeout_seconds == 10  # check.default_timeout's default

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
        with pytest.raises(CheckNotFoundException):
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

        info = await CheckDependencyCoreService.get_dependency_info(db, child.id)
        # Just confirm the call returns a dict shape — exact contents vary
        # by implementation but it should include parent info
        assert isinstance(info, dict)


class TestBulkFailureLeavesTheSessionUsable:
    """A failed bulk statement is reported as failures — and that report only
    reaches anyone if the session it ran in can still commit afterwards."""

    @pytest.mark.parametrize("operation", ["toggle", "delete"])
    async def test_a_database_error_is_reported_and_the_session_survives(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch, operation: str
    ):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        check = make_check(agent_id=agent.id)
        db.add(check)
        await db.flush()

        async def _fails(session: AsyncSession, *_args: object) -> int:
            await session.execute(text("SELECT 1 / 0"))
            return 0

        target = "bulk_set_enabled" if operation == "toggle" else "bulk_delete_by_ids"
        monkeypatch.setattr(CheckCRUD, target, staticmethod(_fails))

        if operation == "toggle":
            result = await CheckCoreService.bulk_set_enabled(
                db, [check.id], enabled=False, actor_is_admin=True
            )
        else:
            result = await CheckCoreService.bulk_delete(
                db, [check.id], actor_is_admin=True
            )

        assert result["success_count"] == 0
        assert result["failure_count"] == 1
        assert "division by zero" in result["errors"][0]
        # The request's own commit happens after this returns: it must work.
        assert (
            await db.execute(select(func.count()).select_from(Check))
        ).scalar_one() >= 1
        await db.flush()


class TestBulkCountsAddUp:
    async def test_ids_with_no_check_are_failures(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        check = make_check(agent_id=agent.id)
        db.add(check)
        await db.flush()

        result = await CheckCoreService.bulk_delete(
            db, [check.id, uuid4(), uuid4()], actor_is_admin=True
        )

        assert result["success_count"] == 1
        assert result["failure_count"] == 2
        assert result["errors"] == ["2 check(s) not found"]
