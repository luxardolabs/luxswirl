"""
Database maintenance background task.

Periodically performs database maintenance operations:
- VACUUM to reclaim space from deleted rows
- ANALYZE to update query planner statistics
- Bloat detection and cleanup
- Compression policy enforcement
- Data retention policy enforcement
"""

import asyncio
from datetime import datetime, timedelta
from typing import Any

from shared.logger import get_logger
from sqlalchemy import text

from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.db import get_session_maker
from app.db.database import get_engine

logger = get_logger("luxswirl.background.db_maintenance")

# Task handle
_maintenance_task: asyncio.Task | None = None

# Security: Whitelist of allowed table names to prevent SQL injection
# Only these tables can be used in VACUUM and maintenance operations
ALLOWED_TABLES = frozenset(
    [
        "check_results",
        "check_artifacts",
        "checks",
        "agents",
        "jobs",
        "notification_logs",
        "agent_metrics",
        "sessions",
        "users",
    ]
)

# Pre-built SQL for cases where PostgreSQL won't accept bind parameters:
# VACUUM is DDL (no params), and FROM <table> uses an identifier (also no params).
# Every query is a literal string — no f-string interpolation at any time, ever.
# Adding a new table requires adding both an entry to ALLOWED_TABLES and to this map
# (the assertion below enforces parity).
_TABLE_SQL: dict[str, dict[str, Any]] = {
    "check_results": {
        "vacuum": text("VACUUM ANALYZE check_results"),
        "vacuum_full": text("VACUUM FULL ANALYZE check_results"),
        "count": text("SELECT COUNT(*) FROM check_results"),
    },
    "check_artifacts": {
        "vacuum": text("VACUUM ANALYZE check_artifacts"),
        "vacuum_full": text("VACUUM FULL ANALYZE check_artifacts"),
        "count": text("SELECT COUNT(*) FROM check_artifacts"),
    },
    "checks": {
        "vacuum": text("VACUUM ANALYZE checks"),
        "vacuum_full": text("VACUUM FULL ANALYZE checks"),
        "count": text("SELECT COUNT(*) FROM checks"),
    },
    "agents": {
        "vacuum": text("VACUUM ANALYZE agents"),
        "vacuum_full": text("VACUUM FULL ANALYZE agents"),
        "count": text("SELECT COUNT(*) FROM agents"),
    },
    "jobs": {
        "vacuum": text("VACUUM ANALYZE jobs"),
        "vacuum_full": text("VACUUM FULL ANALYZE jobs"),
        "count": text("SELECT COUNT(*) FROM jobs"),
    },
    "notification_logs": {
        "vacuum": text("VACUUM ANALYZE notification_logs"),
        "vacuum_full": text("VACUUM FULL ANALYZE notification_logs"),
        "count": text("SELECT COUNT(*) FROM notification_logs"),
    },
    "agent_metrics": {
        "vacuum": text("VACUUM ANALYZE agent_metrics"),
        "vacuum_full": text("VACUUM FULL ANALYZE agent_metrics"),
        "count": text("SELECT COUNT(*) FROM agent_metrics"),
    },
    "sessions": {
        "vacuum": text("VACUUM ANALYZE sessions"),
        "vacuum_full": text("VACUUM FULL ANALYZE sessions"),
        "count": text("SELECT COUNT(*) FROM sessions"),
    },
    "users": {
        "vacuum": text("VACUUM ANALYZE users"),
        "vacuum_full": text("VACUUM FULL ANALYZE users"),
        "count": text("SELECT COUNT(*) FROM users"),
    },
}

# Module-load consistency check: the two registries must agree.
assert set(_TABLE_SQL.keys()) == set(ALLOWED_TABLES), (
    "ALLOWED_TABLES and _TABLE_SQL drifted — add SQL templates for new tables"
)


def _validate_table_name(table_name: str) -> None:
    """
    Validate table name against whitelist to prevent SQL injection.

    Args:
        table_name: Name of table to validate

    Raises:
        ValueError: If table name is not in whitelist
    """
    if table_name not in ALLOWED_TABLES:
        raise ValueError(
            f"Invalid table name: {table_name}. Allowed values: {', '.join(sorted(ALLOWED_TABLES))}"
        )


