"""Integration tests for AgentCoreService.

Covers the registration → approval → state-change → key-management lifecycle.
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

import bcrypt
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

_tests_root = str(Path(__file__).resolve().parent.parent)
if _tests_root not in sys.path:
    sys.path.insert(0, _tests_root)

from fixtures.factories import make_agent  # noqa: E402

from app.core.exceptions import (  # noqa: E402
    AgentNotFoundException,
    DuplicateResourceException,
)
from app.schemas.agent_schema import AgentCreate, AgentUpdate  # noqa: E402
from app.services.core.agent_core_service import AgentCoreService  # noqa: E402

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# create / register
# ---------------------------------------------------------------------------


class TestCreateAgent:
    async def test_creates_with_active_defaults(self, db: AsyncSession):
        agent = await AgentCoreService.create_agent(
            db, AgentCreate(agent_name="alpha", hostname="alpha.example.test")
        )
        assert agent.agent_name == "alpha"
        assert agent.hostname == "alpha.example.test"
        assert agent.first_seen is not None
        assert agent.last_seen is not None

    async def test_duplicate_name_raises(self, db: AsyncSession):
        await AgentCoreService.create_agent(db, AgentCreate(agent_name="dup"))
        with pytest.raises(DuplicateResourceException, match="already exists"):
            await AgentCoreService.create_agent(db, AgentCreate(agent_name="dup"))


class TestRegisterAgent:
    async def test_creates_with_pending_status(self, db: AsyncSession):
        agent = await AgentCoreService.register_agent(
            db,
            hostname="newcomer.example.test",
            ip_address="10.0.0.1",
        )
        assert agent.approval_status == "pending"
        assert agent.agent_name is None  # assigned during approval
        assert agent.hostname == "newcomer.example.test"

    async def test_packs_tags_as_array(self, db: AsyncSession):
        agent = await AgentCoreService.register_agent(
            db,
            hostname="h",
            tags=["prod", "us-east"],
        )
        # Tags column is a real ARRAY(String) for agents (LUXSWIRL-176)
        assert agent.tags == ["prod", "us-east"]

    async def test_none_tags_stored_as_none(self, db: AsyncSession):
        agent = await AgentCoreService.register_agent(db, hostname="h", tags=None)
        assert agent.tags is None


# ---------------------------------------------------------------------------
# get_by_id / get_by_name
# ---------------------------------------------------------------------------


class TestGetters:
    async def test_get_by_id(self, db: AsyncSession):
        a = make_agent()
        db.add(a)
        await db.flush()
        loaded = await AgentCoreService.get_agent_by_id(db, a.id)
        assert loaded.id == a.id

    async def test_get_by_id_missing_raises(self, db: AsyncSession):
        with pytest.raises(AgentNotFoundException):
            await AgentCoreService.get_agent_by_id(db, uuid4())

    async def test_get_by_name(self, db: AsyncSession):
        a = make_agent(agent_name="findable")
        db.add(a)
        await db.flush()
        loaded = await AgentCoreService.get_agent_by_name(db, "findable")
        assert loaded is not None
        assert loaded.id == a.id

    async def test_get_by_name_missing_returns_none(self, db: AsyncSession):
        # get_agent_by_name returns None (doesn't raise) — see _or_raise variant
        assert await AgentCoreService.get_agent_by_name(db, "absent") is None

    async def test_get_by_name_or_raise(self, db: AsyncSession):
        with pytest.raises(AgentNotFoundException):
            await AgentCoreService.get_agent_by_name_or_raise(db, "absent")


# ---------------------------------------------------------------------------
# update_agent
# ---------------------------------------------------------------------------


class TestUpdateAgent:
    async def test_updates_fields(self, db: AsyncSession):
        a = make_agent(agent_name="before")
        db.add(a)
        await db.flush()
        updated = await AgentCoreService.update_agent(
            db,
            a.id,
            AgentUpdate(agent_name="after", hostname="new-host"),
        )
        assert updated.agent_name == "after"
        assert updated.hostname == "new-host"

    async def test_missing_agent_raises(self, db: AsyncSession):
        with pytest.raises(AgentNotFoundException):
            await AgentCoreService.update_agent(db, uuid4(), AgentUpdate(agent_name="x"))


# ---------------------------------------------------------------------------
# Approval workflow
# ---------------------------------------------------------------------------


class TestApprovalWorkflow:
    async def test_approve_pending_agent_assigns_api_key(self, db: AsyncSession):
        pending = await AgentCoreService.register_agent(db, hostname="h")
        assert pending.approval_status == "pending"

        agent, plaintext_key = await AgentCoreService.approve_agent(db, pending.id)
        assert agent.approval_status == "active"
        assert plaintext_key.startswith("luxswirl_ak_")
        assert agent.api_key_hash is not None
        # Verify the returned plaintext matches the stored hash
        assert bcrypt.checkpw(plaintext_key.encode(), agent.api_key_hash.encode())
        assert agent.api_key_created_at is not None
        assert agent.approved_at is not None

    async def test_approve_active_agent_raises(self, db: AsyncSession):
        a = make_agent(approval_status="active")
        db.add(a)
        await db.flush()
        with pytest.raises(ValueError, match="already approved"):
            await AgentCoreService.approve_agent(db, a.id)

    async def test_reject_pending_agent(self, db: AsyncSession):
        pending = await AgentCoreService.register_agent(db, hostname="h")
        rejected = await AgentCoreService.reject_agent(db, pending.id, reason="failed audit")
        assert rejected.approval_status == "rejected"
        assert rejected.status_reason == "failed audit"

    async def test_pause_active_agent(self, db: AsyncSession):
        a = make_agent(approval_status="active")
        db.add(a)
        await db.flush()
        paused = await AgentCoreService.pause_agent(db, a.id, reason="maintenance")
        assert paused.approval_status == "paused"
        assert paused.status_reason == "maintenance"

    async def test_resume_paused_agent(self, db: AsyncSession):
        a = make_agent(approval_status="paused", status_reason="was-paused")
        db.add(a)
        await db.flush()
        resumed = await AgentCoreService.resume_agent(db, a.id)
        assert resumed.approval_status == "active"

    async def test_disable_then_enable(self, db: AsyncSession):
        a = make_agent(approval_status="active")
        db.add(a)
        await db.flush()
        disabled = await AgentCoreService.disable_agent(db, a.id, reason="security")
        assert disabled.approval_status == "disabled"
        assert disabled.status_reason == "security"

        enabled = await AgentCoreService.enable_agent(db, a.id)
        assert enabled.approval_status == "active"


# ---------------------------------------------------------------------------
# API key management
# ---------------------------------------------------------------------------


class TestApiKeyManagement:
    async def test_regenerate_changes_hash_and_returns_plaintext(self, db: AsyncSession):
        pending = await AgentCoreService.register_agent(db, hostname="h")
        approved, original_key = await AgentCoreService.approve_agent(db, pending.id)
        original_hash = approved.api_key_hash

        regenerated, new_key = await AgentCoreService.regenerate_agent_key(db, approved.id)
        assert new_key != original_key
        assert regenerated.api_key_hash != original_hash
        assert bcrypt.checkpw(new_key.encode(), regenerated.api_key_hash.encode())
        # Original key must NOT verify against new hash
        assert not bcrypt.checkpw(original_key.encode(), regenerated.api_key_hash.encode())

    async def test_revoke_clears_hash(self, db: AsyncSession):
        pending = await AgentCoreService.register_agent(db, hostname="h")
        approved, _ = await AgentCoreService.approve_agent(db, pending.id)
        assert approved.api_key_hash is not None

        revoked = await AgentCoreService.revoke_agent_key(db, approved.id)
        assert revoked.api_key_hash is None


# ---------------------------------------------------------------------------
# Stats & counters
# ---------------------------------------------------------------------------


class TestStatsAndCounters:
    async def test_get_pending_count(self, db: AsyncSession):
        db.add(make_agent(approval_status="pending"))
        db.add(make_agent(approval_status="pending"))
        db.add(make_agent(approval_status="active"))
        await db.flush()
        assert await AgentCoreService.get_pending_count(db) == 2

    async def test_get_pending_agents(self, db: AsyncSession):
        db.add(make_agent(approval_status="pending"))
        db.add(make_agent(approval_status="active"))
        await db.flush()
        rows = await AgentCoreService.get_pending_agents(db)
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# delete_agent
# ---------------------------------------------------------------------------


class TestDeleteAgent:
    async def test_deletes_existing_agent(self, db: AsyncSession):
        a = make_agent()
        db.add(a)
        await db.flush()
        agent_id = a.id

        await AgentCoreService.delete_agent(db, agent_id)

        from app.crud.agent_crud import AgentCRUD

        assert await AgentCRUD.get_by_id_with_checks(db, agent_id) is None

    async def test_missing_agent_raises(self, db: AsyncSession):
        with pytest.raises(AgentNotFoundException):
            await AgentCoreService.delete_agent(db, uuid4())
