"""
TimescaleDB management service.

Manages compression policies, retention policies, and provides database health
metrics. All raw SQL lives in TimescaleCRUD; this service validates inputs and
shapes results.
"""

from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.timescale_crud import TimescaleCRUD

logger = get_logger("luxswirl.services.timescale")

# Security: Whitelist of allowed hypertable names to prevent SQL injection
# Only these hypertable names can be used in TimescaleDB policy functions
ALLOWED_HYPERTABLES = frozenset(
    [
        "check_results",
        "check_results_hourly",
        "check_results_daily",
        "agent_metrics",
    ]
)


def _validate_hypertable_name(hypertable_name: str) -> None:
    """Validate hypertable name against whitelist to prevent SQL injection."""
    if hypertable_name not in ALLOWED_HYPERTABLES:
        raise ValueError(
            f"Invalid hypertable name: {hypertable_name}. "
            f"Allowed values: {', '.join(sorted(ALLOWED_HYPERTABLES))}"
        )


def _validate_interval_days(days: int, param_name: str = "days") -> None:
    """Validate interval days parameter to prevent SQL injection via INTERVAL."""
    if not isinstance(days, int):
        raise ValueError(f"{param_name} must be an integer, got {type(days).__name__}")
    if days < 1:
        raise ValueError(f"{param_name} must be at least 1, got {days}")
    if days > 3650:  # 10 years max
        raise ValueError(f"{param_name} cannot exceed 3650 days (10 years), got {days}")


def _validate_hours(hours: int) -> None:
    if not isinstance(hours, int):
        raise ValueError(f"hours must be an integer, got {type(hours).__name__}")
    if hours < 1:
        raise ValueError(f"hours must be at least 1, got {hours}")
    if hours > 87600:  # 10 years
        raise ValueError(f"hours cannot exceed 87600 (10 years), got {hours}")


def _bucket_for_hours(hours: int) -> str:
    if hours <= 24:
        return "1 hour"
    if hours <= 720:
        return "1 day"
    return "7 days"