async def _vacuum_table(db, table_name: str, full: bool = False) -> dict:
    """
    Vacuum a specific table.

    Args:
        db: Database session
        table_name: Name of table to vacuum
        full: Whether to run VACUUM FULL (reclaims space to OS)

    Returns:
        Dict with vacuum stats

    Raises:
        ValueError: If table_name is not in whitelist
    """
    # Security: Validate table name to prevent SQL injection
    _validate_table_name(table_name)

    start_time = datetime.utcnow()
    vacuum_type = "VACUUM FULL" if full else "VACUUM"

    # VACUUM cannot run inside a transaction block (PostgreSQL DDL restriction).
    # Use a dedicated AUTOCOMMIT connection from the engine — bypassing the session's
    # implicit transaction. All three queries (size-before, vacuum, size-after) share
    # the same autocommit connection so each sees fresh stats post-VACUUM.
    if table_name == "check_results":
        size_sql = text("SELECT hypertable_size('check_results')")
    else:
        size_sql = text("SELECT pg_total_relation_size(:t)")
    size_params: dict[str, Any] = {} if table_name == "check_results" else {"t": table_name}
    vacuum_sql = _TABLE_SQL[table_name]["vacuum_full" if full else "vacuum"]

    try:
        engine = get_engine()
        async with engine.connect() as conn:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")

            size_before = (await conn.execute(size_sql, size_params)).scalar()
            await conn.execute(vacuum_sql)
            size_after = (await conn.execute(size_sql, size_params)).scalar()

        duration = (datetime.utcnow() - start_time).total_seconds()
        if size_before is None or size_after is None:
            raise RuntimeError(
                f"VACUUM size query returned NULL for table {table_name} "
                f"(before={size_before}, after={size_after})"
            )
        space_reclaimed = int(size_before) - int(size_after)
        space_reclaimed_mb = space_reclaimed / (1024 * 1024)

        return {
            "table": table_name,
            "vacuum_type": vacuum_type,
            "duration_seconds": round(duration, 2),
            "size_before_bytes": size_before,
            "size_after_bytes": size_after,
            "space_reclaimed_mb": round(space_reclaimed_mb, 2),
            "success": True,
        }

    except Exception as e:
        logger.error(
            "Error vacuuming table",
            extra={"table_name": table_name},
            exc_info=True,
        )
        return {
            "table": table_name,
            "vacuum_type": vacuum_type,
            "success": False,
            "error": str(e),
        }


async def _get_table_bloat(db, table_name: str) -> dict:
    """
    Estimate bloat percentage for a table.

    Args:
        db: Database session
        table_name: Name of table to analyze

    Returns:
        Dict with bloat stats

    Raises:
        ValueError: If table_name is not in whitelist
    """
    # Security: Validate table name to prevent SQL injection
    _validate_table_name(table_name)

    try:
        # Get actual size and row count
        if table_name == "check_results":
            actual_size_result = await db.execute(text("SELECT hypertable_size('check_results')"))
        else:
            actual_size_result = await db.execute(
                text("SELECT pg_total_relation_size(:t)"), {"t": table_name}
            )
        actual_size = actual_size_result.scalar()

        # Identifier reference — must use precomputed literal SQL
        row_count_result = await db.execute(_TABLE_SQL[table_name]["count"])
        row_count = row_count_result.scalar()

        # Get dead tuples (relname value — bind param)
        dead_result = await db.execute(
            text(
                """
                SELECT n_dead_tup, n_live_tup
                FROM pg_stat_user_tables
                WHERE relname = :t
                """
            ),
            {"t": table_name},
        )
        dead_stats = dead_result.fetchone()
        n_dead = dead_stats[0] if dead_stats else 0
        n_live = dead_stats[1] if dead_stats else 0

        # Calculate bloat percentage
        total_tuples = n_live + n_dead
        bloat_pct = (n_dead / total_tuples * 100) if total_tuples > 0 else 0

        return {
            "table": table_name,
            "actual_size_bytes": actual_size,
            "actual_size_mb": round(actual_size / (1024 * 1024), 2),
            "row_count": row_count,
            "live_tuples": n_live,
            "dead_tuples": n_dead,
            "bloat_percentage": round(bloat_pct, 2),
        }

    except Exception as e:
        logger.error(
            "Error calculating bloat",
            extra={"table_name": table_name},
            exc_info=True,
        )
        return {
            "table": table_name,
            "error": str(e),
        }


