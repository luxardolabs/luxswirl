"""
Monitoring job functions for the LuxSwirl scheduler.

Collects database health metrics, operational state, and pushes them
into the in-memory Prometheus metrics collector.
"""

from datetime import timedelta

from shared.logger import get_logger

from app.core.datetime_utils import utc_now
from app.crud.agent_crud import AgentCRUD
from app.crud.alert_crud import AlertCRUD
from app.crud.check_crud import CheckCRUD
from app.crud.job_crud import JobCRUD
from app.crud.monitoring_crud import MonitoringCRUD
from app.crud.notification_log_crud import NotificationLogCRUD
from app.db import worker_session
from app.models.enum_model import JobStatus, NotificationStatus
from app.services.core.metrics_collector_core_service import MetricsCollectorCoreService

logger = get_logger("luxswirl.scheduler.monitoring")

# Active-agent threshold: agents seen within this many seconds count as "active"
_ACTIVE_AGENT_TIMEOUT_SECONDS = 300


async def collect_database_metrics() -> dict:
    """
    Collect PostgreSQL/TimescaleDB health metrics + push system gauges to Prometheus.

    Gathers:
    - Database size, table sizes, active connections
    - TimescaleDB chunk info and compression stats
    - Agent / check / result counts (total and active)

    Pushes results into the in-memory Prometheus collector so /metrics stays <1ms.

    Returns:
        Dict with collected metrics (also retained for scheduler logging)
    """
    async with worker_session() as db:
        metrics: dict = {}

        # Database size
        db_size = await MonitoringCRUD.get_database_size_bytes(db)
        metrics["database_size_bytes"] = db_size
        metrics["database_size_mb"] = (
            round(db_size / (1024 * 1024), 2) if db_size is not None else 0
        )

        # Active connections
        metrics["active_connections"] = await MonitoringCRUD.get_active_connection_count(db)

        # Table sizes (top 10)
        table_rows = await MonitoringCRUD.get_top_table_sizes(db, limit=10)
        metrics["table_sizes"] = [{"table": row[0], "size_bytes": row[1]} for row in table_rows]

        # TimescaleDB chunk count and compression stats. SAVEPOINT-guarded: a bare
        # try/except is NOT enough — once a query errors, the asyncpg transaction is
        # aborted and every metric below would fail with InFailedSQLTransactionError.
        # begin_nested() rolls back just this query so the rest still collect.
        try:
            async with db.begin_nested():
                chunk_rows = await MonitoringCRUD.get_hypertable_chunk_stats(db)
            metrics["hypertable_chunks"] = [
                {
                    "hypertable": row[0],
                    "total_chunks": row[1],
                    "compressed_chunks": row[2],
                }
                for row in chunk_rows
            ]
        except Exception as e:
            metrics["hypertable_chunks_error"] = str(e)

        # Check results count — TimescaleDB hypertable, must use approximate_row_count()
        # because pg_stat_user_tables always shows 0 for hypertables (data lives in chunks).
        try:
            async with db.begin_nested():
                approx = await MonitoringCRUD.get_check_results_approx_count(db)
            metrics["check_results_approx_rows"] = approx or 0
        except Exception as e:
            logger.warning("Failed to get check_results approximate count", extra={"error": str(e)})
            metrics["check_results_approx_rows"] = 0

        # Agent count
        metrics["agent_count"] = await AgentCRUD.count_all(db)

        # Check count
        metrics["check_count"] = await CheckCRUD.count_all(db)

        # Active agents (seen within timeout)
        cutoff = utc_now() - timedelta(seconds=_ACTIVE_AGENT_TIMEOUT_SECONDS)
        active_agents_list = await AgentCRUD.list_seen_since(db, cutoff)
        metrics["active_agent_count"] = len(active_agents_list)

        # Active checks (belonging to active agents)
        metrics["active_check_count"] = await CheckCRUD.count_for_agents_seen_since(db, cutoff)

        logger.info(
            "Collected database metrics",
            extra={
                "db_size_mb": metrics["database_size_mb"],
                "connections": metrics["active_connections"],
                "agents": metrics["agent_count"],
                "active_agents": metrics["active_agent_count"],
                "checks": metrics["check_count"],
                "active_checks": metrics["active_check_count"],
            },
        )

        # Push to Prometheus collector
        MetricsCollectorCoreService.update_system_metrics(metrics)

        return metrics


async def collect_operational_metrics() -> dict:
    """
    Collect fast-changing operational state and push to Prometheus.

    Gathers:
    - Enabled alert rule count
    - Job lifecycle status counts
    - Notification log status counts
    - Server uptime (refreshed inside MetricsCollectorCoreService.update_operational_metrics)

    Designed for a short scheduling interval (~60s) so dashboards stay current.

    Returns:
        Dict with collected operational state (also retained for scheduler logging)
    """
    async with worker_session() as db:
        operational: dict = {}

        # Enabled alert rules
        operational["alerts_enabled"] = await AlertCRUD.count_enabled(db)

        # Job status counts
        try:
            job_summary = await JobCRUD.get_job_status_summary(db)
            operational["job_status_counts"] = {
                s.value: int(job_summary.get(s.value, 0)) for s in JobStatus
            }
        except Exception as e:
            logger.warning("Failed to collect job status counts", extra={"error": str(e)})
            operational["job_status_counts"] = {}

        # Notification log status counts
        try:
            rows = await NotificationLogCRUD.get_status_counts(db)
            counts_dict = {status: int(count) for status, count in rows}
            operational["notification_status_counts"] = {
                s.value: counts_dict.get(s.value, 0) for s in NotificationStatus
            }
        except Exception as e:
            logger.warning("Failed to collect notification status counts", extra={"error": str(e)})
            operational["notification_status_counts"] = {}

        logger.info(
            "Collected operational metrics",
            extra={
                "alerts_enabled": operational["alerts_enabled"],
                "jobs_running": operational["job_status_counts"].get("running", 0),
                "jobs_pending": operational["job_status_counts"].get("pending", 0),
                "notifications_failed": operational["notification_status_counts"].get("failed", 0),
            },
        )

        # Push to Prometheus collector
        MetricsCollectorCoreService.update_operational_metrics(operational)

        return operational
