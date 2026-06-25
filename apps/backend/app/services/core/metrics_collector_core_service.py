"""
In-memory Prometheus metrics collector.

This service maintains metrics in memory and updates them as check results arrive,
rather than querying the database on every Prometheus scrape.

Performance:
- Memory footprint: ~70KB for 1000 checks
- Response time: <1ms (vs 2.5s database query)
- No database load on Prometheus scrapes
"""

import time
from datetime import timedelta
from typing import TYPE_CHECKING

from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.datetime_utils import utc_now
from app.crud.agent_crud import AgentCRUD
from app.crud.check_result_crud import CheckResultCRUD
from app.services.core.settings_core_service import SettingsCoreService

# Server start time (set when this module is imported during lifespan startup).
# Used for the luxswirl_server_uptime_seconds metric.
_startup_monotonic = time.monotonic()

if TYPE_CHECKING:
    from app.models.agent_metric_model import AgentMetric
    from app.models.agent_model import Agent
    from app.models.check_model import Check
    from app.models.check_result_model import CheckResult

logger = get_logger("luxswirl.services.metrics_collector")

# Global registry for all LuxSwirl metrics
registry = CollectorRegistry()

# ============================================================================
# CHECK METRICS
# ============================================================================

check_success = Gauge(
    "luxswirl_check_success",
    "Whether the check was successful (1 for success, 0 for failure)",
    ["agent", "check", "type", "target"],
    registry=registry,
)

check_up = Gauge(
    "luxswirl_check_up",
    "Whether the check is still reporting (1) or has stopped (0)",
    ["agent", "check", "type", "target"],
    registry=registry,
)

check_latency_seconds = Gauge(
    "luxswirl_check_latency_seconds",
    "The check latency in seconds",
    ["agent", "check", "type", "target"],
    registry=registry,
)

check_last_execution_time = Gauge(
    "luxswirl_check_last_execution_time",
    "The timestamp of the last check execution (Unix timestamp)",
    ["agent", "check", "type", "target"],
    registry=registry,
)

# HTTP-specific metrics
check_http_status_code = Gauge(
    "luxswirl_check_http_status_code",
    "HTTP status code for HTTP checks",
    ["agent", "check", "target", "code"],
    registry=registry,
)

# DNS-specific metrics
check_dns_ttl = Gauge(
    "luxswirl_check_dns_ttl",
    "DNS record TTL in seconds",
    ["agent", "check", "target"],
    registry=registry,
)

check_dns_record_count = Gauge(
    "luxswirl_check_dns_record_count",
    "Number of DNS records returned",
    ["agent", "check", "target"],
    registry=registry,
)

# Database-specific metrics
check_db_connection_latency_seconds = Gauge(
    "luxswirl_check_db_connection_latency_seconds",
    "Database connection latency in seconds",
    ["agent", "check", "type", "target"],
    registry=registry,
)

check_db_query_latency_seconds = Gauge(
    "luxswirl_check_db_query_latency_seconds",
    "Database query execution latency in seconds",
    ["agent", "check", "type", "target"],
    registry=registry,
)

check_db_row_count = Gauge(
    "luxswirl_check_db_row_count",
    "Number of rows returned by database query",
    ["agent", "check", "type", "target"],
    registry=registry,
)

# ============================================================================
# AGENT METRICS
# ============================================================================

agent_up = Gauge(
    "luxswirl_agent_up",
    "Whether the agent is reporting (1 for up, 0 for down)",
    ["agent"],
    registry=registry,
)

agent_last_seen = Gauge(
    "luxswirl_agent_last_seen",
    "The timestamp when agent was last seen (Unix timestamp)",
    ["agent"],
    registry=registry,
)

agent_cpu_percent = Gauge(
    "luxswirl_agent_cpu_percent",
    "Agent CPU usage percentage",
    ["agent"],
    registry=registry,
)

agent_memory_mb = Gauge(
    "luxswirl_agent_memory_mb",
    "Agent memory usage in MB",
    ["agent"],
    registry=registry,
)

agent_queue_depth = Gauge(
    "luxswirl_agent_queue_depth",
    "Current result queue depth",
    ["agent"],
    registry=registry,
)

agent_queue_max_size = Gauge(
    "luxswirl_agent_queue_max_size",
    "Peak queue size since last heartbeat",
    ["agent"],
    registry=registry,
)

agent_checks_executed_total = Counter(
    "luxswirl_agent_checks_executed_total",
    "Total number of checks executed by agent",
    ["agent"],
    registry=registry,
)

