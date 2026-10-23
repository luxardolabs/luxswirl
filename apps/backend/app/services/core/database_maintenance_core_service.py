"""
Database maintenance: bloat-driven VACUUM, retention, continuous-aggregate refresh.

One cycle:

1. measure bloat on the large tables;
2. VACUUM FULL a table over 50% bloat or 10,000 dead tuples, plain VACUUM over
   10% or 1,000;
3. delete check artifacts and notification logs past their retention settings;
4. refresh the TimescaleDB continuous aggregates.

Each step is its own unit of work (`worker_session()` per step, or an AUTOCOMMIT
connection for VACUUM and the refresh), so one failing step is reported and the
rest still run. The SQL lives in `DatabaseMaintenanceCRUD`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.datetime_utils import utc_now
from app.crud.artifact_crud import ArtifactCRUD
from app.crud.database_maintenance_crud import (
    CONTINUOUS_AGGREGATES,
    DatabaseMaintenanceCRUD,
)
from app.crud.notification_log_crud import NotificationLogCRUD
from app.db import worker_session
from app.schemas.database_maintenance_schema import (
    MaintenanceReport,
    TableBloat,
    VacuumResult,
)
from app.services.core.settings_core_service import SettingsCoreService
from app.shared.logger import get_logger

logger = get_logger("luxswirl.background.db_maintenance")

# The tables a cycle measures and, past the thresholds, vacuums.
CYCLE_TABLES = (
    "check_results",
    "check_artifacts",
    "checks",
    "agents",
    "jobs",
    "notification_logs",
)

FULL_VACUUM_BLOAT_PCT = 50
FULL_VACUUM_DEAD_TUPLES = 10_000
VACUUM_BLOAT_PCT = 10
VACUUM_DEAD_TUPLES = 1_000


def vacuum_kind(bloat: TableBloat) -> bool | None:
    """True for VACUUM FULL, False for plain VACUUM, None for no VACUUM."""
    if (
        bloat.bloat_percentage > FULL_VACUUM_BLOAT_PCT
        or bloat.dead_tuples > FULL_VACUUM_DEAD_TUPLES
    ):
        return True
    if (
        bloat.bloat_percentage > VACUUM_BLOAT_PCT
        or bloat.dead_tuples > VACUUM_DEAD_TUPLES
    ):
        return False
    return None


class DatabaseMaintenanceCoreService:
    """The maintenance cycle and its steps."""

    @staticmethod
    async def measure_bloat(table: str) -> TableBloat:
        """Size, row count and dead/live tuples for `table` (read-only)."""
        async with worker_session() as db:
            size = await DatabaseMaintenanceCRUD.table_size_bytes(db, table)
            rows = await DatabaseMaintenanceCRUD.row_count(db, table)
            dead, live = await DatabaseMaintenanceCRUD.tuple_stats(db, table)
        return TableBloat(
            table=table,
            size_bytes=int(size or 0),
            row_count=rows,
            live_tuples=live,
            dead_tuples=dead,
        )

    @staticmethod
    async def vacuum(table: str, *, full: bool) -> VacuumResult:
        """VACUUM (FULL) `table`; a failure is returned, not raised."""
        started = datetime.now(UTC)
        try:
            before, after = await DatabaseMaintenanceCRUD.vacuum(table, full=full)
        # swallowed-exceptions: per-table result: the failure is returned in the
        # report so the remaining tables still run.
        except Exception as exc:
            logger.exception("Error vacuuming table", extra={"table_name": table})
            return VacuumResult(
                table=table,
                full=full,
                duration_seconds=(datetime.now(UTC) - started).total_seconds(),
                error=type(exc).__name__,
            )
        return VacuumResult(
            table=table,
            full=full,
            duration_seconds=round((datetime.now(UTC) - started).total_seconds(), 2),
            size_before_bytes=before,
            size_after_bytes=after,
        )

    @staticmethod
    async def enforce_artifact_retention() -> int:
        """Delete check artifacts older than `database.artifacts_retention_days`."""
        async with worker_session() as db:
            days = await SettingsCoreService.get_setting(
                db, "database.artifacts_retention_days", 7
            )
            return await ArtifactCRUD.delete_older_than(
                db, utc_now() - timedelta(days=days)
            )

    @staticmethod
    async def enforce_notification_retention() -> int:
        """Delete notification logs older than the retention setting (0 keeps all)."""
        async with worker_session() as db:
            days = await SettingsCoreService.get_setting(
                db, "database.notification_logs_retention_days", 90
            )
            if days <= 0:
                return 0
            return await NotificationLogCRUD.delete_older_than(
                db, utc_now() - timedelta(days=days)
            )

    @staticmethod
    async def run_cycle() -> MaintenanceReport:
        """One full maintenance cycle (see the module docstring)."""
        report = MaintenanceReport(started_at=datetime.now(UTC))
        logger.info("Starting database maintenance cycle")

        for table in CYCLE_TABLES:
            try:
                bloat = await DatabaseMaintenanceCoreService.measure_bloat(table)
            # swallowed-exceptions: per-table measurement; reported, the rest still run.
            except Exception as exc:
                logger.exception("Error calculating bloat", extra={"table_name": table})
                report.failures.append(f"bloat:{table}:{type(exc).__name__}")
                continue
            report.bloat.append(bloat)
            logger.info(
                "Table bloat report",
                extra={
                    "table_name": table,
                    "actual_size_mb": bloat.size_mb,
                    "row_count": bloat.row_count,
                    "bloat_percentage": bloat.bloat_percentage,
                    "dead_tuples": bloat.dead_tuples,
                },
            )

        for bloat in report.bloat:
            full = vacuum_kind(bloat)
            if full is None:
                continue
            result = await DatabaseMaintenanceCoreService.vacuum(bloat.table, full=full)
            report.vacuums.append(result)
            if result.error is not None:
                report.failures.append(f"vacuum:{bloat.table}:{result.error}")
                continue
            logger.info(
                "VACUUM complete",
                extra={
                    "table_name": bloat.table,
                    "full": full,
                    "bloat_pct": bloat.bloat_percentage,
                    "dead_tuples": bloat.dead_tuples,
                    "space_reclaimed_mb": result.space_reclaimed_mb,
                    "duration_seconds": result.duration_seconds,
                },
            )

        try:
            report.artifacts_deleted = (
                await DatabaseMaintenanceCoreService.enforce_artifact_retention()
            )
        # swallowed-exceptions: one retention step; reported, the rest still run.
        except Exception as exc:
            logger.exception("Error deleting old artifacts")
            report.failures.append(f"retention:artifacts:{type(exc).__name__}")

        try:
            report.notifications_deleted = (
                await DatabaseMaintenanceCoreService.enforce_notification_retention()
            )
        # swallowed-exceptions: one retention step; reported, the rest still run.
        except Exception as exc:
            logger.exception("Error deleting old notification logs")
            report.failures.append(f"retention:notifications:{type(exc).__name__}")

        for name in CONTINUOUS_AGGREGATES:
            try:
                await DatabaseMaintenanceCRUD.refresh_continuous_aggregate(name)
            # swallowed-exceptions: one aggregate; reported, the others still refresh.
            except Exception as exc:
                logger.warning(
                    "Error refreshing continuous aggregate",
                    exc_info=True,
                    extra={"aggregate": name},
                )
                report.failures.append(f"refresh:{name}:{type(exc).__name__}")
            else:
                report.aggregates_refreshed.append(name)

        report.duration_seconds = round(
            (datetime.now(UTC) - report.started_at).total_seconds(), 2
        )
        logger.info(
            "Database maintenance cycle complete",
            extra={
                "total_reclaimed_mb": report.space_reclaimed_mb,
                "total_duration_seconds": report.duration_seconds,
                "artifacts_deleted": report.artifacts_deleted,
                "notifications_deleted": report.notifications_deleted,
                "aggregates_refreshed": len(report.aggregates_refreshed),
                "failures": len(report.failures),
            },
        )
        return report
