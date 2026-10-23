"""
NotificationLog CRUD - database queries for notification log operations.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Row, Select, and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_model import Agent
from app.models.alert_check_mapping_model import AlertCheckMapping
from app.models.alert_model import Alert
from app.models.check_model import Check
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
    async def count_for_provider_since(
        db: AsyncSession, provider_id: UUID, since: datetime, statuses: list[str]
    ) -> int:
        """Count notification logs for a provider sent since a timestamp with given statuses."""
        result = await db.execute(
            select(func.count())
            .select_from(NotificationLog)
            .where(
                and_(
                    NotificationLog.notification_provider_id == provider_id,
                    NotificationLog.sent_at >= since,
                    NotificationLog.status.in_(statuses),
                )
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
        # unbounded-reads: GROUP BY status, an enum of 6 members (NotificationStatus): at most 6 rows
        result = await db.execute(query)
        return [tuple(row) for row in result.all()]