agent_checks_succeeded_total = Counter(
    "luxswirl_agent_checks_succeeded_total",
    "Total number of successful checks",
    ["agent"],
    registry=registry,
)

agent_checks_failed_total = Counter(
    "luxswirl_agent_checks_failed_total",
    "Total number of failed checks",
    ["agent"],
    registry=registry,
)

agent_avg_check_duration_ms = Gauge(
    "luxswirl_agent_avg_check_duration_ms",
    "Average check duration in milliseconds",
    ["agent"],
    registry=registry,
)

agent_errors_total = Counter(
    "luxswirl_agent_errors_total",
    "Total number of errors reported by agent",
    ["agent"],
    registry=registry,
)

agent_warnings_total = Counter(
    "luxswirl_agent_warnings_total",
    "Total number of warnings reported by agent",
    ["agent"],
    registry=registry,
)

# ============================================================================
# SYSTEM METRICS (server-wide aggregates, refreshed periodically)
# ============================================================================

database_size_bytes = Gauge(
    "luxswirl_database_size_bytes",
    "Total size of the LuxSwirl PostgreSQL database in bytes",
    registry=registry,
)

database_compression_ratio = Gauge(
    "luxswirl_database_compression_ratio",
    "Fraction of TimescaleDB chunks that are compressed (0.0-1.0)",
    registry=registry,
)

total_agents = Gauge(
    "luxswirl_total_agents",
    "Total number of registered agents (regardless of online status)",
    registry=registry,
)

active_agents = Gauge(
    "luxswirl_active_agents",
    "Number of agents seen within the configured timeout window",
    registry=registry,
)

total_checks = Gauge(
    "luxswirl_total_checks",
    "Total number of configured checks across all agents",
    registry=registry,
)

active_checks = Gauge(
    "luxswirl_active_checks",
    "Number of checks belonging to agents currently active",
    registry=registry,
)

check_results_total_approx = Gauge(
    "luxswirl_check_results_total_approx",
    "Approximate total rows in the check_results hypertable (TimescaleDB approximate_row_count)",
    registry=registry,
)

# ============================================================================
# OPERATIONAL METRICS (alerts, jobs, notifications, uptime)
# ============================================================================

alerts_enabled = Gauge(
    "luxswirl_alerts_enabled",
    "Number of alert rules currently enabled",
    registry=registry,
)

jobs_status_count = Gauge(
    "luxswirl_jobs_status_count",
    "Number of jobs in each lifecycle status",
    ["status"],
    registry=registry,
)

notifications_total = Gauge(
    "luxswirl_notifications_total",
    "Cumulative count of notification log entries by status",
    ["status"],
    registry=registry,
)

server_uptime_seconds = Gauge(
    "luxswirl_server_uptime_seconds",
    "Seconds since the server process started",
    registry=registry,
)


