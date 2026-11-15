"""
Alert decision CRUD - the queue of results awaiting their alert decisions,
and each check's decision cursor.
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alert_decision_model import AlertDecisionCursor, AlertDecisionQueue

# The first key of the two-key advisory locks that keep one check's alert
# decisions to one transaction at a time (the second is the check's key,
# `AlertDecisionCoreService.decision_lock_key`). Any constant works: the
# two-key space does not overlap the one-key space other advisory locks use.
_ALERT_DECISION_LOCK_CLASS = 1_826_301_558


class AlertDecisionCRUD:
    """Database queries for the alert decision queue and cursors."""

    @staticmethod
    async def enqueue(db: AsyncSession, keys: Sequence[tuple[UUID, datetime]]) -> None:
        """Queue these results' `(check_id, timestamp)` keys for their alert
        decisions; a key already queued is left as it is.

        Inserted in key order, so two reports queueing the same results never
        wait on each other in a cycle.
        """
        if not keys:
            return
        rows = [
            {"check_id": check_id, "result_timestamp": timestamp}
            for check_id, timestamp in sorted(set(keys))
        ]
        await db.execute(
            pg_insert(AlertDecisionQueue).values(rows).on_conflict_do_nothing()
        )

    @staticmethod
    async def checks_with_queued(db: AsyncSession, limit: int) -> list[UUID]:
        """Up to `limit` checks with queued results, the longest waiting first."""
        result = await db.execute(
            select(AlertDecisionQueue.check_id)
            .group_by(AlertDecisionQueue.check_id)
            .order_by(func.min(AlertDecisionQueue.result_timestamp))
            .limit(limit)
        )
        return list(result.scalars().all())

    @staticmethod
    async def try_lock_check(db: AsyncSession, key: int) -> bool:
        """Take the transaction's advisory lock on a check's alert decisions
        without waiting; whether it was had."""
        result = await db.execute(
            select(func.pg_try_advisory_xact_lock(_ALERT_DECISION_LOCK_CLASS, key))
        )
        return bool(result.scalar_one())

    @staticmethod
    async def queued_for_check(
        db: AsyncSession, check_id: UUID, limit: int
    ) -> list[datetime]:
        """The oldest `limit` queued result timestamps of a check, oldest first."""
        result = await db.execute(
            select(AlertDecisionQueue.result_timestamp)
            .where(AlertDecisionQueue.check_id == check_id)
            .order_by(AlertDecisionQueue.result_timestamp)
            .limit(limit)
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_cursor(db: AsyncSession, check_id: UUID) -> datetime | None:
        """The timestamp a check's results are decided through, or None when
        none has been."""
        result = await db.execute(
            select(AlertDecisionCursor.decided_through).where(
                AlertDecisionCursor.check_id == check_id
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def advance(db: AsyncSession, check_id: UUID, through: datetime) -> None:
        """Mark a check's results decided through `through`: its queued
        results no newer are removed, and its cursor moves to `through`
        (never back)."""
        await db.execute(
            delete(AlertDecisionQueue).where(
                AlertDecisionQueue.check_id == check_id,
                AlertDecisionQueue.result_timestamp <= through,
            )
        )
        insert = pg_insert(AlertDecisionCursor).values(
            check_id=check_id, decided_through=through
        )
        await db.execute(
            insert.on_conflict_do_update(
                index_elements=[AlertDecisionCursor.check_id],
                set_={
                    "decided_through": func.greatest(
                        AlertDecisionCursor.decided_through,
                        insert.excluded.decided_through,
                    )
                },
            )
        )
