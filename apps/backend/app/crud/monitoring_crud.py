"""
Monitoring CRUD - PostgreSQL/TimescaleDB health-introspection queries.

These read system catalogs (pg_*, timescaledb_information.*) for the monitoring
scheduler. Kept in crud/ so monitoring_core_service stays raw-SQL-free; the
service owns the error handling + metric-dict assembly, this layer owns the SQL.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class MonitoringCRUD:
    """Read-only database/TimescaleDB introspection for health metrics."""

    @staticmethod
    async def get_database_size_bytes(db: AsyncSession) -> int | None:
        """Total on-disk size of the current database, in bytes."""
        result = await db.execute(text("SELECT pg_database_size(current_database())"))
        return result.scalar()

    @staticmethod
    async def get_active_connection_count(db: AsyncSession) -> int | None:
        """Open backend connections to the current database."""
        result = await db.execute(
            text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()")
        )
        return result.scalar()

    @staticmethod
    async def get_top_table_sizes(db: AsyncSession, limit: int = 10) -> list[tuple]:
        """Largest user tables as (table_name, total_size_bytes), biggest first."""
        result = await db.execute(
            text(
                """
                SELECT relname AS table_name,
                       pg_total_relation_size(relid) AS total_size
                FROM pg_catalog.pg_statio_user_tables
                ORDER BY pg_total_relation_size(relid) DESC
                LIMIT :limit
                """
            ),
            {"limit": limit},
        )
        return [tuple(row) for row in result.all()]

    @staticmethod
    async def get_hypertable_chunk_stats(db: AsyncSession) -> list[tuple]:
        """Per-hypertable (name, total_chunks, compressed_chunks, uncompressed_span)."""
        result = await db.execute(
            text(
                """
                SELECT
                    hypertable_name,
                    count(*) AS total_chunks,
                    count(*) FILTER (WHERE is_compressed) AS compressed_chunks,
                    sum(CASE WHEN NOT is_compressed
                        THEN range_end::timestamptz - range_start::timestamptz
                        END) AS uncompressed_span
                FROM timescaledb_information.chunks
                GROUP BY hypertable_name
                """
            )
        )
        return [tuple(row) for row in result.all()]

    @staticmethod
    async def get_check_results_approx_count(db: AsyncSession) -> int | None:
        """Approximate row count for the check_results hypertable.

        Uses approximate_row_count() because pg_stat_user_tables reports 0 for
        hypertables (rows live in chunks).
        """
        result = await db.execute(text("SELECT approximate_row_count('check_results')"))
        return result.scalar()
