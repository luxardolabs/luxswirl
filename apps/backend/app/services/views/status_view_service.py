"""
Status service - aggregates data for status view.

This service provides data specifically formatted for web UI consumption,
combining agent, check, and result data.
"""

from collections import defaultdict
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import Request
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.datetime_utils import utc_now
from app.core.query_params import split_csv
from app.models.agent_model import Agent
from app.models.user_model import User
from app.schemas.pagination_schema import build_pagination
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.check_core_service import CheckCoreService
from app.services.core.check_result_core_service import CheckResultCoreService
from app.services.core.settings_core_service import SettingsCoreService

logger = get_logger("luxswirl.web.services.status")


class StatusRow:
    """Represents a single check status row for UI display."""

    def __init__(
        self,
        check_id: UUID,
        check_name: str,
        check_type: str,
        target: str,
        agent_name: str,
        success: bool | None,
        latency_ms: float | None,
        last_check: datetime | None,
        uptime_24h: float | None,
        enabled: bool,
        fully_qualified_name: str,
        check_tags: list[str] | None = None,
        agent_tags: list[str] | None = None,
        minute_bars: list[dict] | None = None,
        depends_on_check_id: UUID | None = None,
        dependent_count: int = 0,
    ):
        self.check_id = check_id
        self.check_name = check_name
        self.check_type = check_type
        self.target = target
        self.agent_name = agent_name
        self.success = success
        self.latency_ms = latency_ms
        self.last_check = last_check
        self.uptime_24h = uptime_24h or 0.0
        self.enabled = enabled
        self.fully_qualified_name = fully_qualified_name
        self.check_tags = check_tags or []
        self.agent_tags = agent_tags or []
        self.minute_bars = minute_bars or []
        self.depends_on_check_id = depends_on_check_id
        self.dependent_count = dependent_count

    @property
    def status_class(self) -> str:
        """Get CSS class for status badge."""
        if self.success is None:
            return "unknown"
        return "success" if self.success else "error"

    @property
    def status_text(self) -> str:
        """Get human-readable status text."""
        if self.success is None:
            return "Unknown"
        return "Up" if self.success else "Down"

    @property
    def latency_display(self) -> str:
        """Format latency for display."""
        if self.latency_ms is None:
            return "-"
        if self.latency_ms < 1:
            return f"{self.latency_ms:.2f}ms"
        elif self.latency_ms < 1000:
            return f"{int(self.latency_ms)}ms"
        else:
            return f"{self.latency_ms / 1000:.2f}s"

    @property
    def uptime_display(self) -> str:
        """Format uptime percentage for display."""
        return f"{self.uptime_24h:.1f}%"


