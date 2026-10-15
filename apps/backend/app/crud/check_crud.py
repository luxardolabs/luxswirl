"""
Check CRUD - database queries for check operations.
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import String, and_, cast, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, undefer

from app.core.datetime_utils import utc_now
from app.models.agent_model import Agent
from app.models.check_model import Check
from app.models.enum_model import AssignmentMode


class CheckCRUD:
    """Database queries for checks."""

    # ── Writes (flushed; the session owner commits) ───────────────────────────

    @staticmethod
    async def add_check(db: AsyncSession, check: Check) -> Check:
        """Insert a check; the refreshed row with its agent loaded."""
        db.add(check)
        await db.flush()
        await db.refresh(check, ["agent"])
        return check

    @staticmethod
    async def save_check(db: AsyncSession, check: Check) -> Check:
        """Write a check's changed fields; the refreshed row with its agent loaded."""
        await db.flush()
        await db.refresh(check, ["agent"])
        return check

    @staticmethod
    async def set_parent(
        db: AsyncSession,
        parent_check_id: UUID,
        attach: Sequence[Check],
        detach: Sequence[Check],
    ) -> None:
        """Point ``attach`` at the parent and clear ``detach``'s parent."""
        for check in attach:
            check.depends_on_check_id = parent_check_id
        for check in detach:
            check.depends_on_check_id = None
        await db.flush()

    @staticmethod
    async def load_agent(db: AsyncSession, check: Check) -> None:
        await db.refresh(check, ["agent"])

    @staticmethod
    async def delete_check(db: AsyncSession, check: Check) -> None:
        await db.delete(check)
        await db.flush()

    @staticmethod
    async def get_by_id(
        db: AsyncSession, check_id: UUID, *, include_script_code: bool = False
    ) -> Check | None:
        """Get a check by id with agent loaded (optionally undeferring script_code)."""
        query = (
            select(Check).where(Check.id == check_id).options(selectinload(Check.agent))
        )
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
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[Sequence[Check], int]:
        """Paginated list of checks with agent loaded and filters applied."""
        query = select(Check).options(
            selectinload(Check.agent), undefer(Check.script_code)
        )
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
            query.order_by(Check.agent_id, Check.display_name).offset(skip).limit(limit)
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
    async def get_with_agent_by_ids(
        db: AsyncSession, check_ids: list[UUID]
    ) -> Sequence[Check]:
        """Checks matching IDs with agent loaded."""
        if not check_ids:
            return []
        result = await db.execute(
            select(Check)
            .where(Check.id.in_(check_ids))
            .options(selectinload(Check.agent))
        )
        return result.scalars().all()

    @staticmethod
    async def bulk_delete_by_ids(db: AsyncSession, check_ids: list[UUID]) -> int:
        """Delete checks in bulk; the number deleted."""
        if not check_ids:
            return 0
        result = await db.execute(
            delete(Check).where(Check.id.in_(check_ids)).returning(Check.id)
        )
        return len(result.all())

    @staticmethod
    async def bulk_set_enabled(
        db: AsyncSession, check_ids: list[UUID], enabled: bool
    ) -> int:
        """Bulk update enabled flag; the number of checks updated."""
        if not check_ids:
            return 0
        result = await db.execute(
            update(Check)
            .where(Check.id.in_(check_ids))
            .values(enabled=enabled, updated_at=utc_now())
            .returning(Check.id)
        )
        return len(result.all())

    @staticmethod
    async def list_assignment_candidates(
        db: AsyncSession, agent_id: UUID
    ) -> list[Check]:
        """The checks that could run on `agent_id`: its own manual checks, and
        every replicate/distribute check (their selectors are matched in core).

        Another agent's manual check can never be assigned here, so it is not
        read: this runs on every agent's check sync.
        """
        result = await db.execute(
            select(Check).where(
                or_(
                    and_(
                        Check.assignment_mode == AssignmentMode.MANUAL,
                        Check.agent_id == agent_id,
                    ),
                    Check.assignment_mode.in_(
                        [AssignmentMode.REPLICATE, AssignmentMode.DISTRIBUTE]
                    ),
                )
            )
        )
        return list(result.scalars().all())

    @staticmethod
    async def list_without_parent(
        db: AsyncSession, exclude_check_id: UUID | None = None
    ) -> list[Check]:
        """Checks that do not depend on another, optionally excluding one."""
        query = select(Check).where(Check.depends_on_check_id.is_(None))
        if exclude_check_id is not None:
            query = query.where(Check.id != exclude_check_id)
        result = await db.execute(query)
        return list(result.scalars().all())

    @staticmethod
    async def list_non_parents(db: AsyncSession, exclude_check_id: UUID) -> list[Check]:
        """Checks no other check depends on, excluding `exclude_check_id`."""
        parents = (
            select(Check.depends_on_check_id)
            .where(Check.depends_on_check_id.is_not(None))
            .scalar_subquery()
        )
        result = await db.execute(
            select(Check).where(Check.id != exclude_check_id, Check.id.not_in(parents))
        )
        return list(result.scalars().all())

    @staticmethod
    async def parents_among(db: AsyncSession, check_ids: Sequence[UUID]) -> set[UUID]:
        """Which of `check_ids` some other check depends on."""
        if not check_ids:
            return set()
        result = await db.execute(
            select(Check.depends_on_check_id)
            .where(Check.depends_on_check_id.in_(check_ids))
            .distinct()
        )
        return {parent_id for parent_id in result.scalars() if parent_id is not None}

    @staticmethod
    async def list_dependents(
        db: AsyncSession, parent_check_id: UUID
    ) -> Sequence[Check]:
        """Checks that depend on the given parent.

        Args:
            db: Database session.
            parent_check_id: The parent whose children to list.

        Returns:
            The dependent checks.
        """
        result = await db.execute(
            select(Check).where(Check.depends_on_check_id == parent_check_id)
        )
        return list(result.scalars().all())

    @staticmethod
    async def count_dependents(db: AsyncSession, parent_check_id: UUID) -> int:
        result = await db.execute(
            select(func.count(Check.id)).where(
                Check.depends_on_check_id == parent_check_id
            )
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
        # The WHERE ... IN (parent ids) means no group key is NULL; mypy cannot
        # read that through the nullable column, so the comprehension narrows.
        return {parent: n for parent, n in result if parent is not None}

    @staticmethod
    async def count_for_agents_seen_since(db: AsyncSession, cutoff: datetime) -> int:
        """Count checks belonging to agents whose last_seen is >= cutoff."""
        result = await db.execute(
            select(func.count(Check.id))
            .join(Agent, Check.agent_id == Agent.id)
            .where(Agent.last_seen >= cutoff)
        )
        return result.scalar_one()

    @staticmethod
    async def count_by_agent(
        db: AsyncSession, agent_ids: Sequence[UUID]
    ) -> dict[UUID, tuple[int, int]]:
        """agent_id -> (checks, enabled checks), one grouped query for them all.

        Agents with no checks are absent.
        """
        if not agent_ids:
            return {}
        result = await db.execute(
            select(
                Check.agent_id,
                func.count(Check.id),
                func.count(Check.id).filter(Check.enabled.is_(True)),
            )
            .where(Check.agent_id.in_(agent_ids))
            .group_by(Check.agent_id)
        )
        return {agent_id: (total, enabled) for agent_id, total, enabled in result.all()}

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
        query = select(Check.check_type).distinct().order_by(Check.check_type)
        # unbounded-reads: DISTINCT over the check-type catalogue: one row per type
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
            if row[0] and isinstance(row[0], list):
                all_tags.update(tag.strip() for tag in row[0] if tag and tag.strip())

        # Agent tags (PostgreSQL array)
        agents_query = select(Agent.tags).where(Agent.tags.isnot(None))
        agents_result = await db.execute(agents_query)
        for agent_row in agents_result.all():
            if agent_row[0]:
                all_tags.update(
                    tag.strip() for tag in agent_row[0] if tag and tag.strip()
                )

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

        query = (
            select(Check)
            .where(Check.id.in_(check_ids))
            .options(selectinload(Check.agent))
        )
        result = await db.execute(query)
        return list(result.scalars().all())
