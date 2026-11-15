"""
NotificationLog CRUD - database queries for notification log operations.
"""

from collections.abc import Collection
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Row,
    Select,
    and_,
    delete,
    exists,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.agent_model import Agent
from app.models.alert_check_mapping_model import AlertCheckMapping
from app.models.alert_model import Alert
from app.models.check_model import Check
from app.models.enum_model import NotificationStatus
from app.models.notification_log_model import NotificationLog
from app.models.notification_provider_model import NotificationProvider

# The table-row select spans 15 columns; SQLAlchemy types only the first ten,
# and cannot know the outer-joined check columns may be NULL. Rows are
# validated into NotificationLogEntry in core, which carries the real types.
LogRowSelect = Select[*tuple[Any, ...]]
LogRow = Row[*tuple[Any, ...]]


class NotificationLogCRUD:
    """Database queries for notification logs."""

    # ── Writes (flushed; the session owner commits) ───────────────────────────

    @staticmethod
    async def add_log(db: AsyncSession, log_entry: NotificationLog) -> NotificationLog:
        db.add(log_entry)
        await db.flush()
        await db.refresh(log_entry)
        return log_entry

    @staticmethod
    async def save_logs(db: AsyncSession) -> None:
        """Write the changed fields of the notification rows the session holds."""
        await db.flush()

    @staticmethod
    async def claim_due(
        db: AsyncSession,
        now: datetime,
        limit: int,
        *,
        provider_share: int,
        held: Collection[UUID] = (),
    ) -> list[NotificationLog]:
        """Up to `limit` rows due for delivery, and the first due row of each
        provider with nothing in flight beyond them, locked FOR UPDATE SKIP
        LOCKED.

        Due: pending or retrying with `next_attempt_at` reached, or sending
        with its lease lapsed (its worker stopped). A row is held back while
        an earlier-decided row of the same alert, subject (check or agent)
        and provider still awaits delivery, so one subject's notifications
        reach a provider in the order they were decided: a recovery never
        overtakes its outage.

        A provider's rows in flight are its `sending` rows under a live
        lease, whichever server process holds them. Rows are taken in turn
        across providers, the provider with the fewest in flight first (each
        provider's own in decision order), and no provider is given more
        than `provider_share` in flight in all, so providers whose every
        attempt waits out its timeout never fill the slots between them. A
        provider with nothing in flight is given its first row whether or not
        a slot is free, so one that comes due while every slot is held, by
        however many providers that never answer, is never left waiting for
        one to free. A row in `held` (this process's own attempts, still
        unrecorded) is never claimed, whatever its lease says.
        """
        earlier = aliased(NotificationLog)
        awaiting = [
            NotificationStatus.PENDING,
            NotificationStatus.SENDING,
            NotificationStatus.RETRYING,
        ]
        held_back = exists().where(
            earlier.alert_id == NotificationLog.alert_id,
            earlier.notification_provider_id
            == NotificationLog.notification_provider_id,
            earlier.check_id.is_not_distinct_from(NotificationLog.check_id),
            earlier.agent_id.is_not_distinct_from(NotificationLog.agent_id),
            earlier.status.in_(awaiting),
            earlier.decision_seq < NotificationLog.decision_seq,
        )
        due = or_(
            and_(
                NotificationLog.status.in_(
                    [NotificationStatus.PENDING, NotificationStatus.RETRYING]
                ),
                NotificationLog.next_attempt_at <= now,
            ),
            and_(
                NotificationLog.status == NotificationStatus.SENDING,
                NotificationLog.lease_expires_at <= now,
            ),
        )
        in_flight = (
            select(
                NotificationLog.notification_provider_id.label("provider_id"),
                func.count().label("held"),
            )
            .where(
                NotificationLog.status == NotificationStatus.SENDING,
                NotificationLog.lease_expires_at > now,
            )
            .group_by(NotificationLog.notification_provider_id)
            .subquery()
        )
        ranked = (
            select(
                NotificationLog.id,
                NotificationLog.notification_provider_id,
                NotificationLog.decision_seq,
                func.row_number()
                .over(
                    partition_by=NotificationLog.notification_provider_id,
                    order_by=NotificationLog.decision_seq,
                )
                .label("rank"),
            )
            .where(due, ~held_back, NotificationLog.id.not_in(list(held)))
            .subquery()
        )
        held = func.coalesce(in_flight.c.held, 0)
        turn = held + ranked.c.rank
        placed = (
            select(
                ranked.c.id,
                ranked.c.rank,
                held.label("held"),
                func.row_number()
                .over(order_by=(turn, ranked.c.decision_seq))
                .label("position"),
            )
            .select_from(
                ranked.outerjoin(
                    in_flight,
                    in_flight.c.provider_id == ranked.c.notification_provider_id,
                )
            )
            .where(turn <= provider_share)
            .subquery()
        )
        eligible = select(placed.c.id).where(
            or_(
                placed.c.position <= limit,
                and_(placed.c.held == 0, placed.c.rank == 1),
            ),
        )
        result = await db.execute(
            select(NotificationLog)
            .where(NotificationLog.id.in_(eligible))
            .order_by(NotificationLog.decision_seq)
            .with_for_update(skip_locked=True, of=NotificationLog)
            # The rows as locked now, over any copy the session holds.
            .execution_options(populate_existing=True)
        )
        return list(result.scalars().all())

    @staticmethod
    async def database_now(db: AsyncSession) -> datetime:
        """The database's clock: the one every server process's claims,
        leases and retries are judged by, whatever each host's clock says."""
        now: datetime = (await db.execute(select(func.clock_timestamp()))).scalar_one()
        return now

    @staticmethod
    async def release_attempts(
        db: AsyncSession, attempts: list[tuple[UUID, int]], now: datetime
    ) -> int:
        """Hand back rows whose attempts this process cut off (a graceful
        stop): each still `sending` for that attempt is due again at `now`,
        `retrying`, with no lease, so no process counts it in flight. How many
        were handed back."""
        released = 0
        for log_id, attempt in attempts:
            result = await db.execute(
                update(NotificationLog)
                .where(
                    NotificationLog.id == log_id,
                    NotificationLog.status == NotificationStatus.SENDING,
                    NotificationLog.attempts == attempt,
                )
                .values(
                    status=NotificationStatus.RETRYING,
                    next_attempt_at=now,
                    lease_expires_at=None,
                )
                .returning(NotificationLog.id)
            )
            released += result.scalar_one_or_none() is not None
        return released

    @staticmethod
    async def mark_sending(
        db: AsyncSession, log_id: UUID, *, lease_expires_at: datetime
    ) -> int:
        """Mark a claimed row `sending` for one more attempt, in one statement;
        the attempt's number."""
        result = await db.execute(
            update(NotificationLog)
            .where(NotificationLog.id == log_id)
            .values(
                status=NotificationStatus.SENDING,
                attempts=NotificationLog.attempts + 1,
                lease_expires_at=lease_expires_at,
            )
            .returning(NotificationLog.attempts)
        )
        attempt: int = result.scalar_one()
        return attempt

    @staticmethod
    async def finish_attempt(
        db: AsyncSession, log_id: UUID, attempt: int, values: dict[str, Any]
    ) -> bool:
        """Record an attempt's outcome on a row still claimed for that attempt.

        False when the row is gone (its alert or provider deleted) or another
        worker claimed it since (this worker's lease lapsed): that worker's
        outcome is the row's.
        """
        result = await db.execute(
            update(NotificationLog)
            .where(
                NotificationLog.id == log_id,
                NotificationLog.status == NotificationStatus.SENDING,
                NotificationLog.attempts == attempt,
            )
            .values(**values)
            .returning(NotificationLog.id)
        )
        return result.scalar_one_or_none() is not None

    @staticmethod
    async def count_delivered_for_provider_since(
        db: AsyncSession,
        provider_id: UUID,
        since: datetime,
        *,
        now: datetime,
        excluding: UUID,
    ) -> int:
        """A provider's deliveries that count against its rate limit: those
        sent or rate limited at `since` or later, by when their delivery
        ended (a row from before delivery was recorded, by its decision), and
        every one being sent now, under a lease still live at `now`.

        The row `excluding` (the one being judged) is never counted, and
        neither is a `sending` row whose lease has lapsed: its worker stopped
        mid-attempt, so it is due again, not in flight.
        """
        ended_at = func.coalesce(NotificationLog.delivered_at, NotificationLog.sent_at)
        result = await db.execute(
            select(func.count())
            .select_from(NotificationLog)
            .where(
                NotificationLog.notification_provider_id == provider_id,
                NotificationLog.id != excluding,
                or_(
                    and_(
                        NotificationLog.status == NotificationStatus.SENDING,
                        NotificationLog.lease_expires_at > now,
                    ),
                    and_(
                        NotificationLog.status.in_(
                            [NotificationStatus.SENT, NotificationStatus.RATE_LIMITED]
                        ),
                        ended_at >= since,
                    ),
                ),
            )
        )
        return result.scalar() or 0

    @staticmethod
    async def delete_older_than(db: AsyncSession, cutoff: datetime) -> int:
        """Delete notification log records older than cutoff; the number deleted."""
        result = await db.execute(
            delete(NotificationLog)
            .where(NotificationLog.sent_at < cutoff)
            .returning(NotificationLog.id)
        )
        return len(result.all())

    @staticmethod
    def _log_rows_query() -> LogRowSelect:
        """SELECT + joins for a notification-log table row.

        Uses the ORM (typed columns) so EncryptedString fields like
        ``Check.target`` are auto-decrypted on load. A raw ``text()`` SELECT of
        ``c.target`` returns the stored ciphertext instead — the reason this was
        rewritten off raw SQL.
        """
        return (
            select(
                NotificationLog.id,
                NotificationLog.sent_at,
                Alert.name.label("alert_name"),
                Alert.id.label("alert_id"),
                AlertCheckMapping.snoozed_until,
                NotificationProvider.friendly_name.label("provider_name"),
                Check.display_name,
                Check.check_type,
                Check.target,
                NotificationLog.status,
                NotificationLog.error_message,
                NotificationLog.check_success.label("success"),
                NotificationLog.check_latency_ms.label("latency_ms"),
                Check.id.label("check_id"),
                Agent.agent_name.label("agent_name"),
            )
            .select_from(NotificationLog)
            .join(Alert, NotificationLog.alert_id == Alert.id)
            .join(
                NotificationProvider,
                NotificationLog.notification_provider_id == NotificationProvider.id,
            )
            .outerjoin(Check, Check.id == NotificationLog.check_id)
            .outerjoin(Agent, Agent.id == NotificationLog.agent_id)
            .outerjoin(
                AlertCheckMapping,
                and_(
                    AlertCheckMapping.alert_id == Alert.id,
                    AlertCheckMapping.check_id == Check.id,
                ),
            )
        )

    @staticmethod
    def _count_query() -> Select[int]:
        """Row count over the same joins (search filters reference joined tables)."""
        return (
            select(func.count())
            .select_from(NotificationLog)
            .join(Alert, NotificationLog.alert_id == Alert.id)
            .join(
                NotificationProvider,
                NotificationLog.notification_provider_id == NotificationProvider.id,
            )
            .outerjoin(Check, Check.id == NotificationLog.check_id)
            .outerjoin(
                AlertCheckMapping,
                and_(
                    AlertCheckMapping.alert_id == Alert.id,
                    AlertCheckMapping.check_id == Check.id,
                ),
            )
        )

    @staticmethod
    async def get_logs_paginated(
        db: AsyncSession,
        *,
        status: str | None = None,
        alert_id: UUID | None = None,
        notification_provider_id: UUID | None = None,
        search: str | None = None,
        limit: int = 25,
        skip: int = 0,
    ) -> tuple[list[LogRow], int]:
        """
        Get paginated notification logs with filters and joins.

        check_success and check_latency_ms are denormalized on
        notification_logs, so no join against check_results is needed.

        Returns:
            Tuple of (list of result rows, total count)
        """
        conditions = []
        if status:
            conditions.append(NotificationLog.status == status)
        if alert_id:
            conditions.append(NotificationLog.alert_id == alert_id)
        if notification_provider_id:
            conditions.append(
                NotificationLog.notification_provider_id == notification_provider_id
            )
        if search:
            pattern = f"%{search}%"
            conditions.append(
                or_(
                    Alert.name.ilike(pattern),
                    NotificationProvider.friendly_name.ilike(pattern),
                    Check.display_name.ilike(pattern),
                )
            )

        count_stmt = NotificationLogCRUD._count_query()
        if conditions:
            count_stmt = count_stmt.where(*conditions)
        total = (await db.execute(count_stmt)).scalar() or 0

        stmt = NotificationLogCRUD._log_rows_query()
        if conditions:
            stmt = stmt.where(*conditions)
        stmt = stmt.order_by(NotificationLog.sent_at.desc()).limit(limit).offset(skip)
        result = await db.execute(stmt)

        return list(result.all()), total

    @staticmethod
    async def get_log_by_id(db: AsyncSession, log_id: UUID) -> LogRow | None:
        """
        Fetch a single notification log row by id, with the same shape as
        `get_logs_paginated` so the page partial can re-render it after a
        snooze toggle.

        Returns:
            Row with the columns expected by NotificationLogRow, or None.
        """
        stmt = NotificationLogCRUD._log_rows_query().where(NotificationLog.id == log_id)
        result = await db.execute(stmt)
        return result.first()

    @staticmethod
    async def get_all_alerts_for_dropdown(db: AsyncSession) -> list[Any]:
        """
        Get all alerts for filter dropdown.

        Returns:
            List of Row(id, name) objects
        """
        query = select(Alert.id, Alert.name).order_by(Alert.name)
        # unbounded-reads: operator-defined alert rules, listed whole in a filter dropdown
        result = await db.execute(query)
        return [tuple(row) for row in result.all()]

    @staticmethod
    async def get_all_providers_for_dropdown(db: AsyncSession) -> list[Any]:
        """
        Get all notification providers for filter dropdown.

        Returns:
            List of Row(id, friendly_name) objects
        """
        query = select(
            NotificationProvider.id,
            NotificationProvider.friendly_name,
        ).order_by(NotificationProvider.friendly_name)
        # unbounded-reads: operator-defined providers, listed whole in a filter dropdown
        result = await db.execute(query)
        return [tuple(row) for row in result.all()]

    @staticmethod
    async def get_status_counts(db: AsyncSession) -> list[Any]:
        """
        Get notification log counts grouped by status.

        Returns:
            List of Row(status, count) objects
        """
        query = select(
            NotificationLog.status,
            func.count(NotificationLog.id).label("count"),
        ).group_by(NotificationLog.status)
        # unbounded-reads: GROUP BY status, an enum of 8 members (NotificationStatus): at most 8 rows
        result = await db.execute(query)
        return [tuple(row) for row in result.all()]
