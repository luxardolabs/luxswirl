"""
Prometheus metrics endpoint mounted at /metrics (standard scrape path).

Uses in-memory metrics collector for instant response (<1ms, no DB queries).
Auth is configurable via Settings (public by default; bearer token if enabled).

Lives under api/v1/routers/ (canonical router home) but is mounted at the root
`/metrics` path by main.py's include_router (no prefix), so the scrape URL is
unchanged.
"""

import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db import get_db
from app.services.core.metrics_collector_core_service import MetricsCollectorCoreService
from app.services.core.settings_core_service import SettingsCoreService

router = APIRouter(tags=["Metrics"])


@router.get(
    "/metrics",
    summary="Prometheus metrics endpoint",
    description="Export metrics in Prometheus text format (optional auth)",
    response_class=Response,
)
async def get_metrics(
    db: Annotated[AsyncSession, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
):
    """
    Get Prometheus-formatted metrics from in-memory collector.

    Authentication:
    - By default: PUBLIC (no auth required) - standard for Prometheus
    - If enabled in settings: requires bearer token
    - Configure via Settings > API Keys > Prometheus Metrics

    Performance:
    - <1ms response time, no database queries
    - Metrics updated in real-time as check results arrive
    - On startup: rebuilds from last 10 minutes of database

    Prometheus scrape config (no auth):
    ```yaml
    scrape_configs:
      - job_name: 'luxswirl'
        static_configs:
          - targets: ['localhost:9000']
    ```

    Prometheus scrape config (Bearer token):
    ```yaml
    scrape_configs:
      - job_name: 'luxswirl'
        static_configs:
          - targets: ['localhost:9000']
        bearer_token_file: /etc/prometheus/luxswirl-token.txt
    ```

    Exported metrics include:

    Per-check (labels: agent, check, type, target):
    - luxswirl_check_success - Check success/failure (1/0)
    - luxswirl_check_up - Check reporting status (1/0)
    - luxswirl_check_latency_seconds - Check latency
    - luxswirl_check_last_execution_time - Last execution timestamp
    - luxswirl_check_http_status_code - HTTP status codes
    - luxswirl_check_dns_ttl - DNS record TTL
    - luxswirl_check_dns_record_count - DNS records returned
    - luxswirl_check_db_connection_latency_seconds - DB connection time
    - luxswirl_check_db_query_latency_seconds - DB query time
    - luxswirl_check_db_row_count - Rows returned

    Per-agent (label: agent):
    - luxswirl_agent_up - Agent online status (1/0)
    - luxswirl_agent_last_seen - Last seen timestamp
    - luxswirl_agent_cpu_percent / memory_mb / queue_depth - Resource usage
    - luxswirl_agent_checks_executed_total / succeeded_total / failed_total - Counters
    - luxswirl_agent_errors_total / warnings_total - Error counters

    System aggregates (refreshed every 5 minutes):
    - luxswirl_database_size_bytes - Total DB size
    - luxswirl_database_compression_ratio - Fraction of TimescaleDB chunks compressed (0-1)
    - luxswirl_total_agents / luxswirl_active_agents - Registered vs reporting
    - luxswirl_total_checks / luxswirl_active_checks - Configured vs assigned to active agents
    - luxswirl_check_results_total_approx - Approximate row count of check_results hypertable

    Operational state (refreshed every 60 seconds):
    - luxswirl_alerts_enabled - Number of alert rules enabled
    - luxswirl_jobs_status_count{status} - Jobs in each lifecycle state
    - luxswirl_notifications_total{status} - Notification log counts by status
    - luxswirl_server_uptime_seconds - Seconds since server process started
    """
    # Check if metrics endpoint is enabled
    metrics_enabled = await SettingsCoreService.get_setting(db, "metrics.enabled", True)
    if not metrics_enabled:
        # Return plain 404 as if endpoint doesn't exist
        return Response(status_code=404)

    # Get metrics settings from database
    auth_required = await SettingsCoreService.get_setting(db, "metrics.auth_required", False)
    configured_token = await SettingsCoreService.get_setting(db, "metrics.bearer_token", "")

    # Check if auth is required
    if auth_required:
        if not authorization:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Metrics authentication required. Configure in Settings > API Keys.",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Parse authorization header
        authenticated = False

        # Try Bearer token
        if authorization.startswith("Bearer "):
            token = authorization[7:]  # Remove "Bearer " prefix

            # Check against configured metrics token (if set) — constant-time.
            if configured_token and hmac.compare_digest(token, configured_token):
                authenticated = True
            # Fall back to API tokens
            elif any(hmac.compare_digest(token, t) for t in settings.security.auth_tokens):
                authenticated = True

        # Reject if no valid auth method worked
        if not authenticated:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    # Refresh agent up/down status based on current time vs last_seen
    # This ensures Prometheus always gets current agent state
    await MetricsCollectorCoreService.refresh_agent_status(db)

    metrics_bytes = MetricsCollectorCoreService.generate()

    return Response(
        content=metrics_bytes,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