async def _run_maintenance_cycle(db):
    """
    Run a complete maintenance cycle.

    1. Check bloat on major tables
    2. VACUUM tables with >10% bloat
    3. VACUUM FULL tables with >50% bloat
    4. ANALYZE all tables
    5. Log results
    """
    logger.info("Starting database maintenance cycle")
    start_time = datetime.utcnow()

    # Tables to maintain
    tables = [
        "check_results",
        "check_artifacts",
        "checks",
        "agents",
        "jobs",
        "notification_logs",
    ]

    maintenance_report = {
        "cycle_start": start_time.isoformat(),
        "bloat_stats": [],
        "vacuum_operations": [],
    }

    # Step 1: Check bloat on all tables
    logger.info("Checking table bloat...")
    for table in tables:
        bloat = await _get_table_bloat(db, table)
        maintenance_report["bloat_stats"].append(bloat)

        if "error" not in bloat:
            logger.info(
                "Table bloat report",
                extra={
                    "table_name": table,
                    "actual_size_mb": bloat["actual_size_mb"],
                    "row_count": bloat["row_count"],
                    "bloat_percentage": bloat["bloat_percentage"],
                    "dead_tuples": bloat["dead_tuples"],
                },
            )

    # Step 2: Vacuum tables based on bloat
    for bloat in maintenance_report["bloat_stats"]:
        if "error" in bloat:
            continue

        table = bloat["table"]
        bloat_pct = bloat["bloat_percentage"]
        dead_tuples = bloat["dead_tuples"]

        # VACUUM FULL if >50% bloat OR >10,000 dead tuples
        if bloat_pct > 50 or dead_tuples > 10000:
            logger.info(
                "Running VACUUM FULL",
                extra={
                    "table_name": table,
                    "bloat_pct": bloat_pct,
                    "dead_tuples": dead_tuples,
                },
            )
            result = await _vacuum_table(db, table, full=True)
            maintenance_report["vacuum_operations"].append(result)

            if result["success"]:
                logger.info(
                    "VACUUM FULL complete",
                    extra={
                        "table_name": table,
                        "space_reclaimed_mb": result["space_reclaimed_mb"],
                        "duration_seconds": result["duration_seconds"],
                    },
                )

        # Regular VACUUM if >10% bloat OR >1,000 dead tuples
        elif bloat_pct > 10 or dead_tuples > 1000:
            logger.info(
                "Running VACUUM",
                extra={
                    "table_name": table,
                    "bloat_pct": bloat_pct,
                    "dead_tuples": dead_tuples,
                },
            )
            result = await _vacuum_table(db, table, full=False)
            maintenance_report["vacuum_operations"].append(result)

            if result["success"]:
                logger.info(
                    "VACUUM complete",
                    extra={
                        "table_name": table,
                        "space_reclaimed_mb": result["space_reclaimed_mb"],
                        "duration_seconds": result["duration_seconds"],
                    },
                )

    # Step 3: Data retention policy enforcement
    logger.info("Enforcing data retention policies...")

    # Import here to avoid circular dependency
    from app.services.core.settings_core_service import SettingsCoreService

    retention_stats = {"artifacts_deleted": 0, "notifications_deleted": 0}

    # Delete old check artifacts
    try:
        artifact_retention_days = await SettingsCoreService.get_setting(
            db, "database.artifacts_retention_days", 7
        )
        artifact_cutoff = utc_now() - timedelta(days=artifact_retention_days)

        result = await db.execute(
            text("DELETE FROM check_artifacts WHERE created_at < :cutoff"),
            {"cutoff": artifact_cutoff},
        )
        retention_stats["artifacts_deleted"] = result.rowcount
        await db.commit()

        if retention_stats["artifacts_deleted"] > 0:
            logger.info(
                "Deleted old artifacts",
                extra={
                    "artifacts_deleted": retention_stats["artifacts_deleted"],
                    "older_than_days": artifact_retention_days,
                },
            )
    except Exception:
        logger.error("Error deleting old artifacts", exc_info=True)
        await db.rollback()

    # Delete old notification logs
    try:
        notification_retention_days = await SettingsCoreService.get_setting(
            db, "database.notification_logs_retention_days", 90
        )

        if notification_retention_days > 0:  # 0 = keep forever
            notification_cutoff = utc_now() - timedelta(days=notification_retention_days)

            result = await db.execute(
                text("DELETE FROM notification_logs WHERE created_at < :cutoff"),
                {"cutoff": notification_cutoff},
            )
            retention_stats["notifications_deleted"] = result.rowcount
            await db.commit()

            if retention_stats["notifications_deleted"] > 0:
                logger.info(
                    "Deleted old notification logs",
                    extra={
                        "notifications_deleted": retention_stats["notifications_deleted"],
                        "older_than_days": notification_retention_days,
                    },
                )
    except Exception:
        logger.error("Error deleting old notification logs", exc_info=True)
        await db.rollback()

    maintenance_report["retention_stats"] = retention_stats

    # Step 4: Refresh TimescaleDB continuous aggregates
    try:
        logger.info("Refreshing continuous aggregates...")
        await db.execute(
            text("CALL refresh_continuous_aggregate('check_results_5min', NULL, NULL)")
        )
        await db.execute(
            text("CALL refresh_continuous_aggregate('check_results_hourly', NULL, NULL)")
        )
        await db.execute(
            text("CALL refresh_continuous_aggregate('check_results_daily', NULL, NULL)")
        )
        await db.commit()
        logger.info("  ✓ Continuous aggregates refreshed")
    except Exception:
        logger.warning("Error refreshing continuous aggregates", exc_info=True)

    # Calculate total stats
    total_duration = (datetime.utcnow() - start_time).total_seconds()
    total_reclaimed = sum(
        op.get("space_reclaimed_mb", 0)
        for op in maintenance_report["vacuum_operations"]
        if op.get("success")
    )

    maintenance_report["cycle_end"] = datetime.utcnow().isoformat()
    maintenance_report["total_duration_seconds"] = round(total_duration, 2)
    maintenance_report["total_space_reclaimed_mb"] = round(total_reclaimed, 2)

    logger.info(
        "Database maintenance cycle complete",
        extra={
            "total_reclaimed_mb": round(total_reclaimed, 2),
            "total_duration_seconds": round(total_duration, 1),
        },
    )

    return maintenance_report


