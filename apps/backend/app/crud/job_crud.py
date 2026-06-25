"""
Job CRUD - database queries for job operations.
"""

from collections.abc import Sequence
from datetime import timedelta
from uuid import UUID

from sqlalchemy import and_, delete, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.datetime_utils import utc_now
from app.models.agent_model import Agent
from app.models.job_model import Job


class JobCRUD:
    """Database queries for jobs."""

    @staticmethod
    async def agent_exists(db: AsyncSession, agent_id: UUID) -> bool:
        result = await db.execute(select(Agent.id).where(Agent.id == agent_id))
        return result.scalar_one_or_none() is not None

    @staticmethod
    async def get_by_id(db: AsyncSession, job_id: UUID) -> Job | None:
        result = await db.execute(select(Job).where(Job.id == job_id))
        return result.scalar_one_or_none()

    @staticmethod
    def _runner_filter(agent_id: UUID | None, server_only: bool):
        """Condition for the agent/server runner filter, or None for no filter.

        Single source of truth for "which runner ran this job": server_only maps
        to the server runner (Job.agent_id IS NULL); agent_id to a specific agent.
        """
        if server_only:
            return Job.agent_id.is_(None)
        if agent_id is not None:
            return Job.agent_id == agent_id
        return None

    @staticmethod
    async def list_paginated(
        db: AsyncSession,
        agent_id: UUID | None = None,
        server_only: bool = False,
        status: str | None = None,
        job_type: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[Sequence[Job], int]:
        """Paginated list of jobs with filtering."""
        query = select(Job)
        count_query = select(func.count()).select_from(Job)

        filters = []
        runner = JobCRUD._runner_filter(agent_id, server_only)
        if runner is not None:
            filters.append(runner)
        if status:
            filters.append(Job.status == status)
        if job_type:
            filters.append(Job.job_type == job_type)
        if filters:
            query = query.where(and_(*filters))
            count_query = count_query.where(and_(*filters))

        total = (await db.execute(count_query)).scalar_one()
        query = (
            query.order_by(Job.priority.desc(), Job.created_at.desc()).offset(offset).limit(limit)
        )
        result = await db.execute(query)
        return result.scalars().all(), total

    @staticmethod
    async def get_pending_for_agent(db: AsyncSession, agent_id: UUID, limit: int) -> Sequence[Job]:
        """Jobs in 'pending' status for an agent, ordered by priority."""
        result = await db.execute(
            select(Job)
            .where(and_(Job.agent_id == agent_id, Job.status == "pending"))
            .order_by(Job.priority.desc(), Job.created_at.asc())
            .limit(limit)
        )
        return result.scalars().all()

    @staticmethod
    async def delete_expired_terminal(db: AsyncSession) -> int:
        """Delete jobs in terminal state past their expires_at. Returns rowcount."""
        result = await db.execute(
            delete(Job).where(
                and_(
                    Job.expires_at.is_not(None),
                    Job.expires_at < utc_now(),
                    Job.status.in_(["completed", "failed", "cancelled"]),
                )
            )
        )
        return result.rowcount or 0  # type: ignore[attr-defined]

    @staticmethod
    async def get_status_counts(
        db: AsyncSession, agent_id: UUID | None = None, server_only: bool = False
    ) -> list[tuple[str, int]]:
        """List of (status, count) tuples, optionally filtered by runner."""
        query = select(Job.status, func.count(Job.id)).group_by(Job.status)
        runner = JobCRUD._runner_filter(agent_id, server_only)
        if runner is not None:
            query = query.where(runner)
        result = await db.execute(query)
        return [(row[0], int(row[1])) for row in result.all()]

    @staticmethod
    async def get_agents_with_jobs(db: AsyncSession) -> list[Agent]:
        """
        Get distinct agents that have jobs (excluding pending/rejected).

        Args:
            db: Database session

        Returns:
            List of Agent objects
        """
        agent_ids_query = select(Job.agent_id).distinct().where(Job.agent_id.isnot(None))
        result = await db.execute(agent_ids_query)
        agent_ids = [row[0] for row in result.all()]

        if not agent_ids:
            return []

        agents_query = (
            select(Agent)
            .where(Agent.id.in_(agent_ids))
            .where(Agent.approval_status.not_in(["pending", "rejected"]))
            .order_by(Agent.agent_name)
        )
        agents_result = await db.execute(agents_query)
        agents = agents_result.scalars().all()
        return list(agents)

    @staticmethod
    async def get_distinct_job_types(db: AsyncSession) -> list[str]:
        """
        Get all distinct job types ordered alphabetically.

        Args:
            db: Database session

        Returns:
            List of job type strings
        """
        query = select(Job.job_type).distinct().order_by(Job.job_type)
        result = await db.execute(query)
        return [row[0] for row in result.all()]

    @staticmethod
    async def list_jobs_with_agents(
        db: AsyncSession,
        status: str | None = None,
        job_type: str | None = None,
        agent_id: UUID | None = None,
        server_only: bool = False,
        priority: str | None = None,
        created: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> tuple[list[tuple], int]:
        """
        List jobs with agent data, filtering, and pagination.

        Args:
            db: Database session
            status: Filter by status
            job_type: Filter by job type
            agent_id: Filter by a specific agent (UUID)
            server_only: Only server-run jobs (agent_id IS NULL)
            priority: Filter by priority (high/normal/low)
            created: Filter by created time (1h/24h/7d/30d)
            limit: Max results
            offset: Pagination offset

        Returns:
            Tuple of (list of (Job, Agent|None) tuples, total count)
        """
        query = select(Job, Agent).outerjoin(Agent, Job.agent_id == Agent.id)

        conditions = []

        if status:
            conditions.append(Job.status == status)

        if job_type:
            conditions.append(Job.job_type == job_type)

        runner = JobCRUD._runner_filter(agent_id, server_only)
        if runner is not None:
            conditions.append(runner)

        if priority:
            if priority == "high":
                conditions.append(Job.priority >= 75)
            elif priority == "normal":
                conditions.append(and_(Job.priority >= 25, Job.priority < 75))
            elif priority == "low":
                conditions.append(Job.priority < 25)

        if created:
            now = utc_now()
            if created == "1h":
                conditions.append(Job.created_at >= now - timedelta(hours=1))
            elif created == "24h":
                conditions.append(Job.created_at >= now - timedelta(hours=24))
            elif created == "7d":
                conditions.append(Job.created_at >= now - timedelta(days=7))
            elif created == "30d":
                conditions.append(Job.created_at >= now - timedelta(days=30))

        if conditions:
            query = query.where(and_(*conditions))

        count_query = select(func.count()).select_from(query.subquery())
        total_result = await db.execute(count_query)
        total = total_result.scalar_one()

        query = query.order_by(desc(Job.created_at)).limit(limit).offset(offset)
        result = await db.execute(query)
        rows = result.all()

        return [(job, agent) for job, agent in rows], total

    @staticmethod
    async def get_job_status_summary(db: AsyncSession) -> dict:
        """
        Get job status summary counts.

        Args:
            db: Database session

        Returns:
            Dictionary with total, per-status counts, and active count
        """
        total_query = select(func.count(Job.id))
        total_result = await db.execute(total_query)
        total = total_result.scalar_one()

        status_query = select(Job.status, func.count(Job.id).label("count")).group_by(Job.status)
        status_result = await db.execute(status_query)
        status_counts = {row.status: row.count for row in status_result.all()}

        cutoff = utc_now() - timedelta(hours=1)
        active_query = select(func.count(Job.id)).where(
            and_(
                Job.status.in_(["running", "assigned"]),
                Job.created_at >= cutoff,
            )
        )
        active_result = await db.execute(active_query)
        active_count = active_result.scalar_one()

        return {
            "total": total,
            "pending": status_counts.get("pending", 0),
            "assigned": status_counts.get("assigned", 0),
            "running": status_counts.get("running", 0),
            "completed": status_counts.get("completed", 0),
            "failed": status_counts.get("failed", 0),
            "cancelled": status_counts.get("cancelled", 0),
            "active": active_count,
        }