class TimescaleCoreService:
    """Service for managing TimescaleDB policies and health."""

    @staticmethod
    async def update_retention_policy(
        db: AsyncSession,
        hypertable_name: str,
        retention_days: int,
    ) -> None:
        """Update retention policy for a hypertable."""
        _validate_hypertable_name(hypertable_name)
        _validate_interval_days(retention_days, "retention_days")

        try:
            await TimescaleCRUD.remove_retention_policy(db, hypertable_name)
            await TimescaleCRUD.add_retention_policy(db, hypertable_name, retention_days)
            logger.info(
                "Updated retention policy",
                extra={
                    "hypertable_name": hypertable_name,
                    "retention_days": retention_days,
                },
            )
        except Exception:
            # get_db() owns the transaction and rolls back on the re-raised error.
            logger.error(
                "Failed to update retention policy",
                extra={"hypertable_name": hypertable_name},
                exc_info=True,
            )
            raise

    @staticmethod
    async def update_compression_policy(
        db: AsyncSession,
        hypertable_name: str,
        compress_after_days: int,
    ) -> None:
        """Update compression policy for a hypertable."""
        _validate_hypertable_name(hypertable_name)
        _validate_interval_days(compress_after_days, "compress_after_days")

        try:
            await TimescaleCRUD.remove_compression_policy(db, hypertable_name)
            await TimescaleCRUD.add_compression_policy(db, hypertable_name, compress_after_days)
            logger.info(
                "Updated compression policy",
                extra={
                    "hypertable_name": hypertable_name,
                    "compress_after_days": compress_after_days,
                },
            )
        except Exception:
            # get_db() owns the transaction and rolls back on the re-raised error.
            logger.error(
                "Failed to update compression policy",
                extra={"hypertable_name": hypertable_name},
                exc_info=True,
            )
            raise

    @staticmethod
    async def get_database_size(db: AsyncSession) -> dict:
        """Get database size information."""
        row = await TimescaleCRUD.get_database_size(db)
        if row is None:
            return {"total_size_bytes": 0, "total_size_pretty": "0 bytes"}
        return {"total_size_bytes": row[0], "total_size_pretty": row[1]}

    @staticmethod
    async def get_hypertable_stats(db: AsyncSession) -> list[dict]:
        """Get statistics for all hypertables."""
        rows = await TimescaleCRUD.get_hypertable_stats(db)
        return [
            {
                "name": row[0],
                "compression_enabled": row[1],
                "chunk_count": row[2],
                "total_size": row[3],
            }
            for row in rows
        ]

    @staticmethod
    async def get_compression_stats(db: AsyncSession) -> dict:
        """Get compression statistics for check_results hypertable."""
        row = await TimescaleCRUD.get_compression_stats_for_check_results(db)
        if row is None:
            return {
                "total_chunks": 0,
                "compressed_chunks": 0,
                "uncompressed_chunks": 0,
                "compression_ratio": 0.0,
            }
        total, compressed, uncompressed = row
        return {
            "total_chunks": total,
            "compressed_chunks": compressed,
            "uncompressed_chunks": uncompressed,
            "compression_ratio": round(compressed / total * 100, 1) if total > 0 else 0.0,
        }

    @staticmethod
    async def get_retention_info(db: AsyncSession) -> list[dict]:
        """Get current retention policies."""
        rows = await TimescaleCRUD.get_retention_policies(db)
        return [{"hypertable": name, "drop_after": drop_after} for name, drop_after in rows]

    @staticmethod
    async def get_table_row_counts(db: AsyncSession) -> dict:
        """Get row counts and sizes for major tables."""
        rows = await TimescaleCRUD.get_table_row_counts(db)
        counts: dict[str, dict] = {}
        for row in rows:
            counts[row[0]] = {
                "row_count": row[1],
                "oldest_record": row[2],
                "newest_record": row[3],
                "size_bytes": row[4],
                "size_pretty": row[5],
            }
        return counts

    @staticmethod
    async def get_daily_growth_rate(db: AsyncSession, days: int = 7) -> list[dict]:
        """Get daily data growth rate for the past N days."""
        _validate_interval_days(days, "days")
        rows = await TimescaleCRUD.get_daily_growth(db, days)
        return [{"date": date, "row_count": count} for date, count in rows]

    @staticmethod
    async def get_growth_chart_data(db: AsyncSession, hours: int = 168) -> list[dict]:
        """Get storage growth breakdown by table category over the given window."""
        _validate_hours(hours)
        bucket = _bucket_for_hours(hours)

        (
            avg_check_result_bytes,
            avg_check_bytes,
            avg_agent_metric_bytes,
        ) = await TimescaleCRUD.get_avg_row_sizes(db)
        avg_check_result = avg_check_result_bytes or 1200.0
        avg_check = avg_check_bytes or 2000.0
        avg_agent_metric = avg_agent_metric_bytes or 600.0

        rows = await TimescaleCRUD.get_growth_buckets(db, bucket, hours)

        growth: list[dict] = []
        for row in rows:
            results_mb = round((row[1] * avg_check_result) / (1024 * 1024), 2)
            artifacts_mb = round(row[2] / (1024 * 1024), 2)
            checks_mb = round((row[3] * avg_check) / (1024 * 1024), 2)
            agent_metrics_mb = round((row[4] * avg_agent_metric) / (1024 * 1024), 2)
            growth.append(
                {
                    "timestamp": row[0].isoformat() if row[0] else None,
                    "results_mb": results_mb,
                    "artifacts_mb": artifacts_mb,
                    "checks_mb": checks_mb,
                    "agent_metrics_mb": agent_metrics_mb,
                    "total_mb": round(results_mb + artifacts_mb + checks_mb + agent_metrics_mb, 2),
                }
            )
        return growth

    @staticmethod
    async def get_health_summary(db: AsyncSession) -> dict:
        """Get comprehensive database health summary."""
        return {
            "database_size": await TimescaleCoreService.get_database_size(db),
            "hypertables": await TimescaleCoreService.get_hypertable_stats(db),
            "compression": await TimescaleCoreService.get_compression_stats(db),
            "retention_policies": await TimescaleCoreService.get_retention_info(db),
            "row_counts": await TimescaleCoreService.get_table_row_counts(db),
            "daily_growth": await TimescaleCoreService.get_daily_growth_rate(db, days=7),
        }
