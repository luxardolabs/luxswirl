"""
CheckResult CRUD - database queries for check result operations.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Integer, Row, and_, cast, desc, func, or_, select, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql import ColumnElement

from app.core.datetime_utils import utc_now
from app.models.agent_model import Agent
from app.models.agent_model import Agent as AgentModel
from app.models.check_artifact_model import CheckArtifact
from app.models.check_model import Check
from app.models.check_result_model import CheckResult


class CheckResultCRUD:
    """Database queries for check results."""

    @staticmethod
    async def bulk_insert_idempotent(
        db: AsyncSession, rows: list[dict[str, Any]]
    ) -> list[CheckResult]:
        """Bulk-insert check results idempotently.

        ON CONFLICT DO NOTHING on the ``(check_id, timestamp)`` unique index, so
        an agent retry (same check, same instant) is a no-op at the DB level.
        Returns only the rows actually inserted — skipped duplicates are not
        returned, so the caller never re-processes them.

        Inserted in chunks so a single statement's bind-parameter count
        (rows × columns) stays under Postgres/asyncpg's hard cap of 32767 — a
        large agent batch (default report_batch_size=5000) otherwise overflows it.
        """
        if not rows:
            return []
        # Each row contributes (columns) bind params; a single statement must stay
        # under Postgres/asyncpg's 32767-param cap. A fixed 1500-row chunk keeps a
        # wide margin even for CheckResult's full column set (a 5000-row agent batch
        # → 4 chunks).
        chunk_size = 1500
        inserted: list[CheckResult] = []
        for start in range(0, len(rows), chunk_size):
            stmt = (
                pg_insert(CheckResult)
                .values(rows[start : start + chunk_size])
                .on_conflict_do_nothing(index_elements=["check_id", "timestamp"])
                .returning(CheckResult)
            )
            result = await db.execute(stmt)
            inserted.extend(result.scalars().all())
        return inserted

    @staticmethod
    async def get_latest_per_check_for_agent(
        db: AsyncSession, agent_id: UUID, cutoff
    ) -> Sequence[CheckResult]:
        """Latest result per check_id for an agent since cutoff."""
        latest_subquery = (
            select(
                CheckResult.check_id,
                func.max(CheckResult.timestamp).label("max_timestamp"),
            )
            .where(
                and_(
                    CheckResult.agent_id == agent_id,
                    CheckResult.timestamp >= cutoff,
                )
            )
            .group_by(CheckResult.check_id)
            .subquery()
        )

        result = await db.execute(
            select(CheckResult)
            .join(
                latest_subquery,
                and_(
                    CheckResult.check_id == latest_subquery.c.check_id,
                    CheckResult.timestamp == latest_subquery.c.max_timestamp,
                ),
            )
            .options(selectinload(CheckResult.check), selectinload(CheckResult.agent))
            .order_by(CheckResult.check_id)
        )
        return result.scalars().all()

    @staticmethod
    async def get_history_for_check(
        db: AsyncSession, check_id: UUID, cutoff, limit: int
    ) -> Sequence[CheckResult]:
        """Check history since cutoff, newest first, with check+agent loaded."""
        result = await db.execute(
            select(CheckResult)
            .where(
                and_(
                    CheckResult.check_id == check_id,
                    CheckResult.timestamp >= cutoff,
                )
            )
            .options(selectinload(CheckResult.check), selectinload(CheckResult.agent))
            .order_by(desc(CheckResult.timestamp))
            .limit(limit)
        )
        return result.scalars().all()

    @staticmethod
    async def get_summary_stats_for_check(db: AsyncSession, check_id: UUID, cutoff) -> Any:
        """Aggregate summary stats for a check since cutoff."""
        result = await db.execute(
            select(
                func.count(CheckResult.id).label("total_checks"),
                func.sum(func.cast(CheckResult.success, Integer)).label("successful_checks"),
                func.avg(CheckResult.latency_ms).label("avg_latency_ms"),
                func.min(CheckResult.latency_ms).label("min_latency_ms"),
                func.max(CheckResult.latency_ms).label("max_latency_ms"),
            ).where(
                and_(
                    CheckResult.check_id == check_id,
                    CheckResult.timestamp >= cutoff,
                )
            )
        )
        return result.one()

    @staticmethod
    async def get_latency_percentiles_for_check(
        db: AsyncSession, check_id: UUID, cutoff
    ) -> Any | None:
        """p50/p95/p99 latency for a check since cutoff."""
        result = await db.execute(
            select(
                func.percentile_cont(0.50).within_group(CheckResult.latency_ms).label("p50"),
                func.percentile_cont(0.95).within_group(CheckResult.latency_ms).label("p95"),
                func.percentile_cont(0.99).within_group(CheckResult.latency_ms).label("p99"),
            ).where(
                and_(
                    CheckResult.check_id == check_id,
                    CheckResult.timestamp >= cutoff,
                    CheckResult.latency_ms.isnot(None),
                )
            )
        )
        return result.one_or_none()

    @staticmethod
    async def get_overall_stats(db: AsyncSession, cutoff) -> Any:
        """Aggregate counts/avg latency across all check results since cutoff."""
        result = await db.execute(
            select(
                func.count(CheckResult.id).label("total_checks"),
                func.sum(func.cast(CheckResult.success, Integer)).label("successful_checks"),
                func.avg(CheckResult.latency_ms).label("avg_latency_ms"),
            ).where(CheckResult.timestamp >= cutoff)
        )
        return result.one()

    @staticmethod
    async def count_active_agents_since(db: AsyncSession, cutoff) -> int:
        """Distinct agents whose last_seen >= cutoff."""
        result = await db.execute(
            select(func.count(func.distinct(Agent.id))).where(Agent.last_seen >= cutoff)
        )
        return result.scalar_one()

    @staticmethod
    async def count_active_checks_since(db: AsyncSession, cutoff) -> int:
        """Checks whose owning agent has last_seen >= cutoff."""
        result = await db.execute(
            select(func.count(func.distinct(Check.id)))
            .join(Agent, Check.agent_id == Agent.id)
            .where(Agent.last_seen >= cutoff)
        )
        return result.scalar_one()

    @staticmethod
    async def delete_older_than(db: AsyncSession, cutoff) -> int:
        """Bulk delete check results with timestamp < cutoff. Returns rowcount."""
        result = await db.execute(
            text("DELETE FROM check_results WHERE timestamp < :cutoff_time"),
            {"cutoff_time": cutoff},
        )
        return result.rowcount or 0  # type: ignore[attr-defined]

    @staticmethod
    async def get_stats_for_agent(db: AsyncSession, agent_id: UUID, since) -> Any:
        """Aggregate check-result stats for an agent since a timestamp.
        Returns row with total_checks, successful_checks, avg_latency_ms, last_check_time."""
        result = await db.execute(
            select(
                func.count(CheckResult.id).label("total_checks"),
                func.sum(func.cast(CheckResult.success, Integer)).label("successful_checks"),
                func.avg(CheckResult.latency_ms).label("avg_latency_ms"),
                func.max(CheckResult.timestamp).label("last_check_time"),
            ).where(and_(CheckResult.agent_id == agent_id, CheckResult.timestamp >= since))
        )
        return result.one()

    @staticmethod
    async def count_since(db: AsyncSession, cutoff) -> int:
        """Count check results with timestamp >= cutoff."""
        result = await db.execute(
            select(func.count(CheckResult.id)).where(CheckResult.timestamp >= cutoff)
        )
        return result.scalar_one()

    @staticmethod
    async def get_success_stats_for_check(
        db: AsyncSession, check_id: UUID, since
    ) -> tuple[int, int]:
        """Return (total_count, successful_count) for a check since a timestamp."""
        result = await db.execute(
            select(
                func.count(CheckResult.id).label("total"),
                func.sum(func.cast(CheckResult.success, Integer)).label("successful"),
            ).where(
                and_(
                    CheckResult.check_id == check_id,
                    CheckResult.timestamp >= since,
                )
            )
        )
        row = result.one()
        return int(row.total or 0), int(row.successful or 0)

    @staticmethod
    async def get_latest_results_for_agent_with_check(
        db: AsyncSession, agent_id: UUID, cutoff
    ) -> list:
        """
        For a single agent, return list of (CheckResult, Check) pairs where
        CheckResult is the latest per check_id since cutoff.
        """
        latest_subquery = (
            select(
                CheckResult.check_id,
                func.max(CheckResult.timestamp).label("max_timestamp"),
            )
            .where(
                and_(
                    CheckResult.agent_id == agent_id,
                    CheckResult.timestamp >= cutoff,
                )
            )
            .group_by(CheckResult.check_id)
            .subquery()
        )

        results_query = (
            select(CheckResult, Check)
            .join(Check, CheckResult.check_id == Check.id)
            .join(
                latest_subquery,
                and_(
                    CheckResult.check_id == latest_subquery.c.check_id,
                    CheckResult.timestamp == latest_subquery.c.max_timestamp,
                ),
            )
            .where(CheckResult.agent_id == agent_id)
        )
        result = await db.execute(results_query)
        return list(result.all())

    @staticmethod
    async def get_checks_with_agents_filtered(
        db: AsyncSession,
        agent_id: UUID | None = None,
        check_type: str | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        search: str | None = None,
        check_ids: list[UUID] | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> tuple[Sequence[Row], int]:
        """
        Get filtered checks with their agents, paginated.

        Returns:
            Tuple of (list of (Check, Agent) tuples, total count)
        """
        query = (
            select(Check, Agent)
            .join(Agent, Check.agent_id == Agent.id)
            .options(selectinload(Check.agent))
        )

        conditions: list[ColumnElement[bool]] = []

        if check_ids:
            conditions.append(Check.id.in_(check_ids))

        if agent_id:
            conditions.append(Agent.id == agent_id)

        if check_type:
            conditions.append(Check.check_type == check_type)

        if search:
            search_pattern = f"%{search}%"
            conditions.append(
                or_(
                    Check.display_name.ilike(search_pattern),
                    Check.target.ilike(search_pattern),
                )
            )

        if tags:
            # Overlap (&&): a check matches if its own tags OR its agent's tags
            # share ANY of the requested tags (OR semantics).
            tag_array = cast(tags, ARRAY(Check.tags.type.item_type))
            conditions.append(
                or_(
                    Check.tags.op("&&")(tag_array),
                    Agent.tags.op("&&")(tag_array),
                )
            )

        # Status filter via latest results CTE
        if status:
            cutoff_status = utc_now() - timedelta(minutes=32)
            latest_result_subquery = (
                select(
                    CheckResult.check_id,
                    CheckResult.success,
                    func.row_number()
                    .over(
                        partition_by=CheckResult.check_id,
                        order_by=desc(CheckResult.timestamp),
                    )
                    .label("rn"),
                )
                .where(CheckResult.timestamp >= cutoff_status)
                .subquery()
            )

            latest_result_cte = (
                select(
                    latest_result_subquery.c.check_id,
                    latest_result_subquery.c.success,
                )
                .where(latest_result_subquery.c.rn == 1)
                .subquery()
            )

            query = query.outerjoin(latest_result_cte, Check.id == latest_result_cte.c.check_id)

            if status == "up":
                conditions.append(latest_result_cte.c.success.is_(True))
            elif status == "down":
                conditions.append(latest_result_cte.c.success.is_(False))
            elif status == "unknown":
                conditions.append(latest_result_cte.c.success.is_(None))

        if conditions:
            query = query.where(and_(*conditions))

        # Total count
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await db.execute(count_query)
        total = total_result.scalar_one()

        # Paginated results
        query = query.order_by(Agent.id, Check.display_name).limit(limit).offset(offset)
        result = await db.execute(query)
        checks_and_agents = result.all()

        return checks_and_agents, total

    @staticmethod
    async def get_latest_results_batch(
        db: AsyncSession, check_ids: list[UUID], cutoff_minutes: int = 32
    ) -> dict[UUID, Any]:
        """
        Batch query latest results for multiple checks.

        Returns:
            Map of check_id to latest result row
        """
        if not check_ids:
            return {}

        cutoff = utc_now() - timedelta(minutes=cutoff_minutes)
        subquery = (
            select(
                CheckResult.check_id,
                CheckResult.success,
                CheckResult.latency_ms,
                CheckResult.timestamp,
                func.row_number()
                .over(
                    partition_by=CheckResult.check_id,
                    order_by=desc(CheckResult.timestamp),
                )
                .label("rn"),
            )
            .where(
                and_(
                    CheckResult.check_id.in_(check_ids),
                    CheckResult.timestamp >= cutoff,
                )
            )
            .subquery()
        )

        query = select(subquery).where(subquery.c.rn == 1)
        result = await db.execute(query)
        return {row.check_id: row for row in result.all()}

    @staticmethod
    async def get_24h_stats_batch(db: AsyncSession, check_ids: list[UUID]) -> dict[UUID, Any]:
        """
        Batch query 24h success stats for multiple checks.

        Returns:
            Map of check_id to stats row (total, successful)
        """
        if not check_ids:
            return {}

        cutoff = utc_now() - timedelta(hours=24)
        query = (
            select(
                CheckResult.check_id,
                func.count(CheckResult.id).label("total"),
                func.sum(func.cast(CheckResult.success, Integer)).label("successful"),
            )
            .where(
                and_(
                    CheckResult.check_id.in_(check_ids),
                    CheckResult.timestamp >= cutoff,
                )
            )
            .group_by(CheckResult.check_id)
        )
        result = await db.execute(query)
        return {row.check_id: row for row in result.all()}

    @staticmethod
    async def get_minute_bars_results(
        db: AsyncSession, check_ids: list[UUID], minutes: int = 15
    ) -> list[CheckResult]:
        """
        Get all results from last N minutes for multiple checks.

        Returns:
            List of CheckResult objects
        """
        if not check_ids:
            return []

        cutoff = utc_now() - timedelta(minutes=minutes)
        query = (
            select(CheckResult)
            .where(
                and_(
                    CheckResult.check_id.in_(check_ids),
                    CheckResult.timestamp >= cutoff,
                )
            )
            .order_by(CheckResult.timestamp.desc())
        )
        result = await db.execute(query)
        return list(result.scalars().all())

    @staticmethod
    async def get_status_summary_data(db: AsyncSession) -> dict:
        """
        Get all data needed for status summary header.

        Returns:
            Dict with total_checks, enabled_checks, up_count, down_count,
            active_agents, overall_total, overall_successful
        """
        cutoff_recent = utc_now() - timedelta(minutes=5)
        cutoff_24h = utc_now() - timedelta(hours=24)

        # Total checks
        total_checks_result = await db.execute(select(func.count(Check.id)))
        total_checks = total_checks_result.scalar_one()

        # Enabled checks
        enabled_checks_result = await db.execute(
            select(func.count(Check.id)).where(Check.enabled.is_(True))
        )
        enabled_checks = enabled_checks_result.scalar_one()

        # Latest results subquery for up/down
        latest_results_subquery = (
            select(
                CheckResult.check_id,
                func.max(CheckResult.timestamp).label("max_timestamp"),
            )
            .where(CheckResult.timestamp >= cutoff_recent)
            .group_by(CheckResult.check_id)
            .subquery()
        )

        # The timestamp bound is required, not redundant: the planner can't push
        # `cr.timestamp == max_timestamp` down for chunk exclusion, so the scan
        # must carry its own bound to prune chunks.
        up_query = (
            select(func.count(CheckResult.check_id.distinct()))
            .join(
                latest_results_subquery,
                and_(
                    CheckResult.check_id == latest_results_subquery.c.check_id,
                    CheckResult.timestamp == latest_results_subquery.c.max_timestamp,
                ),
            )
            .where(
                and_(
                    CheckResult.success.is_(True),
                    CheckResult.timestamp >= cutoff_recent,
                )
            )
        )
        up_result = await db.execute(up_query)
        up_count = up_result.scalar_one()

        # Down count — same bound as up_query.
        down_query = (
            select(func.count(CheckResult.check_id.distinct()))
            .join(
                latest_results_subquery,
                and_(
                    CheckResult.check_id == latest_results_subquery.c.check_id,
                    CheckResult.timestamp == latest_results_subquery.c.max_timestamp,
                ),
            )
            .where(
                and_(
                    CheckResult.success.is_(False),
                    CheckResult.timestamp >= cutoff_recent,
                )
            )
        )
        down_result = await db.execute(down_query)
        down_count = down_result.scalar_one()

        # Active agents

        active_agents_result = await db.execute(
            select(func.count(AgentModel.id)).where(
                AgentModel.last_seen >= utc_now() - timedelta(minutes=10)
            )
        )
        active_agents = active_agents_result.scalar_one()

        # Overall 24h stats
        overall_stats_result = await db.execute(
            select(
                func.count(CheckResult.id).label("total"),
                func.sum(func.cast(CheckResult.success, Integer)).label("successful"),
            ).where(CheckResult.timestamp >= cutoff_24h)
        )
        overall_stats = overall_stats_result.one()

        return {
            "total_checks": total_checks,
            "enabled_checks": enabled_checks,
            "up_count": up_count,
            "down_count": down_count,
            "active_agents": active_agents,
            "overall_total": overall_stats.total,
            "overall_successful": overall_stats.successful,
        }

    # ------------------------------------------------------------------
    # Check detail queries (from check_detail_view_service)
    # ------------------------------------------------------------------

    @staticmethod
    async def get_check_with_agent(db: AsyncSession, check_id: UUID) -> Row | None:
        """
        Get check with its agent by check ID.

        Returns:
            Tuple of (Check, Agent) or None
        """
        query = (
            select(Check, Agent).join(Agent, Check.agent_id == Agent.id).where(Check.id == check_id)
        )
        result = await db.execute(query)
        return result.one_or_none()

    @staticmethod
    async def get_bucketed_history(db: AsyncSession, check_id: UUID, hours: int) -> list:
        """
        Get time-bucketed check result history using TimescaleDB time_bucket.

        Bucket sizes vary by time range:
        - <= 4h: 1-minute buckets
        - <= 8h: 2-minute buckets
        - <= 24h: 5-minute buckets
        - > 24h: 30-minute or 1-hour buckets

        Returns:
            List of result rows with bucket_time, success, latency_ms, error, metrics
        """
        cutoff = utc_now() - timedelta(hours=hours)

        if hours <= 4:
            interval = "1 minute"
        elif hours <= 8:
            interval = "2 minutes"
        elif hours <= 24:
            interval = "5 minutes"
        elif hours <= 72:
            interval = "30 minutes"
        else:
            interval = "1 hour"

        history_query = text(
            f"""
            SELECT
                time_bucket('{interval}', timestamp) as bucket_time,
                check_id,
                BOOL_AND(success) as success,
                AVG(latency_ms) as latency_ms,
                STRING_AGG(DISTINCT error, '; ') as error,
                (ARRAY_AGG(metrics ORDER BY timestamp DESC))[1] as metrics
            FROM check_results
            WHERE check_id = :check_id
                AND timestamp >= :cutoff
            GROUP BY bucket_time, check_id
            ORDER BY bucket_time ASC
            LIMIT 500
            """
        )
        result = await db.execute(history_query, {"check_id": check_id, "cutoff": cutoff})
        return list(result.all())

    @staticmethod
    async def get_raw_results_for_status_bar(
        db: AsyncSession, check_id: UUID, minutes: int = 30
    ) -> list[CheckResult]:
        """
        Get raw check results for status bar display.

        Returns:
            List of CheckResult objects ordered by timestamp desc
        """
        cutoff = utc_now() - timedelta(minutes=minutes)
        query = (
            select(CheckResult)
            .where(
                and_(
                    CheckResult.check_id == check_id,
                    CheckResult.timestamp >= cutoff,
                )
            )
            .order_by(desc(CheckResult.timestamp))
        )
        result = await db.execute(query)
        return list(result.scalars().all())

    @staticmethod
    async def get_check_detail_stats(db: AsyncSession, check_id: UUID, hours: int) -> Any:
        """
        Get stats for check detail panel (count, success, latencies).

        Returns:
            Row with total, successful, avg_latency, min_latency, max_latency
        """
        cutoff = utc_now() - timedelta(hours=hours)
        query = select(
            func.count(CheckResult.id).label("total"),
            func.sum(func.cast(CheckResult.success, Integer)).label("successful"),
            func.avg(CheckResult.latency_ms).label("avg_latency"),
            func.min(CheckResult.latency_ms).label("min_latency"),
            func.max(CheckResult.latency_ms).label("max_latency"),
        ).where(
            and_(
                CheckResult.check_id == check_id,
                CheckResult.timestamp >= cutoff,
            )
        )
        result = await db.execute(query)
        return result.one()

    @staticmethod
    async def get_latest_result_for_check(db: AsyncSession, check_id: UUID) -> CheckResult | None:
        """
        Get the most recent result for a check.

        Returns:
            CheckResult or None
        """
        query = (
            select(CheckResult)
            .where(CheckResult.check_id == check_id)
            .order_by(desc(CheckResult.timestamp))
            .limit(1)
        )
        result = await db.execute(query)
        return result.scalar_one_or_none()

    @staticmethod
    async def get_check_result_by_id(db: AsyncSession, check_result_id: UUID) -> CheckResult | None:
        """Get a single check result by id (unique across the hypertable)."""
        query = select(CheckResult).where(CheckResult.id == check_result_id).limit(1)
        result = await db.execute(query)
        return result.scalar_one_or_none()

    @staticmethod
    async def get_artifacts_for_result(
        db: AsyncSession, check_result_id: UUID, check_result_timestamp: datetime
    ) -> list[CheckArtifact]:
        """
        Get artifacts for a check result.

        check_artifacts is a hypertable partitioned by created_at, so the scan is
        bounded to a ±1-day window around the result's timestamp for chunk
        exclusion. Artifacts are written within seconds of the check result, so
        the window is result-identical, not a behaviour change.

        Returns:
            List of CheckArtifact objects
        """
        window = timedelta(days=1)
        query = (
            select(CheckArtifact)
            .where(
                and_(
                    CheckArtifact.check_result_id == check_result_id,
                    CheckArtifact.created_at >= check_result_timestamp - window,
                    CheckArtifact.created_at <= check_result_timestamp + window,
                )
            )
            .order_by(CheckArtifact.created_at)
        )
        result = await db.execute(query)
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Dashboard queries (from dashboard_view_service)
    # ------------------------------------------------------------------

    @staticmethod
    async def get_uptime_stats_bulk(
        db: AsyncSession, check_ids: list[UUID], hours: int = 168
    ) -> list:
        """
        Get uptime stats for multiple checks in a single query.

        Returns:
            List of rows with check_id, total, successful
        """
        if not check_ids:
            return []

        cutoff = utc_now() - timedelta(hours=hours)
        query = (
            select(
                CheckResult.check_id,
                func.count(CheckResult.id).label("total"),
                func.sum(func.cast(CheckResult.success, Integer)).label("successful"),
            )
            .where(
                and_(
                    CheckResult.check_id.in_(check_ids),
                    CheckResult.timestamp >= cutoff,
                )
            )
            .group_by(CheckResult.check_id)
        )
        result = await db.execute(query)
        return list(result.all())

    @staticmethod
    async def get_last_failure_timestamps_bulk(
        db: AsyncSession, check_ids: list[UUID], hours: int = 168
    ) -> list:
        """
        Get last failure timestamp for multiple checks.

        Returns:
            List of rows with check_id, last_failure_at
        """
        if not check_ids:
            return []

        cutoff = utc_now() - timedelta(hours=hours)
        query = (
            select(
                CheckResult.check_id,
                func.max(CheckResult.timestamp).label("last_failure_at"),
            )
            .where(
                and_(
                    CheckResult.check_id.in_(check_ids),
                    CheckResult.timestamp >= cutoff,
                    CheckResult.success.is_(False),
                )
            )
            .group_by(CheckResult.check_id)
        )
        result = await db.execute(query)
        return list(result.all())

    @staticmethod
    async def get_results_for_status_bars_bulk(
        db: AsyncSession, check_ids: list[UUID], minutes: int = 32
    ) -> list[CheckResult]:
        """
        Get results for status bars for multiple checks.

        Returns:
            List of CheckResult objects ordered by check_id, timestamp desc
        """
        if not check_ids:
            return []

        cutoff = utc_now() - timedelta(minutes=minutes)
        query = (
            select(CheckResult)
            .where(
                and_(
                    CheckResult.check_id.in_(check_ids),
                    CheckResult.timestamp >= cutoff,
                )
            )
            .order_by(CheckResult.check_id, desc(CheckResult.timestamp))
        )
        result = await db.execute(query)
        return list(result.scalars().all())

    @staticmethod
    async def get_bucketed_status_bars_bulk(
        db: AsyncSession,
        check_ids: list[UUID],
        minutes: int = 32,
        bucket_minutes: int = 1,
    ) -> list[Any]:
        """
        Get aggregated status bar data bucketed by time interval.
        Aggregates in SQL for performance on larger time ranges.

        Returns:
            List of rows with (check_id, bucket_start, success_count, fail_count,
            avg_latency, any_http_status) ordered by check_id, bucket_start desc
        """
        if not check_ids:
            return []

        cutoff = utc_now() - timedelta(minutes=minutes)
        bucket_interval = timedelta(minutes=bucket_minutes)

        query = text("""
            SELECT
                check_id,
                time_bucket(:interval, timestamp) AS bucket_start,
                COUNT(*) FILTER (WHERE success = true) AS success_count,
                COUNT(*) FILTER (WHERE success = false) AS fail_count,
                ROUND(AVG(latency_ms)::numeric, 1) AS avg_latency,
                (array_agg(http_status_code ORDER BY timestamp DESC))[1] AS http_status_code
            FROM check_results
            WHERE check_id = ANY(:check_ids)
              AND timestamp >= :cutoff
            GROUP BY check_id, bucket_start
            ORDER BY check_id, bucket_start DESC
        """)

        result = await db.execute(
            query,
            {
                "interval": bucket_interval,
                "check_ids": [str(cid) for cid in check_ids],
                "cutoff": cutoff,
            },
        )
        return list(result.all())
