"""
Timescale CRUD - raw SQL/DDL queries for TimescaleDB management.

Every caller value is a BOUND parameter: a hypertable name is cast to
`regclass` (so it must name an existing relation), day counts are bound as
intervals, and hour counts and bucket widths become intervals in SQL. Nothing a caller passes is spliced into
the statement text.
"""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import cast, func, literal, select, text
from sqlalchemy.dialects.postgresql import REGCLASS
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import Cast

from app.models.check_result_model import CheckResult


def _relation(name: str) -> Cast[Any]:
    """`name` as a bound value cast to regclass: it must name an existing relation."""
    return cast(literal(name), REGCLASS)


class TimescaleCRUD:
    """Database queries for TimescaleDB administration and metrics."""

    @staticmethod
    async def remove_retention_policy(db: AsyncSession, hypertable_name: str) -> None:
        await db.execute(
            select(func.remove_retention_policy(_relation(hypertable_name), True))
        )

    @staticmethod
    async def add_retention_policy(
        db: AsyncSession, hypertable_name: str, retention_days: int
    ) -> None:
        await db.execute(
            select(
                func.add_retention_policy(
                    _relation(hypertable_name), timedelta(days=retention_days), True
                )
            )
        )

    @staticmethod
    async def remove_compression_policy(db: AsyncSession, hypertable_name: str) -> None:
        await db.execute(
            select(func.remove_compression_policy(_relation(hypertable_name), True))
        )

    @staticmethod
    async def add_compression_policy(
        db: AsyncSession, hypertable_name: str, compress_after_days: int
    ) -> None:
        await db.execute(
            select(
                func.add_compression_policy(
                    _relation(hypertable_name),
                    timedelta(days=compress_after_days),
                    True,
                )
            )
        )

    @staticmethod
    async def get_database_size(db: AsyncSession) -> tuple[int, str] | None:
        """Returns (total_bytes, total_pretty) or None."""
        result = await db.execute(
            text(
                """
                SELECT
                    pg_database_size(current_database()) as total_bytes,
                    pg_size_pretty(pg_database_size(current_database())) as total_pretty
                """
            )
        )
        row = result.fetchone()
        if row is None:
            return None
        return (row[0], row[1])

    @staticmethod
    async def get_hypertable_stats(
        db: AsyncSession,
    ) -> list[tuple[str, bool, int, str | None]]:
        """Returns rows of (name, compression_enabled, num_chunks, total_size_pretty)."""
        result = await db.execute(
            text(
                """
                SELECT
                    h.hypertable_name,
                    h.compression_enabled,
                    h.num_chunks,
                    pg_size_pretty(
                        (SELECT SUM(pg_total_relation_size(format('%I.%I', chunk_schema, chunk_name)))
                         FROM timescaledb_information.chunks
                         WHERE hypertable_name = h.hypertable_name)
                    ) as total_size
                FROM timescaledb_information.hypertables h
                WHERE hypertable_schema = 'public'
                ORDER BY hypertable_name
                """
            )
        )
        return [(name, enabled, chunks, size) for name, enabled, chunks, size in result]

    @staticmethod
    async def get_compression_stats_for_check_results(
        db: AsyncSession,
    ) -> tuple[int, int, int] | None:
        """Returns (total_chunks, compressed_chunks, uncompressed_chunks) or None."""
        result = await db.execute(
            text(
                """
                SELECT
                    COUNT(*) as total_chunks,
                    COUNT(*) FILTER (WHERE is_compressed) as compressed_chunks,
                    COUNT(*) FILTER (WHERE NOT is_compressed) as uncompressed_chunks
                FROM timescaledb_information.chunks
                WHERE hypertable_name = 'check_results'
                """
            )
        )
        row = result.fetchone()
        if row is None:
            return None
        return (row[0], row[1], row[2])

    @staticmethod
    async def get_retention_policies(db: AsyncSession) -> list[tuple[str, str]]:
        """Returns rows of (hypertable_name, drop_after)."""
        result = await db.execute(
            text(
                """
                SELECT
                    hypertable_name,
                    config->>'drop_after' as drop_after
                FROM timescaledb_information.jobs
                WHERE proc_name = 'policy_retention'
                ORDER BY hypertable_name
                """
            )
        )
        return [(row[0], row[1]) for row in result.fetchall()]

    @staticmethod
    async def get_table_row_counts(
        db: AsyncSession,
    ) -> list[tuple[str, int, datetime | None, datetime | None, int, str]]:
        """Returns rows of (table_name, row_count, oldest, newest, size_bytes, size_pretty)."""
        result = await db.execute(
            text(
                """
                SELECT
                    'check_results' as table_name,
                    approximate_row_count('check_results') as row_count,
                    (SELECT MIN(timestamp) FROM check_results) as oldest_record,
                    (SELECT MAX(timestamp) FROM check_results) as newest_record,
                    pg_total_relation_size('check_results') as size_bytes,
                    pg_size_pretty(pg_total_relation_size('check_results')) as size_pretty
                UNION ALL
                SELECT
                    'checks' as table_name,
                    approximate_row_count('checks') as row_count,
                    NULL as oldest_record,
                    NULL as newest_record,
                    pg_total_relation_size('checks') as size_bytes,
                    pg_size_pretty(pg_total_relation_size('checks')) as size_pretty
                UNION ALL
                SELECT
                    'agents' as table_name,
                    approximate_row_count('agents') as row_count,
                    NULL as oldest_record,
                    NULL as newest_record,
                    pg_total_relation_size('agents') as size_bytes,
                    pg_size_pretty(pg_total_relation_size('agents')) as size_pretty
                UNION ALL
                SELECT
                    'check_artifacts' as table_name,
                    approximate_row_count('check_artifacts') as row_count,
                    (SELECT MIN(created_at) FROM check_artifacts) as oldest_record,
                    (SELECT MAX(created_at) FROM check_artifacts) as newest_record,
                    pg_total_relation_size('check_artifacts') as size_bytes,
                    pg_size_pretty(pg_total_relation_size('check_artifacts')) as size_pretty
                """
            )
        )
        return [
            (table, rows, oldest, newest, size, pretty)
            for table, rows, oldest, newest, size, pretty in result
        ]

    @staticmethod
    async def get_daily_growth(db: AsyncSession, days: int) -> list[tuple[str, int]]:
        """Returns (date_str, row_count) per day for the last N days, newest first."""
        day = func.date(CheckResult.timestamp)
        result = await db.execute(
            select(day, func.count())
            .where(CheckResult.timestamp >= func.now() - timedelta(days=days))
            .group_by(day)
            .order_by(day.desc())
        )
        return [(str(on), count) for on, count in result]

    @staticmethod
    async def get_avg_row_sizes(
        db: AsyncSession,
    ) -> tuple[float | None, float | None, float | None]:
        """Returns (check_result_bytes, check_bytes, agent_metric_bytes) averages."""
        result = await db.execute(
            text(
                """
                SELECT
                    hypertable_size('check_results')::numeric / NULLIF(approximate_row_count('check_results'), 0) as check_result_bytes,
                    pg_total_relation_size('checks')::numeric / NULLIF((SELECT COUNT(*) FROM checks), 0) as check_bytes,
                    hypertable_size('agent_metrics')::numeric / NULLIF(approximate_row_count('agent_metrics'), 0) as agent_metric_bytes
                """
            )
        )
        row = result.fetchone()
        if row is None:
            return (None, None, None)
        return (
            float(row[0]) if row[0] is not None else None,
            float(row[1]) if row[1] is not None else None,
            float(row[2]) if row[2] is not None else None,
        )

    @staticmethod
    async def get_growth_buckets(
        db: AsyncSession, bucket: str, hours: int
    ) -> list[tuple[datetime, int, int, int, int]]:
        """Returns time-bucketed (timestamp, result_count, artifact_bytes, check_count, agent_metric_count)."""
        result = await db.execute(
            text(
                """
                WITH results_bucketed AS (
                    SELECT time_bucket(CAST(CAST(:bucket AS text) AS interval), timestamp) as bucket, COUNT(*) as count
                    FROM check_results WHERE timestamp >= NOW() - make_interval(hours => :hours)
                    GROUP BY bucket
                ),
                artifacts_bucketed AS (
                    SELECT time_bucket(CAST(CAST(:bucket AS text) AS interval), created_at) as bucket, COALESCE(SUM(size_bytes), 0) as bytes
                    FROM check_artifacts WHERE created_at >= NOW() - make_interval(hours => :hours)
                    GROUP BY bucket
                ),
                checks_bucketed AS (
                    SELECT time_bucket(CAST(CAST(:bucket AS text) AS interval), created_at) as bucket, COUNT(*) as count
                    FROM checks WHERE created_at >= NOW() - make_interval(hours => :hours)
                    GROUP BY bucket
                ),
                agent_metrics_bucketed AS (
                    SELECT time_bucket(CAST(CAST(:bucket AS text) AS interval), timestamp) as bucket, COUNT(*) as count
                    FROM agent_metrics WHERE timestamp >= NOW() - make_interval(hours => :hours)
                    GROUP BY bucket
                )
                SELECT
                    COALESCE(r.bucket, a.bucket, c.bucket, am.bucket) as time_bucket,
                    COALESCE(r.count, 0) as result_count,
                    COALESCE(a.bytes, 0) as artifact_bytes,
                    COALESCE(c.count, 0) as check_count,
                    COALESCE(am.count, 0) as agent_metric_count
                FROM results_bucketed r
                FULL OUTER JOIN artifacts_bucketed a ON r.bucket = a.bucket
                FULL OUTER JOIN checks_bucketed c ON COALESCE(r.bucket, a.bucket) = c.bucket
                FULL OUTER JOIN agent_metrics_bucketed am ON COALESCE(r.bucket, a.bucket, c.bucket) = am.bucket
                ORDER BY time_bucket ASC
                """
            ),
            {"bucket": bucket, "hours": hours},
        )
        return [
            (bucket_at, results, artifact_bytes, checks, agent_metrics)
            for bucket_at, results, artifact_bytes, checks, agent_metrics in result
        ]