async def _database_maintenance_loop():
    """Background task loop for database maintenance."""
    interval_hours = settings.server.database_maintenance_interval_hours
    logger.info(
        "Database maintenance task started",
        extra={"interval_hours": interval_hours},
    )

    # Get session maker
    session_maker = get_session_maker()

    while True:
        try:
            # Wait for interval
            await asyncio.sleep(interval_hours * 3600)

            # Run maintenance cycle
            async with session_maker() as db:
                report = await _run_maintenance_cycle(db)

                # Log summary
                if report["total_space_reclaimed_mb"] > 0:
                    logger.info(
                        "Maintenance summary",
                        extra={
                            "operation_count": len(report["vacuum_operations"]),
                            "total_space_reclaimed_mb": report["total_space_reclaimed_mb"],
                        },
                    )

        except asyncio.CancelledError:
            logger.info("Database maintenance task cancelled")
            break
        except Exception:
            logger.error("Error in database maintenance task", exc_info=True)
            # Continue running despite errors


def start_database_maintenance_task() -> asyncio.Task:
    """
    Start the database maintenance background task.

    Returns:
        Task handle for the background task
    """
    global _maintenance_task

    if _maintenance_task is not None:
        logger.warning("Database maintenance task already running")
        return _maintenance_task

    _maintenance_task = asyncio.create_task(_database_maintenance_loop())
    logger.info("Database maintenance background task started")

    return _maintenance_task


async def stop_database_maintenance_task():
    """Stop the database maintenance background task."""
    global _maintenance_task

    if _maintenance_task is None:
        return

    logger.info("Stopping database maintenance task")
    _maintenance_task.cancel()

    try:
        await _maintenance_task
    except asyncio.CancelledError:
        pass

    _maintenance_task = None
    logger.info("Database maintenance task stopped")