class StatusViewService:
    """Service for status page data aggregation."""

    @staticmethod
    async def get_all_agents(db: AsyncSession) -> list[Agent]:
        """Get all admitted agents (excluding pending and rejected agents)."""
        return await AgentCoreService.get_admitted_agents(db)

    @staticmethod
    async def get_all_check_types(db: AsyncSession) -> list[str]:
        """Get all distinct check types."""
        return await CheckCoreService.get_distinct_check_types(db)

    @staticmethod
    async def get_all_tags(db: AsyncSession) -> list[str]:
        """Get all distinct tags from both checks and agents."""
        return await CheckCoreService.get_all_tags_combined(db)

    @staticmethod
    async def get_all_checks_status(
        db: AsyncSession,
        agent_id: UUID | None = None,
        check_type: str | None = None,
        status: str | None = None,  # 'up', 'down', 'unknown', 'all'
        tags: list[str] | None = None,
        search: str | None = None,
        check_ids: list[UUID] | None = None,
        limit: int = 1000,
        offset: int = 0,
        include_minute_bars: bool = False,
    ) -> tuple[list[StatusRow], int]:
        """
        Get status of all checks with filtering.

        Args:
            db: Database session
            agent_id: Filter by agent_id
            check_type: Filter by check_type
            status: Filter by health (up/down/unknown/all)
            tags: Filter by tags (overlap with check or agent tags)
            check_ids: Filter by specific check IDs (optimized bulk query)
            search: Search in check_name or target
            limit: Max results
            offset: Pagination offset
            include_minute_bars: Generate 15-minute status bars for each check

        Returns:
            Tuple of (status rows, total count)
        """
        # Get filtered checks with agents and total count from core service
        checks_and_agents, total = await CheckResultCoreService.get_checks_with_agents_filtered(
            db,
            agent_id=agent_id,
            check_type=check_type,
            status=status,
            tags=tags,
            search=search,
            check_ids=check_ids,
            limit=limit,
            offset=offset,
        )

        # Extract check IDs for batch querying
        filtered_check_ids = [check.id for check, _ in checks_and_agents]

        if not filtered_check_ids:
            return [], total

        # Batch query: Get latest results for all checks (30-minute window)
        latest_results_map = await CheckResultCoreService.get_latest_results_batch(
            db, filtered_check_ids, cutoff_minutes=32
        )

        # Batch query: Get 24h stats for all checks
        stats_map = await CheckResultCoreService.get_24h_stats_batch(db, filtered_check_ids)
        dependent_counts = await CheckCoreService.count_dependents_bulk(db, filtered_check_ids)

        # Batch query: Get 15-minute bars for all checks (if requested)
        minute_bars_map: dict[UUID, list[dict]] = {}
        if include_minute_bars:
            all_bars_results = await CheckResultCoreService.get_minute_bars_results(
                db, filtered_check_ids, minutes=15
            )

            # Group results by check_id and minute offset (data shaping in view service)
            now = utc_now()
            check_minute_buckets: dict[UUID, dict[int, list]] = defaultdict(
                lambda: defaultdict(list)
            )

            for result in all_bars_results:
                minutes_ago = int((now - result.timestamp).total_seconds() / 60)
                if 0 <= minutes_ago < 15:
                    check_minute_buckets[result.check_id][minutes_ago].append(result)

            # Build minute bars for each check
            for cid in filtered_check_ids:
                minute_buckets = check_minute_buckets.get(cid, {})
                bars = []

                for minute_offset in range(15):  # 0 to 14 minutes ago
                    if minute_offset in minute_buckets:
                        results_in_minute = minute_buckets[minute_offset]
                        all_success = all(r.success for r in results_in_minute)

                        # Calculate avg latency (filter out None values)
                        latencies = [
                            r.latency_ms for r in results_in_minute if r.latency_ms is not None
                        ]
                        avg_latency = sum(latencies) / len(latencies) if latencies else 0

                        bar = {
                            "success": all_success,
                            "count": len(results_in_minute),
                            "avg_latency": round(avg_latency, 1),
                            "minutes_ago": minute_offset,
                        }
                    else:
                        # No data for this minute
                        bar = {
                            "success": None,
                            "count": 0,
                            "avg_latency": None,
                            "minutes_ago": minute_offset,
                        }

                    bars.append(bar)

                # Reverse so oldest is first (left side of bar)
                bars.reverse()
                minute_bars_map[cid] = bars

        # Build status rows (data shaping stays in view service)
        status_rows = []

        for check, agent in checks_and_agents:
            latest_result = latest_results_map.get(check.id)
            stats = stats_map.get(check.id)

            uptime_24h = None
            if stats and stats.total and stats.total > 0:
                uptime_24h = (stats.successful / stats.total) * 100

            # Parse tags from check and agent (both are PostgreSQL arrays)
            check_tags = [tag.strip() for tag in (check.tags or []) if tag and tag.strip()]
            agent_tags = [tag.strip() for tag in (agent.tags or []) if tag and tag.strip()]

            # Get minute bars for this check (if requested)
            minute_bars = minute_bars_map.get(check.id, []) if include_minute_bars else []

            # Create status row
            status_row = StatusRow(
                check_id=check.id,
                check_name=check.display_name,
                check_type=check.check_type,
                target=check.target,
                agent_name=agent.agent_name
                or str(agent.id),  # Use agent_name for display, fallback to ID
                success=latest_result.success if latest_result else None,
                latency_ms=latest_result.latency_ms if latest_result else None,
                last_check=latest_result.timestamp if latest_result else None,
                uptime_24h=uptime_24h,
                enabled=check.enabled,
                fully_qualified_name=f"{agent.agent_name}:{check.display_name}",
                check_tags=check_tags,
                agent_tags=agent_tags,
                minute_bars=minute_bars,
                depends_on_check_id=check.depends_on_check_id,
                dependent_count=dependent_counts.get(check.id, 0),
            )

            status_rows.append(status_row)

        return status_rows, total

    @staticmethod
    async def get_status_summary(db: AsyncSession) -> dict:
        """
        Get summary statistics for status page header.

        Returns:
            Dictionary with summary stats
        """
        data = await CheckResultCoreService.get_status_summary_data(db)

        overall_success_rate = (
            (data["overall_successful"] / data["overall_total"]) * 100
            if data["overall_total"]
            else 0.0
        )

        return {
            "total_checks": data["total_checks"],
            "enabled_checks": data["enabled_checks"],
            "up_count": data["up_count"],
            "down_count": data["down_count"],
            "unknown_count": data["enabled_checks"] - data["up_count"] - data["down_count"],
            "active_agents": data["active_agents"],
            "overall_success_rate": round(overall_success_rate, 2),
        }

    @staticmethod
    async def build_oob_status_context(
        db: AsyncSession, request: Request, current_user: User
    ) -> dict[str, Any]:
        """
        Build the summary + status_rows context for OOB-swap responses.

        Used by routers (e.g. checks_router bulk endpoints) that need to
        refresh the status header + rows out-of-band after an action.
        Honors the page/per_page/agent/type/status/tag/search query params.
        """
        page = int(request.query_params.get("page", 1))
        per_page_param = request.query_params.get("per_page")
        if per_page_param:
            per_page = int(str(per_page_param))
        else:
            per_page = await SettingsCoreService.get_setting(db, "general.default_page_size", 50)
        offset = (page - 1) * per_page

        summary = await StatusViewService.get_status_summary(db)
        agent_filter = request.query_params.get("agent_id")
        status_rows, _total = await StatusViewService.get_all_checks_status(
            db=db,
            agent_id=UUID(agent_filter) if agent_filter else None,
            check_type=request.query_params.get("check_type"),
            status=request.query_params.get("status") or None,
            tags=split_csv(request.query_params.get("tags")),
            search=request.query_params.get("search"),
            limit=per_page,
            offset=offset,
            include_minute_bars=True,
        )
        return {
            "request": request,
            "current_user": current_user,
            "summary": summary,
            "status_rows": status_rows,
        }

    @staticmethod
    async def get_setting(db, key: str, default):
        return await SettingsCoreService.get_setting(db, key, default)

    @staticmethod
    async def build_dashboard_context(
        db: AsyncSession,
        *,
        request,
        current_user,
        agent_id: UUID | None,
        check_type: str | None,
        status: str | None,
        tags: str | None,
        search: str | None,
        page: int,
        per_page: int | None,
        include_minute_bars: bool,
        page_title: str,
    ) -> dict:
        """Build the full template context for /dashboard."""
        if per_page is None:
            per_page = await SettingsCoreService.get_setting(db, "general.default_page_size", 50)
        offset = (page - 1) * per_page

        refresh_interval = await SettingsCoreService.get_setting(
            db, "general.dashboard_refresh_interval", 10
        )
        summary = await StatusViewService.get_status_summary(db)
        status_rows, total = await StatusViewService.get_all_checks_status(
            db=db,
            agent_id=agent_id,
            check_type=check_type,
            status=status,
            tags=split_csv(tags),
            search=search,
            limit=per_page,
            offset=offset,
            include_minute_bars=include_minute_bars,
        )
        all_agents = await StatusViewService.get_all_agents(db)
        all_types = await StatusViewService.get_all_check_types(db)
        all_tags = await StatusViewService.get_all_tags(db)

        filters = {
            "agent_id": str(agent_id) if agent_id else None,
            "check_type": check_type,
            "status": status,
            "tags": tags,
            "search": search,
        }
        pagination = build_pagination(page=page, per_page=per_page, total=total, filters=filters)

        return {
            "request": request,
            "current_user": current_user,
            "summary": summary,
            "status_rows": status_rows,
            "filters": filters,
            "all_agents": all_agents,
            "all_types": all_types,
            "all_tags": all_tags,
            "pagination": pagination,
            "refresh_interval": refresh_interval,
            "page_title": page_title,
        }

    @staticmethod
    async def build_summary_partial_context(db: AsyncSession, *, request, current_user) -> dict:
        """Build context for the /internal/summary HTMX partial."""
        return {
            "request": request,
            "current_user": current_user,
            "summary": await StatusViewService.get_status_summary(db),
        }
