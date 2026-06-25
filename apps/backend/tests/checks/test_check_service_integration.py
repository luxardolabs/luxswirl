"""Integration tests for CheckCoreService.

Covers the business logic on top of CheckCRUD: create/update/delete
lifecycle, dependency validation, bulk operations, type-specific config
packing. Pure DB integration tests against the real TimescaleDB; no
service-layer mocks.
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

from app.core.exceptions import (  # noqa: E402
    AgentNotFoundException,
    AuthorizationException,
    NotFoundException,
    ValidationException,
)
from app.schemas.check_schema import CheckCreate, CheckUpdate  # noqa: E402
from app.services.core.check_core_service import CheckCoreService  # noqa: E402

pytestmark = pytest.mark.integration


def _create_data(**overrides) -> CheckCreate:
    """CheckCreate payload with safe defaults. Use 127.0.0.1 to bypass SSRF
    checks against unresolvable hosts."""
    defaults = {
        "display_name": "test-ping",
        "check_type": "ping",
        "target": "127.0.0.1",
        "interval_seconds": 60,
        "timeout_seconds": 5,
        "retry_attempts": 2,
        "retry_interval_seconds": 30,
        "enabled": True,
    }
    defaults.update(overrides)
    return CheckCreate(**defaults)


# ---------------------------------------------------------------------------
# create_check
# ---------------------------------------------------------------------------


class TestCreateCheck:
    async def test_creates_check_for_agent(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()

        check = await CheckCoreService.create_check(
            db,
            agent.id,
            _create_data(skip_config_update=False),
            skip_config_update=True,
        )
        assert check.id is not None
        assert check.agent_id == agent.id
        assert check.display_name == "test-ping"
        assert check.check_type == "ping"
        assert check.target == "127.0.0.1"
        assert check.enabled is True

    async def test_missing_agent_raises(self, db: AsyncSession):
        with pytest.raises(AgentNotFoundException):
            await CheckCoreService.create_check(
                db, uuid4(), _create_data(), skip_config_update=True
            )

    async def test_http_check_packs_check_config(self, db: AsyncSession):
        """HTTP check fields (http_method, expected_status, verify_ssl) must
        land in check_config JSONB, not on the row directly."""
        agent = make_agent()
        db.add(agent)
        await db.flush()

        check = await CheckCoreService.create_check(
            db,
            agent.id,
            _create_data(
                display_name="api-check",
                check_type="http",
                target="https://api.example.test/health",
                http_method="POST",
                expected_status=204,
                verify_ssl=False,
            ),
            skip_config_update=True,
        )
        assert check.check_config is not None
        assert check.check_config.get("http_method") == "POST"
        assert check.check_config.get("expected_status") == 204
        assert check.check_config.get("verify_ssl") is False

    async def test_irrelevant_fields_not_packed(self, db: AsyncSession):
        """A ping check shouldn't get HTTP-specific fields in its config even
        if the user supplies them — the type_fields map gates by check_type."""
        agent = make_agent()
        db.add(agent)
        await db.flush()

        check = await CheckCoreService.create_check(
            db,
            agent.id,
            _create_data(
                check_type="ping",
                target="127.0.0.1",
                http_method="GET",  # irrelevant for ping
                expected_status=200,  # irrelevant for ping
            ),
            skip_config_update=True,
        )
        # ping has no relevant type_fields → check_config stays None
        assert check.check_config is None

    async def test_dns_check_packs_dns_fields(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()

        check = await CheckCoreService.create_check(
            db,
            agent.id,
            _create_data(
                check_type="dns",
                target="example.test",
                record_type="A",
                nameserver="1.1.1.1",
            ),
            skip_config_update=True,
        )
        assert check.check_config is not None
        assert check.check_config.get("record_type") == "A"
        assert check.check_config.get("nameserver") == "1.1.1.1"

    async def test_synthetic_check_blocks_malicious_script(self, db: AsyncSession):
        """AST validation must reject scripts that import dangerous modules."""
        from app.core.synthetic_security import SyntheticSecurityError

        agent = make_agent()
        db.add(agent)
        await db.flush()

        with pytest.raises(SyntheticSecurityError):
            await CheckCoreService.create_check(
                db,
                agent.id,
                _create_data(
                    check_type="synthetic",
                    target="https://example.test",
                    script_code="import os; os.system('rm -rf /')",
                ),
                skip_config_update=True,
                actor_is_admin=True,
            )

    async def test_synthetic_check_allows_safe_script(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()

        safe_script = (
            "async def run_check(page):\n"
            "    await page.goto('https://example.test')\n"
            "    return {'status': 'success', 'steps': ['Loaded page']}\n"
        )
        check = await CheckCoreService.create_check(
            db,
            agent.id,
            _create_data(
                check_type="synthetic",
                target="https://example.test",
                script_code=safe_script,
            ),
            skip_config_update=True,
            actor_is_admin=True,
        )
        assert check.id is not None

    async def test_synthetic_create_denied_without_admin(self, db: AsyncSession):
        """H-1 regression (LUXSWIRL-190): the core service is the authoritative gate.
        A non-admin actor must be rejected BEFORE AST validation, regardless of the
        entry layer — this is what stops the JSON-API path from bypassing the gate."""
        agent = make_agent()
        db.add(agent)
        await db.flush()

        with pytest.raises(AuthorizationException):
            await CheckCoreService.create_check(
                db,
                agent.id,
                _create_data(
                    check_type="synthetic",
                    target="https://example.test",
                    script_code="async def run_check(page):\n    return {}\n",
                ),
                skip_config_update=True,
                actor_is_admin=False,
            )

    async def test_non_synthetic_create_allowed_without_admin(self, db: AsyncSession):
        """The gate is scoped to synthetic only — a non-admin can create a ping check."""
        agent = make_agent()
        db.add(agent)
        await db.flush()

        check = await CheckCoreService.create_check(
            db,
            agent.id,
            _create_data(check_type="ping", target="example.test"),
            skip_config_update=True,
            actor_is_admin=False,
        )
        assert check.id is not None

    async def test_update_to_synthetic_denied_without_admin(self, db: AsyncSession):
        """A non-admin must not be able to convert an existing check to synthetic."""
        agent = make_agent()
        db.add(agent)
        await db.flush()
        check = await CheckCoreService.create_check(
            db,
            agent.id,
            _create_data(check_type="ping", target="example.test"),
            skip_config_update=True,
            actor_is_admin=False,
        )

        with pytest.raises(AuthorizationException):
            await CheckCoreService.update_check(
                db,
                check.id,
                CheckUpdate(
                    check_type="synthetic",
                    script_code="async def run_check(page):\n    return {}\n",
                ),
                actor_is_admin=False,
            )

    async def test_setting_parent_dependency(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()

        # Create parent
        parent = await CheckCoreService.create_check(
            db, agent.id, _create_data(display_name="parent"), skip_config_update=True
        )

        # Create child with depends_on_check_id
        child = await CheckCoreService.create_check(
            db,
            agent.id,
            _create_data(display_name="child", depends_on_check_id=parent.id),
            skip_config_update=True,
        )
        assert child.depends_on_check_id == parent.id

    async def test_rejects_multi_level_dependency(self, db: AsyncSession):
        """Single-level dependencies only — parent cannot itself have a parent."""
        agent = make_agent()
        db.add(agent)
        await db.flush()

        grandparent = await CheckCoreService.create_check(
            db,
            agent.id,
            _create_data(display_name="grandparent"),
            skip_config_update=True,
        )
        parent = await CheckCoreService.create_check(
            db,
            agent.id,
            _create_data(display_name="parent", depends_on_check_id=grandparent.id),
            skip_config_update=True,
        )

        with pytest.raises(ValidationException, match="already has a parent"):
            await CheckCoreService.create_check(
                db,
                agent.id,
                _create_data(display_name="child", depends_on_check_id=parent.id),
                skip_config_update=True,
            )

    async def test_rejects_nonexistent_parent(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()

        with pytest.raises(ValidationException, match="not found"):
            await CheckCoreService.create_check(
                db,
                agent.id,
                _create_data(depends_on_check_id=uuid4()),
                skip_config_update=True,
            )


# ---------------------------------------------------------------------------
# update_check
# ---------------------------------------------------------------------------


class TestUpdateCheck:
    async def test_updates_display_name_and_enabled(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        original = await CheckCoreService.create_check(
            db, agent.id, _create_data(), skip_config_update=True
        )

        updated = await CheckCoreService.update_check(
            db,
            original.id,
            CheckUpdate(display_name="renamed", enabled=False),
        )
        assert updated.display_name == "renamed"
        assert updated.enabled is False

    async def test_partial_update_preserves_other_fields(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        original = await CheckCoreService.create_check(
            db,
            agent.id,
            _create_data(target="127.0.0.1", interval_seconds=60),
            skip_config_update=True,
        )

        updated = await CheckCoreService.update_check(
            db,
            original.id,
            CheckUpdate(interval_seconds=120),
        )
        assert updated.interval_seconds == 120
        assert updated.target == "127.0.0.1"  # untouched

    async def test_missing_check_raises(self, db: AsyncSession):
        with pytest.raises(NotFoundException):
            await CheckCoreService.update_check(db, uuid4(), CheckUpdate(display_name="x"))


# ---------------------------------------------------------------------------
# delete_check
# ---------------------------------------------------------------------------


class TestDeleteCheck:
    async def test_deletes_existing_check(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        check = make_check(agent_id=agent.id)
        db.add(check)
        await db.flush()
        check_id = check.id

        await CheckCoreService.delete_check(db, check_id)
        from app.crud.check_crud import CheckCRUD

        assert await CheckCRUD.get_by_id(db, check_id) is None

    async def test_missing_check_raises(self, db: AsyncSession):
        with pytest.raises(NotFoundException):
            await CheckCoreService.delete_check(db, uuid4())


# ---------------------------------------------------------------------------
# _validate_dependency (pure logic, but called via the public path)
# ---------------------------------------------------------------------------


class TestValidateDependency:
    async def test_none_is_ok(self, db: AsyncSession):
        # None means "no parent" — no validation needed
        await CheckCoreService._validate_dependency(db, None, own_check_id=uuid4())

    async def test_self_reference_rejected(self, db: AsyncSession):
        own = uuid4()
        with pytest.raises(ValidationException, match="cannot depend on itself"):
            await CheckCoreService._validate_dependency(db, own, own_check_id=own)

    async def test_nonexistent_parent_rejected(self, db: AsyncSession):
        with pytest.raises(ValidationException, match="not found"):
            await CheckCoreService._validate_dependency(db, uuid4(), own_check_id=uuid4())

    async def test_grandparent_chain_rejected(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        grandparent = make_check(agent_id=agent.id)
        db.add(grandparent)
        await db.flush()
        parent = make_check(agent_id=agent.id, depends_on_check_id=grandparent.id)
        db.add(parent)
        await db.flush()

        with pytest.raises(ValidationException, match="already has a parent"):
            await CheckCoreService._validate_dependency(db, parent.id, own_check_id=uuid4())


# ---------------------------------------------------------------------------
# list_dependents / list_eligible_parents / get_dependency_info
# ---------------------------------------------------------------------------


class TestDependencyQueries:
    async def test_list_dependents_returns_children(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        parent = make_check(agent_id=agent.id)
        db.add(parent)
        await db.flush()
        for _ in range(3):
            db.add(make_check(agent_id=agent.id, depends_on_check_id=parent.id))
        await db.flush()

        children = await CheckCoreService.list_dependents(db, parent.id)
        assert len(children) == 3

    async def test_list_eligible_parents_excludes_dependents(self, db: AsyncSession):
        """A check that already has a parent cannot itself be a parent
        (single-level rule). list_eligible_parents must filter those out."""
        agent = make_agent()
        db.add(agent)
        await db.flush()
        free = make_check(agent_id=agent.id, display_name="free")
        already_child = make_check(agent_id=agent.id, display_name="already-child")
        db.add(free)
        db.add(already_child)
        await db.flush()
        # Make already_child actually a child by giving it a parent
        already_child.depends_on_check_id = free.id
        await db.flush()

        # When picking a parent for a NEW check, both are technically eligible
        # — `free` because it has no parent; `already_child` would NOT be
        # eligible because it has one. Verify that's enforced.
        from app.crud.check_crud import CheckCRUD

        candidates = await CheckCoreService.list_eligible_parents(db, exclude_check_id=None)
        candidate_ids = [c.id for c in candidates]
        assert free.id in candidate_ids
        assert already_child.id not in candidate_ids

        # Sanity: CRUD agrees
        assert await CheckCRUD.count_dependents(db, free.id) == 1
