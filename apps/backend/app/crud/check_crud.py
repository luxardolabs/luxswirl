"""
Check CRUD - database queries for check operations.
"""

from collections.abc import Collection, Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    String,
    and_,
    cast,
    delete,
    func,
    inspect,
    or_,
    select,
    true,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, undefer
from sqlalchemy.sql import ColumnElement

from app.core.datetime_utils import utc_now
from app.crud._check_health import current_result, health_condition
from app.crud._check_search import ids_matching_search
from app.models.agent_model import Agent
from app.models.check_model import Check
from app.models.enum_model import AssignmentMode, CheckHealthStatus, CheckType

# The first key of the two-key advisory lock that keeps the writers choosing
# an agent's check names to one transaction at a time (the second is the agent
# id's hashtext). Any constant works: the two-key space does not overlap the
# one-key space other advisory locks use.
_CHECK_NAMES_LOCK_CLASS = 1_826_301_559


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
        """Write a check's changed fields; the row with what the database set
        reloaded and its agent still loaded.

        Only the attributes the flush expired (the server-set `updated_at`) are
        reloaded. Refreshing the whole row, or its `agent`, leaves `agent`
        unloaded whenever the agent's `checks` are loaded in the session, and
        the next read of it would be a lazy load, which async cannot do.
        """
        await db.flush()
        expired = inspect(check).expired_attributes
        if expired:
            await db.refresh(check, list(expired))
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
    async def key_share_existing(
        db: AsyncSession, check_ids: Sequence[UUID]
    ) -> set[UUID]:
        """Those of `check_ids` whose check still exists, each locked FOR KEY SHARE
        to the transaction's end.

        It is the lock an insert of a row referencing the check takes: a delete of
        one of them waits for this transaction, and a check deleted before this
        read is left out, so its results are not inserted against a missing row.
        The rows are locked in id order, the order an agent delete removes its
        checks in, so the two never wait on each other in a cycle. Stated limit: a
        check whose every result turns out a duplicate (skipped by the insert)
        stays locked to the transaction's end too.
        """
        if not check_ids:
            return set()
        rows = await db.scalars(
            select(Check.id)
            .where(Check.id.in_(set(check_ids)))
            .order_by(Check.id)
            .with_for_update(read=True, key_share=True)
        )
        return set(rows)

    @staticmethod
    async def lock_for_delete(
        db: AsyncSession,
        *,
        check_ids: Sequence[UUID] | None = None,
        agent_id: UUID | None = None,
    ) -> set[UUID]:
        """Lock every check a delete writes, in id order, before it removes any;
        the ids locked. That is the checks it removes (`check_ids`, or every
        check of `agent_id`) and each check depending on one of them, whose
        parent the delete clears.

        All of them are held FOR NO KEY UPDATE in one pass, as the clearing of a
        parent takes them: a report's FOR KEY SHARE on a dependent (another
        agent's included) is not held up by the delete. Then the removed ones
        alone are held FOR UPDATE. A report, and every other writer of many
        checks, locks in the same order, so none waits on another in a cycle,
        whatever order the delete then writes the rows in (the ORM removes a
        dependent check before its parent).
        """
        removed = (
            Check.agent_id == agent_id
            if agent_id is not None
            else Check.id.in_(set(check_ids or ()))
        )
        written = or_(
            removed,
            Check.depends_on_check_id.in_(
                select(Check.id).where(removed).correlate(None)
            ),
        )
        held = await CheckCRUD._lock_in_id_order(db, written, key_share=True)
        await CheckCRUD._lock_in_id_order(db, removed, key_share=False)
        return held

    @staticmethod
    async def flush_pending(db: AsyncSession) -> None:
        """Write the session's pending changes, so a savepoint opened next and
        rolled back cannot take them with it."""
        await db.flush()

    @staticmethod
    async def lock_for_change(db: AsyncSession, check_ids: Collection[UUID]) -> None:
        """Lock `check_ids` FOR NO KEY UPDATE in id order, in one pass, as every
        writer of many checks locks them: a write that touches several checks
        (a check and the parent it now references, a parent and its children)
        holds none while it waits for another."""
        await CheckCRUD._lock_in_id_order(
            db, Check.id.in_(set(check_ids)), key_share=True
        )

    @staticmethod
    async def _lock_in_id_order(
        db: AsyncSession, condition: ColumnElement[bool], *, key_share: bool
    ) -> set[UUID]:
        """Lock the checks `condition` selects in id order: FOR UPDATE, or FOR NO
        KEY UPDATE with `key_share` (a write that changes no key, which a report's
        KEY SHARE lock does not wait for); the ids locked. Every writer of many
        checks locks them this way first, so no two wait on each other in a
        cycle."""
        rows = await db.scalars(
            select(Check.id)
            .where(condition)
            .order_by(Check.id)
            .with_for_update(key_share=key_share)
        )
        return set(rows)

    @staticmethod
    async def ids_for_agent(db: AsyncSession, agent_id: UUID) -> set[UUID]:
        """The ids of `agent_id`'s checks as committed now, read without a lock."""
        return set(await db.scalars(select(Check.id).where(Check.agent_id == agent_id)))

    @staticmethod
    async def delete_check(db: AsyncSession, check: Check) -> None:
        """Delete the check and its alert mappings.

        The check's row is locked first: a transaction that holds it FOR SHARE
        (an alert change judging it) finishes before the mappings are deleted,
        so the two never wait on each other's rows.
        """
        await db.execute(select(Check.id).where(Check.id == check.id).with_for_update())
        await db.delete(check)
        await db.flush()

    @staticmethod
    async def exists(db: AsyncSession, check_id: UUID) -> bool:
        """Whether the check exists."""
        result = await db.execute(select(Check.id).where(Check.id == check_id))
        return result.scalar_one_or_none() is not None

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
    async def get_locked(
        db: AsyncSession,
        check_id: UUID,
        *,
        include_script_code: bool = False,
        for_delete: bool = False,
    ) -> Check | None:
        """The check as stored now, its row held to the transaction's end: FOR
        UPDATE for a delete, else FOR NO KEY UPDATE, which a report's KEY SHARE
        lock does not wait for. A write in flight on the row is waited for, so
        what the caller judges is what it then changes. The session's pending
        changes are flushed first, so reading the row as stored keeps them."""
        await db.flush()
        query = (
            select(Check)
            .where(Check.id == check_id)
            .options(selectinload(Check.agent))
            .with_for_update(key_share=not for_delete)
            .execution_options(populate_existing=True)
        )
        if include_script_code:
            query = query.options(undefer(Check.script_code))
        result = await db.execute(query)
        return result.scalar_one_or_none()

    @staticmethod
    async def get_locked_by_ids(
        db: AsyncSession, check_ids: Collection[UUID], *, for_delete: bool = False
    ) -> Sequence[Check]:
        """`check_ids` as stored now, with agent loaded, their rows held in id
        order (as every writer of many checks locks them) to the transaction's
        end: FOR UPDATE for a delete, else FOR NO KEY UPDATE. The session's
        pending changes are flushed first, so reading the rows keeps them."""
        if not check_ids:
            return []
        await db.flush()
        result = await db.execute(
            select(Check)
            .where(Check.id.in_(set(check_ids)))
            .order_by(Check.id)
            .options(selectinload(Check.agent))
            .with_for_update(key_share=not for_delete)
            .execution_options(populate_existing=True)
        )
        return result.scalars().all()

    @staticmethod
    async def agent_has_synthetic(db: AsyncSession, agent_id: UUID) -> bool:
        """Whether `agent_id` owns a synthetic check, as committed now."""
        found = await db.execute(
            select(Check.id)
            .where(Check.agent_id == agent_id, Check.check_type == CheckType.SYNTHETIC)
            .limit(1)
        )
        return found.first() is not None

    @staticmethod
    async def list_paginated(
        db: AsyncSession,
        *,
        agent_id: UUID | None = None,
        check_type: str | None = None,
        enabled_only: bool = False,
        tag: str | None = None,
        search: str | None = None,
        search_full_target: bool = False,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[Sequence[Check], int]:
        """Paginated list of checks with agent loaded and filters applied."""
        clauses = await CheckCRUD._list_filters(
            db,
            agent_id=agent_id,
            check_type=check_type,
            enabled_only=enabled_only,
            tag=tag,
            search=search,
            search_full_target=search_full_target,
        )
        total = (
            await db.execute(select(func.count(Check.id)).where(*clauses))
        ).scalar_one()
        result = await db.execute(
            select(Check)
            .options(selectinload(Check.agent), undefer(Check.script_code))
            .where(*clauses)
            .order_by(Check.agent_id, Check.display_name)
            .offset(skip)
            .limit(limit)
        )
        return result.scalars().all(), total

    @staticmethod
    async def list_page_with_health(
        db: AsyncSession,
        *,
        agent_id: UUID | None = None,
        check_type: str | None = None,
        enabled_only: bool = False,
        tag: str | None = None,
        search: str | None = None,
        search_full_target: bool = False,
        status: CheckHealthStatus | None = None,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[tuple[Check, bool | None, float | None]], int]:
        """`list_paginated`'s page, each check with its current result's success
        and latency (None, None when it has none), filtered on that health.

        One statement reads both the filter and the health it serves, so a
        result landing mid-request cannot list a check under one status and
        serve it with another.
        """
        current = current_result()
        clauses = await CheckCRUD._list_filters(
            db,
            agent_id=agent_id,
            check_type=check_type,
            enabled_only=enabled_only,
            tag=tag,
            search=search,
            search_full_target=search_full_target,
        )
        if status is not None:
            clauses.append(health_condition(current, status))
        total = (
            await db.execute(
                select(func.count(Check.id)).outerjoin(current, true()).where(*clauses)
            )
        ).scalar_one()
        result = await db.execute(
            select(Check, current.c.success, current.c.latency_ms)
            .options(selectinload(Check.agent), undefer(Check.script_code))
            .outerjoin(current, true())
            .where(*clauses)
            .order_by(Check.agent_id, Check.display_name)
            .offset(skip)
            .limit(limit)
        )
        return [
            (check, success, latency_ms) for check, success, latency_ms in result.all()
        ], total

    @staticmethod
    async def _list_filters(
        db: AsyncSession,
        *,
        agent_id: UUID | None,
        check_type: str | None,
        enabled_only: bool,
        tag: str | None,
        search: str | None,
        search_full_target: bool,
    ) -> list[ColumnElement[bool]]:
        """The WHERE clauses the check lists share."""
        clauses: list[ColumnElement[bool]] = []
        if agent_id is not None:
            clauses.append(Check.agent_id == agent_id)
        if check_type:
            clauses.append(Check.check_type == check_type)
        if enabled_only:
            clauses.append(Check.enabled.is_(True))
        if tag:
            clauses.append(Check.tags.op("@>")(cast([tag], ARRAY(String))))
        if search:
            matching = await ids_matching_search(
                db,
                select(Check.id, Check.display_name, Check.target).where(*clauses),
                search,
                full_target=search_full_target,
            )
            clauses.append(Check.id.in_(matching))
        return clauses

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
    async def hold_names(db: AsyncSession, agent_id: UUID) -> None:
        """Take the transaction's advisory lock on the names of `agent_id`'s
        checks: a second writer that reads them to choose new names waits for
        this transaction's end and then reads what it made. The lock excludes
        those writers alone; the agent's row stays free for its own requests."""
        await db.execute(
            select(
                func.pg_advisory_xact_lock(
                    _CHECK_NAMES_LOCK_CLASS, func.hashtext(str(agent_id))
                )
            )
        )

    @staticmethod
    async def display_names_for_agent(
        db: AsyncSession, agent_id: UUID
    ) -> Sequence[str]:
        """The display name of each of an agent's checks."""
        result = await db.execute(
            select(Check.display_name).where(Check.agent_id == agent_id)
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
        """Bulk update enabled flag; the number of checks updated.

        The rows are locked in id order first (an UPDATE takes them in scan
        order), as a bulk delete and a report lock them.
        """
        if not check_ids:
            return 0
        await CheckCRUD._lock_in_id_order(db, Check.id.in_(check_ids), key_share=True)
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
    async def list_enabled(db: AsyncSession) -> list[Check]:
        """Every enabled check."""
        # unbounded-reads: the silence pass resolves every enabled check's assigned agents; all are the input
        result = await db.execute(select(Check).where(Check.enabled.is_(True)))
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

    @staticmethod
    async def share_lock_checks(db: AsyncSession, check_ids: list[UUID]) -> list[Check]:
        """`check_ids`' checks, each held FOR SHARE until the transaction ends.

        A change to a held check (its type, say) waits for this transaction, so
        what was judged of it stays true until the judged write commits. NOWAIT:
        a check another transaction is changing raises at once (PostgreSQL
        lock_not_available, 55P03) rather than waiting, so this read never
        waits on a check writer, and never deadlocks with one. The savepoint
        keeps the caller's transaction usable after that refusal; the locks
        taken inside it are held to the transaction's end.
        """
        if not check_ids:
            return []
        async with db.begin_nested():
            result = await db.execute(
                select(Check)
                .where(Check.id.in_(check_ids))
                .order_by(Check.id)
                .with_for_update(read=True, nowait=True)
                # The locked row's values, not a copy this session loaded earlier.
                .execution_options(populate_existing=True)
            )
            return list(result.scalars().all())
