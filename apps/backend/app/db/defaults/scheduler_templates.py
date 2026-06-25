"""
Default job configurations for the LuxSwirl scheduler.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

# Job categories (string constants - no enum needed for simple cases)
CATEGORY_CLEANUP = "cleanup"
CATEGORY_MONITORING = "monitoring"
CATEGORY_SYSTEM = "system"

# Trigger types
TRIGGER_INTERVAL = "interval"
TRIGGER_CRON = "cron"
TRIGGER_MANUAL = "manual"

# Initial job configurations to create on first startup
DEFAULT_JOB_CONFIGURATIONS: list[dict[str, Any]] = [
    # ==========================================================================
    # CLEANUP JOBS
    # ==========================================================================
    # Cleanup old job executions - daily at 2 AM UTC
    {
        "job_key": "cleanup_job_executions",
        "function_name": "cleanup_old_job_executions",
        "display_name": "Cleanup Old Job Executions",
        "description": "Delete job execution records older than 90 days to keep the database trim",
        "category": CATEGORY_CLEANUP,
        "enabled": True,
        "parameters": {"days_to_keep": 90},
        "trigger_type": TRIGGER_CRON,
        "cron_expression": "0 2 * * *",  # Daily at 2 AM
        "next_run_at": datetime.now(UTC).replace(hour=2, minute=0, second=0, microsecond=0)
        + timedelta(days=1),
        "max_runtime_seconds": 300,
        "notify_on_failure": True,
    },
    # Cleanup old notification logs - daily at 2:30 AM UTC
    {
        "job_key": "cleanup_notification_logs",
        "function_name": "cleanup_notification_logs",
        "display_name": "Cleanup Old Notification Logs",
        "description": "Delete notification log records older than 30 days",
        "category": CATEGORY_CLEANUP,
        "enabled": True,
        "parameters": {"days_to_keep": 30},
        "trigger_type": TRIGGER_CRON,
        "cron_expression": "30 2 * * *",  # Daily at 2:30 AM
        "next_run_at": datetime.now(UTC).replace(hour=2, minute=30, second=0, microsecond=0)
        + timedelta(days=1),
        "max_runtime_seconds": 300,
        "notify_on_failure": True,
    },
    # Cleanup stale agents - daily at 3 AM UTC
    {
        "job_key": "cleanup_stale_agents",
        "function_name": "cleanup_stale_agents",
        "display_name": "Cleanup Stale Agents",
        "description": "Remove agents that haven't reported in 30 days (cascades to checks and results)",
        "category": CATEGORY_CLEANUP,
        "enabled": False,  # Disabled by default - destructive operation
        "parameters": {"inactive_days": 30},
        "trigger_type": TRIGGER_CRON,
        "cron_expression": "0 3 * * *",  # Daily at 3 AM
        "next_run_at": None,  # Disabled, no next run
        "max_runtime_seconds": 300,
        "notify_on_failure": True,
    },
    # Cleanup old check artifacts - daily at 3:30 AM UTC
    {
        "job_key": "cleanup_check_artifacts",
        "function_name": "cleanup_check_artifacts",
        "display_name": "Cleanup Old Check Artifacts",
        "description": "Delete check artifacts (screenshots, logs) older than 30 days",
        "category": CATEGORY_CLEANUP,
        "enabled": True,
        "parameters": {"days_to_keep": 30},
        "trigger_type": TRIGGER_CRON,
        "cron_expression": "30 3 * * *",  # Daily at 3:30 AM
        "next_run_at": datetime.now(UTC).replace(hour=3, minute=30, second=0, microsecond=0)
        + timedelta(days=1),
        "max_runtime_seconds": 300,
        "notify_on_failure": True,
    },
    # Cleanup expired sessions - every 6 hours
    {
        "job_key": "cleanup_old_sessions",
        "function_name": "cleanup_old_sessions",
        "display_name": "Cleanup Expired Sessions",
        "description": "Delete expired user sessions older than 7 days",
        "category": CATEGORY_CLEANUP,
        "enabled": True,
        "parameters": {"days_to_keep": 7},
        "trigger_type": TRIGGER_INTERVAL,
        "interval_seconds": 21600,  # Every 6 hours
        "next_run_at": datetime.now(UTC) + timedelta(hours=6),
        "max_runtime_seconds": 120,
        "notify_on_failure": True,
    },
    # ==========================================================================
    # MONITORING JOBS
    # ==========================================================================
    # Collect database metrics - every 5 minutes
    {
        "job_key": "collect_database_metrics",
        "function_name": "collect_database_metrics",
        "display_name": "Collect Database Metrics",
        "description": "Collect PostgreSQL/TimescaleDB health metrics for monitoring",
        "category": CATEGORY_MONITORING,
        "enabled": True,
        "parameters": {},
        "trigger_type": TRIGGER_INTERVAL,
        "interval_seconds": 300,  # Every 5 minutes
        "next_run_at": datetime.now(UTC) + timedelta(minutes=5),
        "max_runtime_seconds": 60,
        "retry_limit": 0,  # Never auto-disable - monitoring is critical
        "notify_on_failure": True,
    },
    # Collect operational metrics - every 60 seconds (alerts, jobs, notifications, uptime)
    {
        "job_key": "collect_operational_metrics",
        "function_name": "collect_operational_metrics",
        "display_name": "Collect Operational Metrics",
        "description": "Refresh fast-changing Prometheus gauges (alerts, jobs, notifications, uptime)",
        "category": CATEGORY_MONITORING,
        "enabled": True,
        "parameters": {},
        "trigger_type": TRIGGER_INTERVAL,
        "interval_seconds": 60,
        "next_run_at": datetime.now(UTC) + timedelta(seconds=60),
        "max_runtime_seconds": 30,
        "retry_limit": 0,  # Never auto-disable - monitoring is critical
        "notify_on_failure": True,
    },
]
