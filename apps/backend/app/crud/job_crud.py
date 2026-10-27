"""
Job CRUD - database queries for job operations.
"""

from collections.abc import Sequence
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, and_, delete, desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.datetime_utils import utc_now
from app.models.agent_model import Agent
from app.models.enum_model import (
    UNADMITTED_AGENT_STATUSES,
    AgentApprovalStatus,
    JobStatus,
)
from app.models.job_model import Job

_FINISHED = (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)


class JobCRUD:
    """Database queries for jobs."""

    # ── Writes (flushed; the session owner commits) ───────────────────────────

    @staticmethod
    async def add_job(db: AsyncSession, job: Job) -> Job:
        """Insert a job; the refreshed row."""
        db.add(job)
        await db.flush()
        await db.refresh(job)
        return job

    @staticmethod
    async def save_job(db: AsyncSession, job: Job) -> Job:
        """Write a job's changed fields; the refreshed row."""
        await db.flush()
        await db.refresh(job)
        return job

    @staticmethod
    async def delete_job(db: AsyncSession, job: Job) -> None:
        await db.delete(job)

    @staticmethod
    async def agent_approval_status(
        db: AsyncSession, agent_id: UUID
    ) -> AgentApprovalStatus | None:
        """The agent's approval status, or None if there is no such agent."""
        result = await db.execute(
            select(Agent.approval_status).where(Agent.id == agent_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_id(db: AsyncSession, job_id: UUID) -> Job | None:
        result = await db.execute(select(Job).where(Job.id == job_id))
        return result.scalar_one_or_none()

    @staticmethod
    async def move_open_job(
        db: AsyncSession,
        job_id: UUID,
        status: JobStatus,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> Job | None:
        """Move an unfinished job to `status` in one statement; the moved row.

        None when the job is finished, already in `status`, cannot move there
        from where it is (only an assigned job, one dispatched to its agent,
        starts running), or is absent: the statement's own WHERE holds the
        check, so a result or cancel landing at the same moment is never
        overwritten. The start or finish time is stamped once; a completed
        job stores its `result`, a failed one its `error`.
        """
        values: dict[str, object] = {"status": status}
        if result is not None:
            values["result"] = result
        if error is not None:
            values["error"] = error
        movable: ColumnElement[bool] = Job.status.not_in(_FINISHED)
        if status == JobStatus.RUNNING:
            values["started_at"] = func.coalesce(Job.started_at, utc_now())
            movable = Job.status == JobStatus.ASSIGNED
        elif status in _FINISHED:
            values["completed_at"] = func.coalesce(Job.completed_at, utc_now())
        moved = await db.execute(
            update(Job)
            .where(Job.id == job_id, movable, Job.status != status)
            .values(**values)
            .returning(Job)
            # The row the database returned, never the statement's values
            # written onto a copy the session already holds (which may be stale).
            .execution_options(populate_existing=True, synchronize_session=False)
        )
        return moved.scalar_one_or_none()

    @staticmethod
    async def get_current(db: AsyncSession, job_id: UUID) -> Job | None:
        """The job as the database holds it now, refreshing any copy the session has."""
        result = await db.execute(
            select(Job)
            .where(Job.id == job_id)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _runner_filter(
        agent_id: UUID | None, server_only: bool
    ) -> ColumnElement[bool] | None:
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
        *,
        agent_id: UUID | None = None,
        server_only: bool = False,
        status: str | None = None,
        job_type: str | None = None,
        skip: int = 0,
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
            query.order_by(Job.priority.desc(), Job.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        result = await db.execute(query)
        return result.scalars().all(), total

    @staticmethod
    async def get_pending_for_agent(
        db: AsyncSession, agent_id: UUID, limit: int
    ) -> Sequence[Job]:
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
        """Delete jobs in terminal state past their expires_at; returns how many.

        Counts the ids DELETE ... RETURNING hands back: typed, unlike `rowcount`.
        The purge runs on a schedule, so each batch is what expired since the last.
        """
        result = await db.execute(
            delete(Job)
            .where(
                and_(
                    Job.expires_at.is_not(None),
                    Job.expires_at < utc_now(),
                    Job.status.in_(["completed", "failed", "cancelled"]),
                )
            )
            .returning(Job.id)
        )
        return len(result.scalars().all())

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
        agent_ids_query = (
            select(Job.agent_id).distinct().where(Job.agent_id.isnot(None))
        )
        result = await db.execute(agent_ids_query)
        agent_ids = [row[0] for row in result.all()]

        if not agent_ids:
            return []

        agents_query = (
            select(Agent)
            .where(Agent.id.in_(agent_ids))
            .where(Agent.approval_status.not_in(UNADMITTED_AGENT_STATUSES))
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
        # unbounded-reads: DISTINCT over the job-type catalogue: one row per type
        result = await db.execute(query)
        return [row[0] for row in result.all()]

    @staticmethod
    async def list_jobs_with_agents(
        db: AsyncSession,
        *,
        status: str | None = None,
        job_type: str | None = None,
        agent_id: UUID | None = None,
        server_only: bool = False,
        priority: str | None = None,
        created: str | None = None,
        skip: int = 0,
        limit: int = 25,
    ) -> tuple[list[tuple[Job, Agent | None]], int]:
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
            skip: Rows to skip
            limit: Max results

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

        query = query.order_by(desc(Job.created_at)).offset(skip).limit(limit)
        result = await db.execute(query)
        rows = result.all()

        return [(job, agent) for job, agent in rows], total

    @staticmethod
    async def get_job_status_summary(db: AsyncSession) -> dict[str, int]:
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

        # unbounded-reads: GROUP BY status, an enum of 6 members (JobStatus): at most 6 rows
        status_result = await db.execute(
            select(Job.status, func.count(Job.id)).group_by(Job.status)
        )
        status_counts = dict(status_result.all())

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
            **{status.value: status_counts.get(status, 0) for status in JobStatus},
            "active": active_count,
        }
