"""
Agent CRUD - database queries for agent operations.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.datetime_utils import utc_now
from app.models.agent_metric_model import AgentMetric
from app.models.agent_model import Agent


class AgentCRUD:
    """Database queries for agents."""

    @staticmethod
    async def get_stale_agent_names(db: AsyncSession, cutoff: datetime) -> list[str]:
        """Return names of agents whose last_seen is older than cutoff."""
        result = await db.execute(
            select(Agent.agent_name).where(Agent.last_seen < cutoff)
        )
        return [name for name in result.scalars().all() if name]

    @staticmethod
    async def delete_stale_agents(db: AsyncSession, cutoff: datetime) -> int:
        """Delete agents with last_seen older than cutoff. Returns rowcount."""
        result = await db.execute(delete(Agent).where(Agent.last_seen < cutoff))
        return result.rowcount or 0  # type: ignore[attr-defined]

    @staticmethod
    async def list_all(db: AsyncSession) -> list[Agent]:
        """Return all agents (unfiltered, unsorted)."""
        result = await db.execute(select(Agent))
        return list(result.scalars().all())

    @staticmethod
    async def get_by_id_with_checks(db: AsyncSession, agent_id: UUID) -> Agent | None:
        """Fetch agent by id with checks loaded."""
        result = await db.execute(
            select(Agent)
            .where(Agent.id == agent_id)
            .options(selectinload(Agent.checks))
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_name_with_checks(
        db: AsyncSession, agent_name: str
    ) -> Agent | None:
        """Fetch agent by agent_name with checks loaded."""
        result = await db.execute(
            select(Agent)
            .where(Agent.agent_name == agent_name)
            .options(selectinload(Agent.checks))
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def list_filtered_paginated(
        db: AsyncSession,
        *,
        active_only: bool = False,
        active_window_minutes: int = 10,
        search: str | None = None,
        exclude_pending: bool = False,
        skip: int = 0,
        limit: int = 100,
    ) -> tuple[list[Agent], int]:
        """Paginated list of agents with filters. Returns (agents, total)."""
        query = select(Agent).options(selectinload(Agent.checks))
        conditions = []
        if active_only:
            cutoff_time = utc_now() - timedelta(minutes=active_window_minutes)
            conditions.append(Agent.last_seen >= cutoff_time)
        if exclude_pending:
            conditions.append(Agent.approval_status.not_in(["pending", "rejected"]))
        if search:
            pattern = f"%{search}%"
            conditions.append(
                or_(
                    Agent.agent_name.ilike(pattern),
                    Agent.hostname.ilike(pattern),
                    Agent.ip_address.ilike(pattern),
                )
            )
        if conditions:
            query = query.where(*conditions)

        total_result = await db.execute(
            select(func.count()).select_from(query.subquery())
        )
        total = total_result.scalar_one()

        result = await db.execute(
            query.offset(skip)
            .limit(limit)
            .order_by(
                Agent.agent_name.asc().nullslast(), Agent.hostname.asc().nullslast()
            )
        )
        return list(result.scalars().all()), total

    @staticmethod
    async def count_pending(db: AsyncSession) -> int:
        """Count agents with approval_status == 'pending'."""
        result = await db.execute(
            select(func.count())
            .select_from(Agent)
            .where(Agent.approval_status == "pending")
        )
        return result.scalar_one()

    @staticmethod
    async def count_all(db: AsyncSession) -> int:
        """Total agent count (all approval states)."""
        result = await db.execute(select(func.count()).select_from(Agent))
        return result.scalar_one()

    @staticmethod
    async def list_pending(db: AsyncSession) -> list[Agent]:
        """All pending agents ordered by created_at desc."""
        result = await db.execute(
            select(Agent)
            .where(Agent.approval_status == "pending")
            .order_by(Agent.created_at.desc())
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_liveness_rows(
        db: AsyncSession,
    ) -> list[tuple[UUID, str | None, datetime]]:
        """(id, agent_name, last_seen) for every agent; the name may be unset."""
        result = await db.execute(select(Agent.id, Agent.agent_name, Agent.last_seen))
        return [(agent_id, name, last_seen) for agent_id, name, last_seen in result]

    @staticmethod
    async def list_seen_since(db: AsyncSession, cutoff: datetime) -> list[Agent]:
        """All agents whose last_seen is >= cutoff."""
        result = await db.execute(select(Agent).where(Agent.last_seen >= cutoff))
        return list(result.scalars().all())

    @staticmethod
    async def get_latest_metric(db: AsyncSession, agent_id: UUID) -> AgentMetric | None:
        """Latest agent metric for one agent.

        Production reads the fleet in one round-trip through `get_latest_metrics`
        below. This single-agent form is kept deliberately as that query's
        equivalence oracle in tests/agents/test_latest_metrics_batch.py — a
        batched query with no reference implementation to pin it against is how
        a batching rewrite silently starts reporting the wrong sample. It is the
        reference the batched shape is checked against, not a leftover caller.
        """
        result = await db.execute(
            select(AgentMetric)
            .where(AgentMetric.agent_id == agent_id)
            .order_by(AgentMetric.timestamp.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_latest_metrics(
        db: AsyncSession, agent_ids: Sequence[UUID]
    ) -> dict[UUID, AgentMetric]:
        """Latest metric per agent, in one round-trip.

        DISTINCT ON keeps the first row per agent under the same ordering the
        single-agent read uses, so the Prometheus rebuild costs one query
        instead of one per agent.

        Args:
            db: Database session.
            agent_ids: The agents to fetch for.

        Returns:
            agent_id -> its most recent AgentMetric, omitting agents with none.
        """
        if not agent_ids:
            return {}
        result = await db.execute(
            select(AgentMetric)
            .where(AgentMetric.agent_id.in_(agent_ids))
            .order_by(AgentMetric.agent_id, AgentMetric.timestamp.desc())
            .ext(distinct_on(AgentMetric.agent_id))
        )
        return {metric.agent_id: metric for metric in result.scalars()}

    @staticmethod
    async def get_hostnames_by_ids(
        db: AsyncSession, agent_ids: Sequence[UUID]
    ) -> dict[UUID, str]:
        """Hostname per agent, in one round-trip.

        Selects the two columns rather than whole Agent rows: the caller wants
        a label, and a page of jobs can span every agent in the fleet.

        Args:
            db: Database session.
            agent_ids: The agents to fetch for.

        Returns:
            agent_id -> hostname, omitting agents that do not exist.
        """
        if not agent_ids:
            return {}
        result = await db.execute(
            select(Agent.id, Agent.hostname).where(Agent.id.in_(agent_ids))
        )
        return {row.id: row.hostname for row in result}

    @staticmethod
    async def get_admitted_agents(db: AsyncSession) -> list[Agent]:
        """
        Get all admitted agents — those past registration triage.

        Returns agents whose approval_status is active, paused, or disabled
        (i.e. everything except pending and rejected). "Admitted" not
        "approved": only `active` agents are operationally approved — paused
        and disabled agents are blocked from submitting but retain historical
        results, so they belong in result-filter dropdowns (the sole callers).

        Args:
            db: Database session

        Returns:
            List of admitted Agent objects ordered by name
        """
        query = (
            select(Agent)
            .where(Agent.approval_status.not_in(["pending", "rejected"]))
            .order_by(Agent.agent_name)
        )
        result = await db.execute(query)
        return list(result.scalars().all())

    @staticmethod
    async def get_active_agent_count(db: AsyncSession, minutes: int = 10) -> int:
        """
        Get count of agents seen within the given time window.

        Args:
            db: Database session
            minutes: Time window in minutes

        Returns:
            Number of active agents
        """
        query = select(func.count(Agent.id)).where(
            Agent.last_seen >= utc_now() - timedelta(minutes=minutes)
        )
        result = await db.execute(query)
        return result.scalar_one()

    @staticmethod
    async def get_agent_metrics(
        db: AsyncSession, agent_id: UUID, cutoff_time: datetime
    ) -> Sequence[AgentMetric]:
        """
        Get agent metrics since a cutoff time, ordered ascending.

        Args:
            db: Database session
            agent_id: Agent UUID
            cutoff_time: Only return metrics after this time

        Returns:
            List of AgentMetric objects ordered by timestamp ascending
        """
        result = await db.execute(
            select(AgentMetric)
            .where(
                AgentMetric.agent_id == agent_id,
                AgentMetric.timestamp >= cutoff_time,
            )
            .order_by(AgentMetric.timestamp.asc())
        )
        return list(result.scalars().all())
