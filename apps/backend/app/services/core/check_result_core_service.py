"""
CheckResult service - business logic for check result operations and analytics.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import Row
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_extensions import uuid7

from app.core.datetime_utils import utc_now
from app.core.exceptions import AgentNotFoundException
from app.core.ssl_expiry import ssl_expiry_fields
from app.crud.check_crud import CheckCRUD
from app.crud.check_result_crud import (
    AggregateRow,
    CheckResultCRUD,
    ResultCounts,
    UptimeCounts,
)
from app.models.check_result_model import CheckResult
from app.schemas.agent_schema import AgentOption
from app.schemas.check_artifact_schema import CheckArtifactResponse
from app.schemas.check_result_schema import (
    AgentReportRequest,
    AgentReportSubmission,
    CheckResultCreate,
    RecentCheckResult,
    StatusBarResult,
    metrics_json,
)
from app.schemas.check_schema import CheckStatusInfo
from app.services.core.agent_assignment_core_service import AgentAssignmentCoreService
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.alert_decision_core_service import AlertDecisionCoreService
from app.services.core.metrics_collector_core_service import MetricsCollectorCoreService
from app.services.core.settings_core_service import SettingsCoreService
from app.shared.logger import get_logger

logger = get_logger("luxswirl.services.check_result")

# How far past the server's clock a result's timestamp may be. A result's
# timestamp is the agent's clock, and every reader of a check's status bounds
# results from below only (the newest result is the current one): a result
# stamped later than this would stand as the check's status, whatever is
# reported after it, until the clock caught up. Wide enough for an agent clock
# a little fast.
RESULT_CLOCK_TOLERANCE = timedelta(minutes=5)


def _later_than(when: datetime, bound: datetime) -> bool:
    """Whether `when` is after `bound`; a timestamp with no zone is UTC, as
    the database stores it."""
    return (when if when.tzinfo else when.replace(tzinfo=UTC)) > bound


class CheckResultCoreService:
    """Service for check result operations and analytics."""

    @staticmethod
    async def compute_ssl_cert_info(
        db: AsyncSession, raw_cert_data: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Compute SSL certificate expiration info from raw cert data.

        Args:
            db: Database session for fetching settings
            raw_cert_data: Raw cert data from check (expiration_date, valid_from, issuer, subject)

        Returns:
            Dictionary with computed cert info including days_until_expiration, expired, expires_soon
        """
        # The thresholds are operator-configurable; they live under the
        # `alert.` prefix, which this lookup used to omit — so a configured
        # warning/critical window was silently ignored in favour of the default.
        warning_days = await SettingsCoreService.get_setting(
            db, "alert.ssl_cert_warning_days", default=30
        )
        critical_days = await SettingsCoreService.get_setting(
            db, "alert.ssl_cert_critical_days", default=14
        )
        return {
            **raw_cert_data,
            **ssl_expiry_fields(
                raw_cert_data.get("expiration_date"),
                warning_days=warning_days,
                critical_days=critical_days,
            ),
        }

    @staticmethod
    def validate_results(
        submission: AgentReportSubmission,
    ) -> tuple[list[CheckResultCreate], int]:
        """Validate each result of a submission on its own.

        A result the schema refuses (an HTTP status outside 100-599, an error
        text over its limit, a missing field) is logged and counted, the same
        way a result for an unknown check is; the others are kept. The log line
        names the result and the failing fields, never the submitted values.

        Returns:
            The valid results, and how many were refused.
        """
        valid: list[CheckResultCreate] = []
        refused = 0
        for raw in submission.checks:
            try:
                valid.append(CheckResultCreate.model_validate(raw))
            except ValidationError as exc:
                refused += 1
                logger.warning(
                    "Invalid check result in report - skipping result",
                    extra={
                        "check_id": str(raw.get("check_id"))[:64],
                        "display_name": str(raw.get("display_name"))[:255],
                        "agent_id": str(submission.agent_id),
                        "invalid_fields": [
                            {
                                "field": ".".join(str(part) for part in error["loc"]),
                                "error": error["type"],
                            }
                            for error in exc.errors(include_input=False)
                        ],
                    },
                )
        return valid, refused

    @staticmethod
    async def process_agent_submission(
        db: AsyncSession,
        submission: AgentReportSubmission,
        *,
        verified_key_hash: str | None,
    ) -> dict[str, Any]:
        """Validate a report's results one by one, then process the valid ones.

        The refused results are added to `results_failed`.
        `verified_key_hash` is as `process_agent_report` takes it.
        """
        valid, refused = CheckResultCoreService.validate_results(submission)
        report = AgentReportRequest(
            agent_id=submission.agent_id,
            agent_run_id=submission.agent_run_id,
            timestamp=submission.timestamp,
            checks=valid,
        )
        result = await CheckResultCoreService.process_agent_report(
            db, report, verified_key_hash=verified_key_hash
        )
        result["results_failed"] += refused
        return result

    @staticmethod
    async def process_agent_report(
        db: AsyncSession,
        report: AgentReportRequest,
        *,
        verified_key_hash: str | None,
    ) -> dict[str, Any]:
        """
        Process a report from an agent containing multiple check results.

        A result timestamped later than the server's clock plus
        `RESULT_CLOCK_TOLERANCE` is counted in `results_failed` and not stored.

        Args:
            db: Database session
            report: Agent report with check results
            verified_key_hash: The key hash the report's key was checked
                against (None for an agent that had none): before anything is
                stored the agent is judged again on its row as stored now
                (`AgentCoreService.require_still_admitted`).

        No alert is decided here: the key of each stored result an enabled
        alert evaluates is queued in this transaction
        (`AlertDecisionCoreService.queue_report_results`), and the alert
        decision worker decides it once this commits. The commit is all the
        agent's answer waits on.

        Returns:
            Processing summary

        Raises:
            AgentNotFoundException: The agent does not exist, or was deleted
                while the report was in flight.
            AuthenticationException: The agent's key changed meanwhile.
            AuthorizationException: The agent left active meanwhile.
        """
        agent_id = report.agent_id
        timestamp = report.timestamp or utc_now()
        latest_allowed = utc_now() + RESULT_CLOCK_TOLERANCE

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

        # No write to the agent row here: a row write at the start of this
        # transaction would hold that lock through the whole insert, timing out
        # the agent's own heartbeat behind it. The heartbeat owns last_seen and
        # the run id.

        results_failed = 0
        check_display_names: list[str] = []

        # Batch process: collect all check definitions and results

        rows_to_insert: list[dict[str, Any]] = []
        # Each row's display name, in the same order.
        row_display_names: list[str] = []

        # The checks this agent may report: its manual checks, the replicate
        # checks it is selected for, and the distribute checks whose pool it is in.
        assigned_checks = await AgentAssignmentCoreService.get_reportable_checks(
            db, agent
        )
        check_map = {c.id: c for c in assigned_checks}  # Map by UUID

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
                            "agent health metrics should be sent via /api/v1/agent/heartbeat",
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

                # Use check's individual timestamp, not batch timestamp
                check_timestamp = (
                    check_data.timestamp if check_data.timestamp else timestamp
                )
                if _later_than(check_timestamp, latest_allowed):
                    logger.warning(
                        "Check result timestamped in the future - skipping result",
                        extra={
                            "check_id": str(check_id),
                            "display_name": display_name,
                            "agent_name": agent.agent_name,
                            "agent_id": str(agent_id),
                            "result_timestamp": check_timestamp.isoformat(),
                        },
                    )
                    results_failed += 1
                    continue

                # Process SSL cert info if present in metrics
                metrics_dict = check_data.metrics or {}
                if (
                    "response" in metrics_dict
                    and "ssl_certificate" in metrics_dict["response"]
                ):
                    raw_cert = metrics_dict["response"]["ssl_certificate"]
                    computed_cert = await CheckResultCoreService.compute_ssl_cert_info(
                        db, raw_cert
                    )
                    metrics_dict["response"]["ssl_certificate"] = computed_cert

                # Convert metrics to JSON if present: the form the schema's
                # metrics limit measured.
                stored_metrics = None
                if metrics_dict:
                    stored_metrics = metrics_json(metrics_dict)

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
                            check_data.error_type
                            if hasattr(check_data, "error_type")
                            else None
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
                        "metrics": stored_metrics,
                        "response_data": (
                            check_data.response_data
                            if hasattr(check_data, "response_data")
                            else None
                        ),
                    }
                )

                row_display_names.append(display_name)

            except Exception:
                logger.exception("Error processing check result")
                results_failed += 1

        # Bulk insert. ON CONFLICT DO NOTHING on the (check_id, timestamp) unique
        # index makes agent retries no-ops at the DB level — no catch, no rollback,
        # get_db() owns the commit. Returns only the rows actually inserted; those
        # are the ones the metrics are updated for (every kept row is queued for its
        # alert decisions, below). (TimescaleDB >= 2.11 inserts into compressed
        # chunks transparently, so late data no longer rejects.)
        # The checks the rows reference, then the agent, locked as the insert's
        # own foreign key checks lock them and in the order an agent delete
        # removes them: a check deleted after the report read it costs its own
        # results, never the report; an agent deleted meanwhile is not found;
        # and a delete arriving now waits for this report. The agent's lock is
        # also where it is judged again: a key revoked or regenerated, or the
        # agent taken out of active, since its key was checked refuses the
        # report, and neither can land after it before this report commits.
        present = await CheckCRUD.key_share_existing(
            db, [row["check_id"] for row in rows_to_insert]
        )
        if rows_to_insert:
            await AgentCoreService.require_still_admitted(
                db, agent.id, verified_key_hash
            )
        kept_rows: list[dict[str, Any]] = []
        for row, name in zip(rows_to_insert, row_display_names, strict=True):
            if row["check_id"] in present:
                kept_rows.append(row)
                check_display_names.append(name)
        dropped = len(rows_to_insert) - len(kept_rows)
        if dropped:
            results_failed += dropped
            logger.warning(
                "Results for checks deleted while the report was in flight - skipping them",
                extra={
                    "results_skipped": dropped,
                    "agent_name": agent.agent_name,
                    "agent_id": str(agent_id),
                },
            )
        results_processed = len(kept_rows)

        inserted_results: list[CheckResult] = []
        if kept_rows:
            inserted_results = await CheckResultCRUD.bulk_insert_idempotent(
                db, kept_rows
            )

        logger.info(
            "Processed report from agent",
            extra={
                "agent_name": agent.agent_name,
                "agent_id": str(agent_id),
                "results_processed": results_processed,
                "total_checks": len(report.checks),
            },
        )

        # Update the in-memory Prometheus metrics: an instant update on ingestion.

        for check_result in inserted_results:
            try:
                check = check_map[check_result.check_id]
                MetricsCollectorCoreService.update_check_result(
                    check_result, check, agent
                )
            # swallowed-exceptions: per-result metrics update — the results are
            # stored, so one failed gauge must not drop the rest.
            except Exception:
                logger.exception("Error updating Prometheus metrics")

        # The results' alerts are queued for the alert decision worker, never
        # decided here: the report holds the agent's row and its checks only
        # for its reads and inserts. A result already stored (the agent sending
        # again a report the server stored but never answered) is queued too:
        # its alerts may never have been decided, and the check's decision
        # cursor drops it if they were.
        await AlertDecisionCoreService.queue_report_results(
            db, [(row["check_id"], row["timestamp"]) for row in kept_rows]
        )

        return {
            "status": "ok",
            "agent_id": agent_id,
            "received_at": timestamp,
            "results_processed": results_processed,
            "results_failed": results_failed,
            "check_display_names": check_display_names,
        }

    # ------------------------------------------------------------------
    # CRUD delegation methods (used by view services)
    # ------------------------------------------------------------------

    @staticmethod
    async def get_checks_with_agents_filtered(
        db: AsyncSession,
        agent_id: UUID | None = None,
        check_type: str | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        search: str | None = None,
        check_ids: list[UUID] | None = None,
        *,
        skip: int = 0,
        limit: int = 1000,
    ) -> tuple[list[tuple[CheckStatusInfo, AgentOption]], int]:
        """Filtered checks, each with its agent, and the total that match."""
        rows, total = await CheckResultCRUD.get_checks_with_agents_filtered(
            db,
            agent_id=agent_id,
            check_type=check_type,
            status=status,
            tags=tags,
            search=search,
            check_ids=check_ids,
            skip=skip,
            limit=limit,
        )
        return [
            (CheckStatusInfo.model_validate(check), AgentOption.model_validate(agent))
            for check, agent in rows
        ], total

    @staticmethod
    async def get_latest_results_batch(
        db: AsyncSession,
        check_ids: list[UUID],
        cutoff_minutes: int = 32,
    ) -> dict[UUID, AggregateRow]:
        """Batch query latest results for checks. Delegates to CRUD."""
        return await CheckResultCRUD.get_latest_results_batch(
            db, check_ids, cutoff_minutes
        )

    @staticmethod
    async def get_24h_stats_batch(
        db: AsyncSession,
        check_ids: list[UUID],
    ) -> dict[UUID, ResultCounts]:
        """Batch query 24h stats for checks. Delegates to CRUD."""
        return await CheckResultCRUD.get_24h_stats_batch(db, check_ids)

    @staticmethod
    async def get_current_results_batch(
        db: AsyncSession,
        check_ids: list[UUID],
    ) -> dict[UUID, AggregateRow]:
        """Each check's current result, as its health is read. Delegates to CRUD."""
        return await CheckResultCRUD.get_current_results_batch(db, check_ids)

    @staticmethod
    async def get_minute_bars_results(
        db: AsyncSession,
        check_ids: list[UUID],
        minutes: int = 15,
    ) -> list[StatusBarResult]:
        """Results for the minute bars: what a bar reads, per result."""
        return [
            StatusBarResult.model_validate(result)
            for result in await CheckResultCRUD.get_minute_bars_results(
                db, check_ids, minutes
            )
        ]

    @staticmethod
    async def get_status_summary_data(
        db: AsyncSession,
    ) -> dict[str, Any]:
        """Get status summary data. Delegates to CRUD."""
        return await CheckResultCRUD.get_status_summary_data(db)

    @staticmethod
    async def get_check_with_agent(
        db: AsyncSession,
        check_id: UUID,
    ) -> tuple[CheckStatusInfo, AgentOption] | None:
        """The check and its agent, or None if there is no such check."""
        row = await CheckResultCRUD.get_check_with_agent(db, check_id)
        if row is None:
            return None
        check, agent = row
        return CheckStatusInfo.model_validate(check), AgentOption.model_validate(agent)

    @staticmethod
    async def get_bucketed_history(
        db: AsyncSession,
        check_id: UUID,
        hours: int,
    ) -> list[AggregateRow]:
        """Get time-bucketed history. Delegates to CRUD."""
        return await CheckResultCRUD.get_bucketed_history(db, check_id, hours)

    @staticmethod
    async def get_raw_results_for_status_bar(
        db: AsyncSession,
        check_id: UUID,
        minutes: int = 30,
    ) -> list[RecentCheckResult]:
        """The check's results over the last `minutes`, for its status bar."""
        return [
            RecentCheckResult.model_validate(result)
            for result in await CheckResultCRUD.get_raw_results_for_status_bar(
                db, check_id, minutes
            )
        ]

    @staticmethod
    async def get_check_detail_stats(
        db: AsyncSession,
        check_id: UUID,
        hours: int,
    ) -> AggregateRow:
        """(total, successful, avg_latency, min_latency, max_latency) over `hours`."""
        return await CheckResultCRUD.get_check_detail_stats(db, check_id, hours)

    @staticmethod
    async def get_latest_result_for_check(
        db: AsyncSession,
        check_id: UUID,
    ) -> RecentCheckResult | None:
        """The check's most recent result, or None if it has none."""
        result = await CheckResultCRUD.get_latest_result_for_check(db, check_id)
        return None if result is None else RecentCheckResult.model_validate(result)

    @staticmethod
    async def count_deleted_with_agent(db: AsyncSession, agent_id: UUID) -> int:
        """How many stored results deleting the agent removes."""
        return await CheckResultCRUD.count_deleted_with_agent(db, agent_id)

    @staticmethod
    async def get_artifacts_for_result(
        db: AsyncSession,
        check_id: UUID,
        check_result_id: UUID,
        check_result_timestamp: datetime,
    ) -> list[CheckArtifactResponse]:
        """The artifacts one execution of `check_id` produced, metadata only.

        Scoped to the check as well as the result id: the result id is the
        agent's to choose, so an artifact another check's agent uploaded naming
        this result never shows here.
        """
        return [
            CheckArtifactResponse.model_validate(artifact)
            for artifact in await CheckResultCRUD.get_artifacts_for_result(
                db, check_id, check_result_id, check_result_timestamp
            )
        ]

    @staticmethod
    async def list_results_page(
        db: AsyncSession,
        check_id: UUID,
        since: datetime,
        until: datetime,
        *,
        skip: int,
        limit: int,
    ) -> tuple[list[CheckResult], int]:
        """One page of the check's results in [since, until], newest first, and
        the window's count. Delegates to CRUD."""
        return await CheckResultCRUD.list_page_for_check(
            db, check_id, since, until, skip=skip, limit=limit
        )

    @staticmethod
    async def get_result_of_check(
        db: AsyncSession, check_id: UUID, check_result_id: UUID
    ) -> CheckResult | None:
        """One of the check's results by id, or None. Delegates to CRUD."""
        return await CheckResultCRUD.get_result_of_check(db, check_id, check_result_id)

    @staticmethod
    async def get_latest_result_row(
        db: AsyncSession, check_id: UUID
    ) -> CheckResult | None:
        """The check's most recent result, whole. Delegates to CRUD."""
        return await CheckResultCRUD.get_latest_result_for_check(db, check_id)

    @staticmethod
    async def get_check_result_by_id(
        db: AsyncSession,
        check_result_id: UUID,
    ) -> CheckResult | None:
        """Get a single check result by id. Delegates to CRUD."""
        return await CheckResultCRUD.get_check_result_by_id(db, check_result_id)

    @staticmethod
    async def get_uptime_counts_bulk(
        db: AsyncSession,
        check_ids: Sequence[UUID],
        windows_hours: Sequence[int],
    ) -> UptimeCounts:
        """Each check's (results, successful) in each window. Delegates to CRUD."""
        return await CheckResultCRUD.get_uptime_counts_bulk(
            db, check_ids, windows_hours
        )

    @staticmethod
    async def get_last_failure_timestamps_bulk(
        db: AsyncSession,
        check_ids: list[UUID],
        hours: int = 168,
    ) -> list[Row[UUID, datetime]]:
        """Bulk last failure timestamps. Delegates to CRUD."""
        return await CheckResultCRUD.get_last_failure_timestamps_bulk(
            db, check_ids, hours
        )

    @staticmethod
    async def get_results_for_status_bars_bulk(
        db: AsyncSession,
        check_ids: list[UUID],
        minutes: int = 32,
    ) -> list[StatusBarResult]:
        """Bulk results for status bars, newest first within each check."""
        return [
            StatusBarResult(
                check_id=check_id,
                timestamp=timestamp,
                success=success,
                latency_ms=latency_ms,
                http_status_code=http_status_code,
            )
            for (
                check_id,
                timestamp,
                success,
                latency_ms,
                http_status_code,
            ) in await CheckResultCRUD.get_results_for_status_bars_bulk(
                db, check_ids, minutes
            )
        ]

    @staticmethod
    async def get_bucketed_status_bars_bulk(
        db: AsyncSession,
        check_ids: list[UUID],
        minutes: int = 32,
        bucket_minutes: int = 1,
    ) -> list[AggregateRow]:
        """Bucketed/aggregated status bars for larger time ranges. Delegates to CRUD."""
        return await CheckResultCRUD.get_bucketed_status_bars_bulk(
            db, check_ids, minutes, bucket_minutes
        )