class MetricsCollectorCoreService:
    """In-memory Prometheus metrics collector."""

    @staticmethod
    def update_check_result(
        result: CheckResult,
        check: Check,
        agent: Agent,
    ) -> None:
        """
        Update check metrics when a new result arrives.

        This is called during ingestion (when agent reports results),
        NOT during Prometheus scrape.

        Args:
            result: Check result to record
            check: Check configuration
            agent: Agent that executed the check
        """
        labels = {
            "agent": agent.agent_name,
            "check": check.display_name,
            "type": check.check_type,
            "target": check.target,
        }

        # Core check metrics
        check_success.labels(**labels).set(1 if result.success else 0)
        check_up.labels(**labels).set(1)  # Just received result, so it's up

        if result.latency_ms is not None:
            check_latency_seconds.labels(**labels).set(result.latency_ms / 1000.0)

        check_last_execution_time.labels(**labels).set(result.timestamp.timestamp())

        # HTTP-specific metrics
        if check.check_type == "http" and result.http_status_code:
            http_labels = {
                "agent": agent.agent_name,
                "check": check.display_name,
                "target": check.target,
                "code": str(result.http_status_code),
            }
            check_http_status_code.labels(**http_labels).set(1)

        # Check-type-specific metrics from metrics JSON field
        metrics_data = result.get_metrics()

        # DNS metrics
        if check.check_type == "dns" and "dns" in metrics_data:
            dns_labels = {
                "agent": agent.agent_name,
                "check": check.display_name,
                "target": check.target,
            }
            dns = metrics_data["dns"]

            if "ttl" in dns:
                check_dns_ttl.labels(**dns_labels).set(dns["ttl"])
            if "record_count" in dns:
                check_dns_record_count.labels(**dns_labels).set(dns["record_count"])

        # Database metrics (MySQL/PostgreSQL)
        if check.check_type in ("mysql", "postgres") and check.check_type in metrics_data:
            db_labels = {
                "agent": agent.agent_name,
                "check": check.display_name,
                "type": check.check_type,
                "target": check.target,
            }
            db_metrics = metrics_data[check.check_type]

            if "connection_latency_ms" in db_metrics:
                check_db_connection_latency_seconds.labels(**db_labels).set(
                    db_metrics["connection_latency_ms"] / 1000.0
                )
            if "query_latency_ms" in db_metrics:
                check_db_query_latency_seconds.labels(**db_labels).set(
                    db_metrics["query_latency_ms"] / 1000.0
                )
            if "row_count" in db_metrics:
                check_db_row_count.labels(**db_labels).set(db_metrics["row_count"])

    @staticmethod
    def update_agent_status(agent: Agent) -> None:
        """
        Update agent status metrics.

        Called when agent heartbeat is received.

        Args:
            agent: Agent to update metrics for
        """
        labels = {"agent": agent.agent_name}

        # Calculate if agent is online based on last_seen (5 minute threshold)
        is_up = agent.is_online
        agent_up.labels(**labels).set(1 if is_up else 0)

        if agent.last_seen:
            agent_last_seen.labels(**labels).set(agent.last_seen.timestamp())

    @staticmethod
    def update_agent_metrics(metric: AgentMetric, agent: Agent) -> None:
        """
        Update agent health metrics.

        Called when agent reports health metrics.

        Args:
            metric: Agent metric data
            agent: Agent that reported the metrics
        """
        labels = {"agent": agent.agent_name}

        # Resource usage
        if metric.cpu_percent is not None:
            agent_cpu_percent.labels(**labels).set(metric.cpu_percent)
        if metric.memory_mb is not None:
            agent_memory_mb.labels(**labels).set(metric.memory_mb)

        # Queue health
        if metric.queue_depth is not None:
            agent_queue_depth.labels(**labels).set(metric.queue_depth)
        if metric.queue_max_size is not None:
            agent_queue_max_size.labels(**labels).set(metric.queue_max_size)

        # Check throughput (use counter.inc() not set())
        if metric.checks_executed is not None and metric.checks_executed > 0:
            agent_checks_executed_total.labels(**labels).inc(metric.checks_executed)
        if metric.checks_succeeded is not None and metric.checks_succeeded > 0:
            agent_checks_succeeded_total.labels(**labels).inc(metric.checks_succeeded)
        if metric.checks_failed is not None and metric.checks_failed > 0:
            agent_checks_failed_total.labels(**labels).inc(metric.checks_failed)

        # Average check duration
        if metric.avg_check_duration_ms is not None:
            agent_avg_check_duration_ms.labels(**labels).set(metric.avg_check_duration_ms)

        # Error tracking
        if metric.errors_count is not None and metric.errors_count > 0:
            agent_errors_total.labels(**labels).inc(metric.errors_count)
        if metric.warnings_count is not None and metric.warnings_count > 0:
            agent_warnings_total.labels(**labels).inc(metric.warnings_count)

    @staticmethod
    async def refresh_agent_status(db: AsyncSession) -> None:
        """
        Refresh agent up/down status based on current time vs last_seen.

        Called on every Prometheus scrape to ensure up metric is current.
        Very fast query - just gets agent_name and last_seen for all agents.

        Args:
            db: Database session
        """

        # Get configurable timeout threshold (default 300 seconds = 5 minutes)
        timeout_seconds = await SettingsCoreService.get_setting(
            db, "metrics.agent_timeout_seconds", 300
        )

        # Quick query - only get what we need
        agents = await AgentCRUD.get_name_lastseen_pairs(db)

        # Update agent_up metric for each agent based on current time
        for agent_name, last_seen in agents:
            if not last_seen:
                # No last_seen = agent is down
                agent_up.labels(agent=agent_name).set(0)
                continue

            # Check if agent is online (last_seen within configured threshold)
            elapsed = (utc_now() - last_seen).total_seconds()
            is_up = elapsed < timeout_seconds
            agent_up.labels(agent=agent_name).set(1 if is_up else 0)

    @staticmethod
    def update_system_metrics(metrics: dict) -> None:
        """
        Update server-wide aggregate gauges from a metrics dict.

        Expected dict keys (any may be omitted):
            - database_size_bytes: int
            - hypertable_chunks: list[{"total_chunks", "compressed_chunks", ...}]
            - agent_count: int
            - check_count: int
            - check_results_approx_rows: int
            - active_agent_count: int
            - active_check_count: int

        Args:
            metrics: Dict produced by collect_database_metrics()
        """
        if "database_size_bytes" in metrics and metrics["database_size_bytes"] is not None:
            database_size_bytes.set(metrics["database_size_bytes"])

        if "agent_count" in metrics and metrics["agent_count"] is not None:
            total_agents.set(metrics["agent_count"])

        if "check_count" in metrics and metrics["check_count"] is not None:
            total_checks.set(metrics["check_count"])

        if "active_agent_count" in metrics and metrics["active_agent_count"] is not None:
            active_agents.set(metrics["active_agent_count"])

        if "active_check_count" in metrics and metrics["active_check_count"] is not None:
            active_checks.set(metrics["active_check_count"])

        if (
            "check_results_approx_rows" in metrics
            and metrics["check_results_approx_rows"] is not None
        ):
            check_results_total_approx.set(metrics["check_results_approx_rows"])

        # TimescaleDB compression ratio: weighted average across all hypertables
        chunks_info = metrics.get("hypertable_chunks") or []
        total = sum(h.get("total_chunks", 0) or 0 for h in chunks_info)
        compressed = sum(h.get("compressed_chunks", 0) or 0 for h in chunks_info)
        if total > 0:
            database_compression_ratio.set(compressed / total)

    @staticmethod
    def update_operational_metrics(operational: dict) -> None:
        """
        Update fast-changing operational gauges from a metrics dict.

        Expected dict keys (any may be omitted):
            - alerts_enabled: int
            - job_status_counts: dict[str, int]  (e.g. {"pending": 3, "running": 1})
            - notification_status_counts: dict[str, int]  (e.g. {"sent": 100, "failed": 2})

        Args:
            operational: Dict produced by collect_operational_metrics()
        """
        if "alerts_enabled" in operational and operational["alerts_enabled"] is not None:
            alerts_enabled.set(operational["alerts_enabled"])

        # Always update server uptime when operational refresh runs
        server_uptime_seconds.set(time.monotonic() - _startup_monotonic)

        for status, count in (operational.get("job_status_counts") or {}).items():
            jobs_status_count.labels(status=status).set(count)

        for status, count in (operational.get("notification_status_counts") or {}).items():
            notifications_total.labels(status=status).set(count)

    @staticmethod
    def generate() -> bytes:
        """
        Generate Prometheus metrics in text format.

        This is called on Prometheus scrape and is instant (no DB query).

        Returns:
            Prometheus metrics in text format (bytes)
        """
        return generate_latest(registry)

    @staticmethod
    async def rebuild_from_database(db: AsyncSession, lookback_minutes: int = 10) -> None:
        """
        Rebuild metrics from database on startup.

        Queries the database once to populate in-memory metrics with recent state.
        After this, metrics are updated via ingestion, not queries.

        Args:
            db: Database session
            lookback_minutes: How many minutes of recent data to load
        """
        logger.info("Rebuilding Prometheus metrics from database...")
        cutoff = utc_now() - timedelta(minutes=lookback_minutes)

        # Import here to avoid circular dependency

        # Get recent agents
        agents = await AgentCRUD.list_seen_since(db, cutoff)

        logger.info(
            "Found active agents",
            extra={"agent_count": len(agents)},
        )

        # Update agent status
        for agent in agents:
            MetricsCollectorCoreService.update_agent_status(agent)

        # Get latest agent metrics for each agent
        for agent in agents:
            latest_metric = await AgentCRUD.get_latest_metric(db, agent.id)

            if latest_metric:
                MetricsCollectorCoreService.update_agent_metrics(latest_metric, agent)

        # Get latest check results for each check
        check_count = 0
        for agent in agents:
            results = await CheckResultCRUD.get_latest_results_for_agent_with_check(
                db, agent.id, cutoff
            )

            for check_result, check in results:
                MetricsCollectorCoreService.update_check_result(check_result, check, agent)
                check_count += 1

        logger.info(
            "Metrics rebuilt",
            extra={
                "agent_count": len(agents),
                "check_count": check_count,
                "lookback_minutes": lookback_minutes,
            },
        )
