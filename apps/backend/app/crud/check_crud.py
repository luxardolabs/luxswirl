"""
Check CRUD - database queries for check operations.
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import String, cast, delete, distinct, func, or_, select, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, undefer

from app.core.datetime_utils import utc_now
from app.models.agent_model import Agent
from app.models.check_model import Check


class CheckCRUD:
    """Database queries for checks."""

    @staticmethod
    async def get_by_id(
        db: AsyncSession, check_id: UUID, *, include_script_code: bool = False
    ) -> Check | None:
        """Get a check by id with agent loaded (optionally undeferring script_code)."""
        query = select(Check).where(Check.id == check_id).options(selectinload(Check.agent))
        if include_script_code:
            query = query.options(undefer(Check.script_code))
        result = await db.execute(query)
        return result.scalar_one_or_none()

    @staticmethod
    async def list_paginated(
        db: AsyncSession,
        *,
        agent_id: UUID | None = None,
        check_type: str | None = None,
        enabled_only: bool = False,
        tag: str | None = None,
        search: str | None = None,
        exclude_internal: bool = False,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[Sequence[Check], int]:
        """Paginated list of checks with agent loaded and filters applied."""
        query = select(Check).options(selectinload(Check.agent), undefer(Check.script_code))
        count_query = select(func.count(Check.id))

        if agent_id is not None:
            query = query.where(Check.agent_id == agent_id)
            count_query = count_query.where(Check.agent_id == agent_id)
        if check_type:
            query = query.where(Check.check_type == check_type)
            count_query = count_query.where(Check.check_type == check_type)
        if enabled_only:
            query = query.where(Check.enabled.is_(True))
            count_query = count_query.where(Check.enabled.is_(True))
        if tag:
            tag_clause = Check.tags.op("@>")(cast([tag], ARRAY(String)))
            query = query.where(tag_clause)
            count_query = count_query.where(tag_clause)
        if search:
            pattern = f"%{search}%"
            search_clause = or_(
                Check.display_name.ilike(pattern),
                Check.target.ilike(pattern),
            )
            query = query.where(search_clause)
            count_query = count_query.where(search_clause)
        if exclude_internal:
            query = query.where(Check.check_type != "internal")
            count_query = count_query.where(Check.check_type != "internal")

        total = (await db.execute(count_query)).scalar_one()
        result = await db.execute(
            query.order_by(Check.agent_id, Check.display_name).offset(offset).limit(limit)
        )
        return result.scalars().all(), total

    @staticmethod
    async def list_for_agent(db: AsyncSession, agent_id: UUID) -> Sequence[Check]:
        """All checks for an agent, ordered by display_name, with agent + script_code loaded."""
        result = await db.execute(
            select(Check)
            .where(Check.agent_id == agent_id)
            .options(selectinload(Check.agent), undefer(Check.script_code))
            .order_by(Check.display_name)
        )
        return result.scalars().all()

    @staticmethod
    async def get_with_agent_by_ids(db: AsyncSession, check_ids: list[UUID]) -> Sequence[Check]:
        """Checks matching IDs with agent loaded."""
        if not check_ids:
            return []
        result = await db.execute(
            select(Check).where(Check.id.in_(check_ids)).options(selectinload(Check.agent))
        )
        return result.scalars().all()

    @staticmethod
    async def bulk_delete_by_ids(db: AsyncSession, check_ids: list[UUID]) -> int:
        """Delete checks in bulk. Returns rowcount."""
        if not check_ids:
            return 0
        result = await db.execute(delete(Check).where(Check.id.in_(check_ids)))
        return result.rowcount or 0  # type: ignore[attr-defined]

    @staticmethod
    async def bulk_set_enabled(db: AsyncSession, check_ids: list[UUID], enabled: bool) -> int:
        """Bulk update enabled flag. Returns rowcount."""
        if not check_ids:
            return 0
        result = await db.execute(
            update(Check)
            .where(Check.id.in_(check_ids))
            .values(enabled=enabled, updated_at=utc_now())
        )
        return result.rowcount or 0  # type: ignore[attr-defined]

    @staticmethod
    async def list_all(db: AsyncSession) -> list[Check]:
        """Return all checks (unfiltered, unsorted)."""
        result = await db.execute(select(Check))
        return list(result.scalars().all())

    @staticmethod
    async def count_dependents(db: AsyncSession, parent_check_id: UUID) -> int:
        result = await db.execute(
            select(func.count(Check.id)).where(Check.depends_on_check_id == parent_check_id)
        )
        return result.scalar_one()

    @staticmethod
    async def count_dependents_bulk(
        db: AsyncSession, parent_check_ids: list[UUID]
    ) -> dict[UUID, int]:
        if not parent_check_ids:
            return {}
        result = await db.execute(
            select(Check.depends_on_check_id, func.count(Check.id))
            .where(Check.depends_on_check_id.in_(parent_check_ids))
            .group_by(Check.depends_on_check_id)
        )
        return {row[0]: row[1] for row in result.all()}

    @staticmethod
    async def count_for_agents_seen_since(db: AsyncSession, cutoff) -> int:
        """Count checks belonging to agents whose last_seen is >= cutoff."""
        result = await db.execute(
            select(func.count(Check.id))
            .join(Agent, Check.agent_id == Agent.id)
            .where(Agent.last_seen >= cutoff)
        )
        return result.scalar_one()

    @staticmethod
    async def count_all(db: AsyncSession) -> int:
        """Total check count (including internal/system checks)."""
        result = await db.execute(select(func.count(Check.id)))
        return result.scalar_one()

    @staticmethod
    async def get_distinct_check_types(db: AsyncSession) -> list[str]:
        """
        Get all unique check types from checks.

        Args:
            db: Database session

        Returns:
            Sorted list of unique check type strings
        """
        query = select(distinct(Check.check_type)).order_by(Check.check_type)
        result = await db.execute(query)
        return [row[0] for row in result.all()]

    @staticmethod
    async def get_all_check_tags(db: AsyncSession) -> list[str]:
        """
        Get all unique tags from all checks.

        Args:
            db: Database session

        Returns:
            Sorted list of unique tag strings
        """
        query = select(Check.tags).where(Check.tags.isnot(None))
        result = await db.execute(query)

        all_tags = set()
        for row in result:
            if row[0]:
                all_tags.update(row[0])

        return sorted(all_tags)

    @staticmethod
    async def get_all_tags_combined(db: AsyncSession) -> list[str]:
        """
        Get all distinct tags from both checks (array) and agents (comma-separated).

        Args:
            db: Database session

        Returns:
            Sorted list of unique tag strings
        """
        all_tags: set[str] = set()

        # Check tags (PostgreSQL array)
        checks_query = select(Check.tags).where(Check.tags.isnot(None))
        checks_result = await db.execute(checks_query)
        for row in checks_result.all():
            if row[0]:
                if isinstance(row[0], list):
                    all_tags.update(tag.strip() for tag in row[0] if tag and tag.strip())

        # Agent tags (PostgreSQL array)
        agents_query = select(Agent.tags).where(Agent.tags.isnot(None))
        agents_result = await db.execute(agents_query)
        for agent_row in agents_result.all():
            if agent_row[0]:
                all_tags.update(tag.strip() for tag in agent_row[0] if tag and tag.strip())

        return sorted(all_tags)

    @staticmethod
    async def get_checks_by_ids(db: AsyncSession, check_ids: list[UUID]) -> list[Check]:
        """
        Get multiple checks by their IDs with agent relationship loaded.

        Args:
            db: Database session
            check_ids: List of check UUIDs

        Returns:
            List of Check objects with agent loaded
        """
        if not check_ids:
            return []

        query = select(Check).where(Check.id.in_(check_ids)).options(selectinload(Check.agent))
        result = await db.execute(query)
        return list(result.scalars().all())
