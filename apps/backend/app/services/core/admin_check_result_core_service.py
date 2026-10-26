"""
Admin check result core service - a check's results as the admin API serves them.

Each method takes the caller's role and first proves the check exists (404).
A viewer reads every `error` with its credentials masked, as a check's target
is, and never a result's `response_data`: the web UI never shows it, and a
response body can hold anything the monitored service sent.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.credential_mask import mask_credentials
from app.core.datetime_utils import utc_now
from app.core.exceptions import NotFoundException, ValidationException
from app.models.check_result_model import CheckResult
from app.models.enum_model import UserRole
from app.schemas.admin_check_result_schema import (
    AdminCheckResult,
    AdminCheckResultDetail,
    CheckHistory,
    CheckHistoryPoint,
    CheckResultPage,
    CheckSummary,
)
from app.schemas.pagination_schema import build_pagination
from app.services.core.check_core_service import CheckCoreService
from app.services.core.check_result_core_service import CheckResultCoreService

# The window a results page reads when none is given, and the widest allowed.
DEFAULT_WINDOW = timedelta(hours=24)
MAX_WINDOW = timedelta(days=31)


def _metrics(raw: str | None) -> dict[str, Any] | None:
    """The stored JSON text as a dict; anything else reads as none."""
    if not raw:
        return None
    try:
        decoded = json.loads(raw)
    except ValueError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _utc(moment: datetime) -> datetime:
    """`moment` as an aware UTC time; a naive one is taken to be UTC."""
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


class AdminCheckResultCoreService:
    """Read a check's results, one result, its history and its summary."""

    @staticmethod
    def _error(error: str | None, role: UserRole) -> str | None:
        return mask_credentials(error) if role == UserRole.VIEWER else error

    @staticmethod
    def _result(row: CheckResult, role: UserRole) -> AdminCheckResult:
        return AdminCheckResult(
            id=row.id,
            check_id=row.check_id,
            agent_id=row.agent_id,
            timestamp=row.timestamp,
            success=row.success,
            latency_ms=row.latency_ms,
            error=AdminCheckResultCoreService._error(row.error, role),
            error_type=row.error_type,
            http_status_code=row.http_status_code,
            http_response_time_ms=row.http_response_time_ms,
            metrics=_metrics(row.metrics),
        )

    @staticmethod
    async def list_results(
        db: AsyncSession,
        role: UserRole,
        check_id: UUID,
        *,
        since: datetime | None,
        until: datetime | None,
        page: int,
        per_page: int,
    ) -> CheckResultPage:
        """One page of the check's results in [since, until], newest first.

        `until` defaults to now and `since` to 24 hours before it; the window
        must run forward and span at most 31 days.
        """
        await CheckCoreService.get_check_by_id(db, check_id)
        end = _utc(until) if until is not None else utc_now()
        start = _utc(since) if since is not None else end - DEFAULT_WINDOW
        if start >= end:
            raise ValidationException("`since` must be before `until`")
        if end - start > MAX_WINDOW:
            raise ValidationException("The window spans at most 31 days")
        page = max(1, page)
        rows, total = await CheckResultCoreService.list_results_page(
            db, check_id, start, end, limit=per_page, offset=(page - 1) * per_page
        )
        return CheckResultPage.of(
            [AdminCheckResultCoreService._result(row, role) for row in rows],
            build_pagination(page=page, per_page=per_page, total=total),
        )

    @staticmethod
    async def get_result(
        db: AsyncSession, role: UserRole, check_id: UUID, result_id: UUID
    ) -> AdminCheckResultDetail:
        """One result of the check, whole; one of another check is not found."""
        await CheckCoreService.get_check_by_id(db, check_id)
        row = await CheckResultCoreService.get_check_result_by_id(db, result_id)
        if row is None or row.check_id != check_id:
            raise NotFoundException(f"Check {check_id} has no result {result_id}")
        artifacts = await CheckResultCoreService.get_artifacts_for_result(
            db, check_id, row.id, row.timestamp
        )
        return AdminCheckResultDetail(
            **AdminCheckResultCoreService._result(row, role).model_dump(),
            response_data=None if role == UserRole.VIEWER else row.response_data,
            artifacts=artifacts,
        )

    @staticmethod
    async def history(
        db: AsyncSession, role: UserRole, check_id: UUID, hours: int
    ) -> CheckHistory:
        """The check's results over the last `hours`, bucketed as the panel draws them."""
        await CheckCoreService.get_check_by_id(db, check_id)
        rows = await CheckResultCoreService.get_bucketed_history(db, check_id, hours)
        return CheckHistory(
            check_id=check_id,
            hours=hours,
            points=[
                CheckHistoryPoint(
                    bucket_time=row.bucket_time,
                    success=row.success,
                    latency_ms=row.latency_ms,
                    error=AdminCheckResultCoreService._error(row.error, role),
                )
                for row in rows
            ],
        )

    @staticmethod
    async def summary(
        db: AsyncSession, role: UserRole, check_id: UUID, hours: int
    ) -> CheckSummary:
        """The check's results over the last `hours`, counted, and its latest."""
        await CheckCoreService.get_check_by_id(db, check_id)
        stats = await CheckResultCoreService.get_check_detail_stats(db, check_id, hours)
        latest = await CheckResultCoreService.get_latest_result_row(db, check_id)
        total = int(stats.total or 0)
        successful = int(stats.successful or 0)
        return CheckSummary(
            check_id=check_id,
            hours=hours,
            total=total,
            successful=successful,
            uptime_percent=round(successful / total * 100, 3) if total else None,
            avg_latency_ms=stats.avg_latency,
            min_latency_ms=stats.min_latency,
            max_latency_ms=stats.max_latency,
            latest=(
                AdminCheckResultCoreService._result(latest, role) if latest else None
            ),
        )
