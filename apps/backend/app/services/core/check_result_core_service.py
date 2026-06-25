"""
CheckResult service - business logic for check result operations and analytics.
"""

import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_extensions import uuid7

from app.core.datetime_utils import utc_now
from app.core.exceptions import AgentNotFoundException
from app.crud.check_result_crud import CheckResultCRUD
from app.models.check_result_model import CheckResult
from app.schemas.check_result_schema import (
    AgentReportRequest,
    CheckResultCreate,
    CheckSummary,
)
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.alert_core_service import AlertCoreService
from app.services.core.check_core_service import CheckCoreService
from app.services.core.metrics_collector_core_service import MetricsCollectorCoreService
from app.services.core.settings_core_service import SettingsCoreService

logger = get_logger("luxswirl.services.check_result")


class CheckResultCoreService:
    """Service for check result operations and analytics."""

    @staticmethod
    async def compute_ssl_cert_info(db: AsyncSession, raw_cert_data: dict) -> dict:
        """
        Compute SSL certificate expiration info from raw cert data.

        Args:
            db: Database session for fetching settings
            raw_cert_data: Raw cert data from check (expiration_date, valid_from, issuer, subject)

        Returns:
            Dictionary with computed cert info including days_until_expiration, expired, expires_soon
        """
        try:
            expiration_str = raw_cert_data.get("expiration_date")
            if not expiration_str:
                return raw_cert_data

            # Parse expiration date (format: 'Jan 1 00:00:00 2025 GMT')
            expiration_date = datetime.strptime(expiration_str, "%b %d %H:%M:%S %Y %Z")

            # Calculate days until expiration
            now = datetime.utcnow()
            days_until_expiration = (expiration_date - now).days

            # Fetch SSL thresholds from settings
            ssl_warning_days = await SettingsCoreService.get_setting(
                db, "ssl_cert_warning_days", default=30
            )
            ssl_critical_days = await SettingsCoreService.get_setting(
                db, "ssl_cert_critical_days", default=14
            )

            # Compute status flags based on configurable thresholds
            expired = days_until_expiration < 0
            expires_soon = 0 < days_until_expiration <= ssl_warning_days
            expires_critical = 0 < days_until_expiration <= ssl_critical_days

            # Return original data plus computed fields
            return {
                **raw_cert_data,
                "days_until_expiration": days_until_expiration,
                "expired": expired,
                "expires_soon": expires_soon,
                "expires_critical": expires_critical,
            }

        except Exception:
            logger.warning("Failed to compute SSL cert info", exc_info=True)
            return raw_cert_data

    @staticmethod
    async def create_check_result(
        db: AsyncSession,
        agent_id: UUID,
        check_id: UUID,
        data: CheckResultCreate,
    ) -> CheckResult:
        """
        Create a new check result.

        Args:
            db: Database session
            agent_id: Agent UUID
            check_id: Check UUID
            data: Check result data

        Returns:
            Created check result
        """
        # Process SSL cert info if present in metrics
        metrics_dict = data.metrics or {}
        if "response" in metrics_dict and "ssl_certificate" in metrics_dict["response"]:
            raw_cert = metrics_dict["response"]["ssl_certificate"]
            computed_cert = await CheckResultCoreService.compute_ssl_cert_info(db, raw_cert)
            metrics_dict["response"]["ssl_certificate"] = computed_cert

        # Extract metrics if present
        metrics_json = None
        if metrics_dict:
            metrics_json = json.dumps(metrics_dict)

        result = CheckResult(
            agent_id=agent_id,
            check_id=check_id,
            timestamp=data.timestamp,
            success=data.success,
            latency_ms=data.latency_ms,
            error=data.error,
            error_type=data.error_type,
            http_status_code=data.http_status_code,
            http_response_time_ms=data.http_response_time_ms,
            metrics=metrics_json,
            response_data=data.response_data,
        )

        db.add(result)
        await db.flush()
        await db.refresh(result)

        # Evaluate alerts for this check result
        try:
            await AlertCoreService.evaluate_check_result(db, result)
        except Exception:
            logger.error(
                "Error evaluating alerts for check result",
                extra={"result_id": str(result.id)},
                exc_info=True,
            )

        return result

    @staticmethod
    async def get_latest_results_for_agent(
        db: AsyncSession,
        agent_name: str,
        minutes: int = 5,
    ) -> Sequence[CheckResult]:
        """
        Get latest check results for an agent.

        Args:
            db: Database session
            agent_name: Agent name
            minutes: How many minutes back to look

        Returns:
            List of latest check results (one per check)
        """
        agent = await AgentCoreService.get_agent_by_name(db, agent_name)
        if not agent:
            raise AgentNotFoundException(agent_name)

        cutoff_time = utc_now() - timedelta(minutes=minutes)
        return await CheckResultCRUD.get_latest_per_check_for_agent(db, agent.id, cutoff_time)

    @staticmethod
    async def get_check_history(
        db: AsyncSession,
        check_id: UUID,
        hours: int,
        limit: int,
    ) -> Sequence[CheckResult]:
        """
        Get historical check results.

        Args:
            db: Database session
            check_id: Check UUID
            hours: Hours of history to retrieve
            limit: Maximum number of results (typically from API query parameter)

        Returns:
            List of check results ordered by timestamp desc
        """
        check = await CheckCoreService.get_check_by_id(db, check_id)

        cutoff_time = utc_now() - timedelta(hours=hours)
        return await CheckResultCRUD.get_history_for_check(db, check.id, cutoff_time, limit)

    @staticmethod
    async def get_check_summary(
        db: AsyncSession,
        check_id: UUID,
        hours: int = 24,
    ) -> CheckSummary:
        """
        Get summary statistics for a check.

        Args:
            db: Database session
            check_id: Check UUID
            hours: Hours to include in summary

        Returns:
            Check summary statistics
        """
        check = await CheckCoreService.get_check_by_id(db, check_id)

        cutoff_time = utc_now() - timedelta(hours=hours)

        row = await CheckResultCRUD.get_summary_stats_for_check(db, check.id, cutoff_time)

        total_checks = row.total_checks or 0
        successful_checks = int(row.successful_checks or 0)
        failed_checks = total_checks - successful_checks
        success_rate = (successful_checks / total_checks * 100) if total_checks > 0 else 0.0

        p50_latency_ms = None
        p95_latency_ms = None
        p99_latency_ms = None

        if total_checks > 0 and row.avg_latency_ms:
            percentile_row = await CheckResultCRUD.get_latency_percentiles_for_check(
                db, check.id, cutoff_time
            )
            if percentile_row:
                p50_latency_ms = percentile_row.p50
                p95_latency_ms = percentile_row.p95
                p99_latency_ms = percentile_row.p99

        return CheckSummary(
            total_checks=total_checks,
            successful_checks=successful_checks,
            failed_checks=failed_checks,
            success_rate=round(success_rate, 2),
            avg_latency_ms=round(row.avg_latency_ms, 2) if row.avg_latency_ms else None,
            min_latency_ms=round(row.min_latency_ms, 2) if row.min_latency_ms else None,
            max_latency_ms=round(row.max_latency_ms, 2) if row.max_latency_ms else None,
            p50_latency_ms=round(p50_latency_ms, 2) if p50_latency_ms else None,
            p95_latency_ms=round(p95_latency_ms, 2) if p95_latency_ms else None,
            p99_latency_ms=round(p99_latency_ms, 2) if p99_latency_ms else None,
        )

    @staticmethod
    async def process_agent_report(
        db: AsyncSession,
        report: AgentReportRequest,
    ) -> dict[str, Any]:
        """
        Process a report from an agent containing multiple check results.

        Args:
            db: Database session
            report: Agent report with check results

        Returns:
            Processing summary
        """
        agent_id = report.agent_id
        timestamp = report.timestamp or utc_now()

        # Get agent
        agent = await AgentCoreService.get_agent_by_id(db, agent_id)
        if not agent:
            raise AgentNotFoundException(str(agent_id))

        logger.info(
            "Processing report from agent",
            extra={
                "agent_name": agent.agent_name,
                "agent_id": str(agent_id),
                "check_count": len(report.checks),
            },
        )

        # Update agent's last_seen
        agent = await AgentCoreService.update_agent_last_seen(db, agent.id, report.agent_run_id)

        results_processed = 0
        results_failed = 0
        check_display_names = []

        # Batch process: collect all check definitions and results

        rows_to_insert: list[dict[str, Any]] = []

        # Get all existing checks for this agent in one query
        existing_checks = await CheckCoreService.list_checks_for_agent(db, agent.id)
        check_map = {c.id: c for c in existing_checks}  # Map by UUID

        # Process each check result
        for check_data in report.checks:
            try:
                # Get check identification (UUID-based)
                check_id = check_data.check_id if check_data.check_id else None
                display_name = check_data.display_name or "unknown"
                check_type = check_data.check_type or "unknown"

                # Get check from database by ID
                if check_id and check_id in check_map:
                    check = check_map[check_id]
                else:
                    # Internal/system checks (like agent health) should NOT be sent via check results
                    # They should use the dedicated heartbeat endpoint instead
                    if check_type == "internal":
                        logger.warning(
                            "Ignoring internal check from agent - "
                            "agent health metrics should be sent via /api/v1/heartbeat",
                            extra={
                                "display_name": display_name,
                                "agent_name": agent.agent_name,
                                "agent_id": str(agent_id),
                            },
                        )
                        results_failed += 1
                        continue

                    # Skip user checks that don't exist in database
                    logger.warning(
                        "Check ID not found for agent - skipping result. "
                        "Check must be created via API or UI first.",
                        extra={
                            "check_id": str(check_id),
                            "display_name": display_name,
                            "agent_name": agent.agent_name,
                            "agent_id": str(agent_id),
                        },
                    )
                    results_failed += 1
                    continue

                # Process SSL cert info if present in metrics
                metrics_dict = check_data.metrics or {}
                if "response" in metrics_dict and "ssl_certificate" in metrics_dict["response"]:
                    raw_cert = metrics_dict["response"]["ssl_certificate"]
                    computed_cert = await CheckResultCoreService.compute_ssl_cert_info(db, raw_cert)
                    metrics_dict["response"]["ssl_certificate"] = computed_cert

                # Convert metrics to JSON if present
                metrics_json = None
                if metrics_dict:
                    metrics_json = json.dumps(metrics_dict)

                # Use agent's UUID if provided, otherwise generate one (fallback)
                result_id = None
                if check_data.result_id:
                    try:
                        result_id = UUID(check_data.result_id)
                    except ValueError, AttributeError:
                        logger.warning(
                            "Invalid result_id, generating new UUID",
                            extra={"invalid_result_id": str(check_data.result_id)},
                        )
                        result_id = uuid7()
                else:
                    result_id = uuid7()

                # Prepare check result for bulk insert with all fields
                # Use check's individual timestamp, not batch timestamp
                check_timestamp = check_data.timestamp if check_data.timestamp else timestamp
                rows_to_insert.append(
                    {
                        "id": result_id,  # agent's UUID (or generated fallback)
                        "agent_id": agent.id,
                        "check_id": check.id,
                        "timestamp": check_timestamp,
                        "success": check_data.success,
                        "latency_ms": check_data.latency_ms,
                        "error": check_data.error,
                        "error_type": (
                            check_data.error_type if hasattr(check_data, "error_type") else None
                        ),
                        "http_status_code": (
                            check_data.http_status_code
                            if hasattr(check_data, "http_status_code")
                            else None
                        ),
                        "http_response_time_ms": (
                            check_data.http_response_time_ms
                            if hasattr(check_data, "http_response_time_ms")
                            else None
                        ),
                        "metrics": metrics_json,
                        "response_data": (
                            check_data.response_data
                            if hasattr(check_data, "response_data")
                            else None
                        ),
                    }
                )

                results_processed += 1
                check_display_names.append(display_name)

            except Exception:
                logger.error("Error processing check result", exc_info=True)
                results_failed += 1

        # Bulk insert. ON CONFLICT DO NOTHING on the (check_id, timestamp) unique
        # index makes agent retries no-ops at the DB level — no catch, no rollback,
        # get_db() owns the commit. Returns only the rows actually inserted; those
        # are the ones we post-process for metrics + alerts (a retried duplicate
        # must not re-fire either). (TimescaleDB >= 2.11 inserts into compressed
        # chunks transparently, so late data no longer rejects.)
        inserted_results: list[CheckResult] = []
        if rows_to_insert:
            inserted_results = await CheckResultCRUD.bulk_insert_idempotent(db, rows_to_insert)

        logger.info(
            "Processed report from agent",
            extra={
                "agent_name": agent.agent_name,
                "agent_id": str(agent_id),
                "results_processed": results_processed,
                "total_checks": len(report.checks),
            },
        )

        # Update Prometheus metrics in-memory (after commit, instant update on ingestion)

        for check_result in inserted_results:
            try:
                check = check_map[check_result.check_id]
                MetricsCollectorCoreService.update_check_result(check_result, check, agent)
            except Exception:
                logger.error("Error updating Prometheus metrics", exc_info=True)

        # Evaluate alerts for each check result (after commit)
        # This happens asynchronously - errors are logged but don't fail the report processing
        alerts_evaluated = 0
        for check_result in inserted_results:
            try:
                await AlertCoreService.evaluate_check_result(db, check_result)
                alerts_evaluated += 1
            except Exception:
                logger.error(
                    "Error evaluating alerts for check result",
                    extra={"check_result_id": str(check_result.id)},
                    exc_info=True,
                )

        if alerts_evaluated > 0:
            logger.info(
                "Evaluated alerts for check results",
                extra={"alerts_evaluated": alerts_evaluated},
            )

        return {
            "status": "ok",
            "agent_id": agent_id,
            "received_at": timestamp,
            "results_processed": results_processed,
            "results_failed": results_failed,
            "check_display_names": check_display_names,
            "alerts_evaluated": alerts_evaluated,
        }

    @staticmethod
    async def get_aggregated_stats(
        db: AsyncSession,
        hours: int = 24,
    ) -> dict[str, Any]:
        """
        Get aggregated statistics across all agents and checks.

        Args:
            db: Database session
            hours: Hours to include in aggregation

        Returns:
            Aggregated statistics
        """
        cutoff_time = utc_now() - timedelta(hours=hours)

        row = await CheckResultCRUD.get_overall_stats(db, cutoff_time)

        total_checks = row.total_checks or 0
        successful_checks = int(row.successful_checks or 0)
        failed_checks = total_checks - successful_checks
        success_rate = (successful_checks / total_checks * 100) if total_checks > 0 else 0.0

        active_agents = await CheckResultCRUD.count_active_agents_since(db, cutoff_time)
        active_checks = await CheckResultCRUD.count_active_checks_since(db, cutoff_time)

        return {
            "total_checks": total_checks,
            "successful_checks": successful_checks,
            "failed_checks": failed_checks,
            "success_rate": round(success_rate, 2),
            "avg_latency_ms": (round(row.avg_latency_ms, 2) if row.avg_latency_ms else None),
            "active_agents": active_agents,
            "active_checks": active_checks,
            "time_window_hours": hours,
        }

    @staticmethod
    async def cleanup_old_results(
        db: AsyncSession,
        days: int = 90,
    ) -> int:
        """
        Clean up old check results beyond retention period.

        Args:
            db: Database session
            days: Delete results older than this many days

        Returns:
            Number of records deleted
        """
        cutoff_time = utc_now() - timedelta(days=days)
        deleted_count = await CheckResultCRUD.delete_older_than(db, cutoff_time)
        logger.info(
            "Cleaned up old check results",
            extra={"deleted_count": deleted_count, "older_than_days": days},
        )
        return deleted_count

    # ------------------------------------------------------------------
    # CRUD delegation methods (used by view services)
    # ------------------------------------------------------------------

    @staticmethod
    async def get_checks_with_agents_filtered(db, **kwargs):
        """Get filtered checks with agents. Delegates to CRUD."""
        return await CheckResultCRUD.get_checks_with_agents_filtered(db, **kwargs)

    @staticmethod
    async def get_latest_results_batch(db, check_ids, cutoff_minutes=32):
        """Batch query latest results for checks. Delegates to CRUD."""
        return await CheckResultCRUD.get_latest_results_batch(db, check_ids, cutoff_minutes)

    @staticmethod
    async def get_24h_stats_batch(db, check_ids):
        """Batch query 24h stats for checks. Delegates to CRUD."""
        return await CheckResultCRUD.get_24h_stats_batch(db, check_ids)

    @staticmethod
    async def get_minute_bars_results(db, check_ids, minutes=15):
        """Get results for minute bars. Delegates to CRUD."""
        return await CheckResultCRUD.get_minute_bars_results(db, check_ids, minutes)

    @staticmethod
    async def get_status_summary_data(db):
        """Get status summary data. Delegates to CRUD."""
        return await CheckResultCRUD.get_status_summary_data(db)

    @staticmethod
    async def get_check_with_agent(db, check_id):
        """Get check with agent. Delegates to CRUD."""
        return await CheckResultCRUD.get_check_with_agent(db, check_id)

    @staticmethod
    async def get_bucketed_history(db, check_id, hours):
        """Get time-bucketed history. Delegates to CRUD."""
        return await CheckResultCRUD.get_bucketed_history(db, check_id, hours)

    @staticmethod
    async def get_raw_results_for_status_bar(db, check_id, minutes=30):
        """Get raw results for status bar. Delegates to CRUD."""
        return await CheckResultCRUD.get_raw_results_for_status_bar(db, check_id, minutes)

    @staticmethod
    async def get_check_detail_stats(db, check_id, hours):
        """Get check detail stats. Delegates to CRUD."""
        return await CheckResultCRUD.get_check_detail_stats(db, check_id, hours)

    @staticmethod
    async def get_latest_result_for_check(db, check_id):
        """Get latest result for a check. Delegates to CRUD."""
        return await CheckResultCRUD.get_latest_result_for_check(db, check_id)

    @staticmethod
    async def get_artifacts_for_result(db, check_result_id, check_result_timestamp):
        """Get artifacts for a check result. Delegates to CRUD."""
        return await CheckResultCRUD.get_artifacts_for_result(
            db, check_result_id, check_result_timestamp
        )

    @staticmethod
    async def get_check_result_by_id(db, check_result_id):
        """Get a single check result by id. Delegates to CRUD."""
        return await CheckResultCRUD.get_check_result_by_id(db, check_result_id)

    @staticmethod
    async def get_uptime_stats_bulk(db, check_ids, hours=168):
        """Bulk uptime stats. Delegates to CRUD."""
        return await CheckResultCRUD.get_uptime_stats_bulk(db, check_ids, hours)

    @staticmethod
    async def get_last_failure_timestamps_bulk(db, check_ids, hours=168):
        """Bulk last failure timestamps. Delegates to CRUD."""
        return await CheckResultCRUD.get_last_failure_timestamps_bulk(db, check_ids, hours)

    @staticmethod
    async def get_results_for_status_bars_bulk(db, check_ids, minutes=32):
        """Bulk results for status bars. Delegates to CRUD."""
        return await CheckResultCRUD.get_results_for_status_bars_bulk(db, check_ids, minutes)

    @staticmethod
    async def get_bucketed_status_bars_bulk(db, check_ids, minutes=32, bucket_minutes=1):
        """Bucketed/aggregated status bars for larger time ranges. Delegates to CRUD."""
        return await CheckResultCRUD.get_bucketed_status_bars_bulk(
            db, check_ids, minutes, bucket_minutes
        )
