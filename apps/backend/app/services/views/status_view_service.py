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
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.datetime_utils import utc_now
from app.core.liveness import silent_after
from app.core.query_params import split_csv
from app.models.enum_model import CheckHealthStatus, CheckType
from app.schemas.auth_schema import AuthedUser
from app.schemas.check_schema import DashboardCheck, DashboardCheckAgent, DashboardUrls
from app.schemas.pagination_schema import build_pagination
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.check_core_service import CheckCoreService
from app.services.core.check_result_core_service import CheckResultCoreService
from app.services.core.settings_core_service import SettingsCoreService
from app.shared.logger import get_logger

logger = get_logger("luxswirl.web.services.status")


class StatusRow:
    """Represents a single check status row for UI display."""

    def __init__(
        self,
        check_id: UUID,
        agent_id: UUID,
        check_name: str,
        check_type: CheckType,
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
        minute_bars: list[dict[str, Any]] | None = None,
        depends_on_check_id: UUID | None = None,
        dependent_count: int = 0,
    ):
        self.check_id = check_id
        # Carried so the dashboard can name the agent without a second
        # round-trip to the ORM for a row this one already describes.
        self.agent_id = agent_id
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
        # Set by attach_row_urls() when these rows are headed for a template.
        # Rows built purely for status lookup (DashboardRender) never render, so
        # they legitimately have no URL — hence the loud property below rather
        # than a default that would silently emit an empty hx-get.
        self._detail_url: str | None = None

    @property
    def detail_url(self) -> str:
        """Finished href for this check's detail panel.

        Raises:
            RuntimeError: If the row was built without a request. A row that a
                template renders must go through attach_row_urls() first.
        """
        if self._detail_url is None:
            raise RuntimeError(
                f"StatusRow({self.check_id}).detail_url is unset — call "
                "StatusViewService.attach_row_urls(request, rows) before rendering."
            )
        return self._detail_url

    @property
    def status_class(self) -> str:
        """Get CSS class for status badge."""
        if self.success is None:
            return "unknown"
        return "success" if self.success else "error"

    @property
    def health(self) -> CheckHealthStatus:
        """Up, down, or unknown when there is no current result (none yet, or
        the check has gone silent)."""
        if self.success is None:
            return CheckHealthStatus.UNKNOWN
        return CheckHealthStatus.UP if self.success else CheckHealthStatus.DOWN

    @property
    def status_text(self) -> str:
        """Get human-readable status text."""
        if self.success is None:
            return "No data"
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

    def to_dashboard_check(self) -> DashboardCheck:
        """This row as a dashboard/status-page card.

        Lives here rather than in the renderer because every field already
        belongs to this row — the projection is a rename, not a second query.
        The dashboard used to re-fetch the ORM Check and setattr the status
        onto it, which put an ORM model through the view seam and left the
        status missing whenever the re-fetch found a check the status query had
        not returned.
        """
        return DashboardCheck(
            id=self.check_id,
            display_name=self.check_name,
            check_type=self.check_type,
            target=self.target,
            tags=self.check_tags,
            enabled=self.enabled,
            agent=DashboardCheckAgent(
                id=self.agent_id,
                agent_name=self.agent_name,
                tags=self.agent_tags,
            ),
            latest_status=self.health,
            latest_success=self.success,
            latency_ms=self.latency_ms,
            uptime_24h=self.uptime_24h,
        )


class StatusViewService:
    """Service for status page data aggregation."""

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
        (
            checks_and_agents,
            total,
        ) = await CheckResultCoreService.get_checks_with_agents_filtered(
            db,
            agent_id=agent_id,
            check_type=check_type,
            status=status,
            tags=tags,
            search=search,
            check_ids=check_ids,
            skip=offset,
            limit=limit,
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
        stats_map = await CheckResultCoreService.get_24h_stats_batch(
            db, filtered_check_ids
        )
        dependent_counts = await CheckCoreService.count_dependents_bulk(
            db, filtered_check_ids
        )

        # Batch query: Get 15-minute bars for all checks (if requested)
        minute_bars_map: dict[UUID, list[dict[str, Any]]] = {}
        if include_minute_bars:
            all_bars_results = await CheckResultCoreService.get_minute_bars_results(
                db, filtered_check_ids, minutes=15
            )

            # Group results by check_id and minute offset (data shaping in view service)
            now = utc_now()
            # (success, latency_ms) per result: all a bar reads.
            check_minute_buckets: dict[
                UUID, dict[int, list[tuple[bool, float | None]]]
            ] = defaultdict(lambda: defaultdict(list))

            for result in all_bars_results:
                minutes_ago = int((now - result.timestamp).total_seconds() / 60)
                if 0 <= minutes_ago < 15:
                    check_minute_buckets[result.check_id][minutes_ago].append(
                        (result.success, result.latency_ms)
                    )

            # Build minute bars for each check
            for cid in filtered_check_ids:
                minute_buckets = check_minute_buckets.get(cid, {})
                bars: list[dict[str, bool | int | float | None]] = []

                for minute_offset in range(15):  # 0 to 14 minutes ago
                    if minute_offset in minute_buckets:
                        results_in_minute = minute_buckets[minute_offset]
                        all_success = all(ok for ok, _ in results_in_minute)

                        # Calculate avg latency (filter out None values)
                        latencies = [
                            ms for _, ms in results_in_minute if ms is not None
                        ]
                        avg_latency = (
                            sum(latencies) / len(latencies) if latencies else 0
                        )

                        bar: dict[str, bool | int | float | None] = {
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
        now = utc_now()

        for check, agent in checks_and_agents:
            latest_result = latest_results_map.get(check.id)
            # A check that has gone silent shows no data, not its last state
            # (the same rule as the no_data alert's default).
            current = (
                latest_result
                if latest_result
                and latest_result.timestamp
                >= now - silent_after(check.interval_seconds)
                else None
            )
            stats = stats_map.get(check.id)

            uptime_24h = None
            if stats and stats.total and stats.total > 0:
                uptime_24h = (stats.successful / stats.total) * 100

            # Parse tags from check and agent (both are PostgreSQL arrays)
            check_tags = [
                tag.strip() for tag in (check.tags or []) if tag and tag.strip()
            ]
            agent_tags = [
                tag.strip() for tag in (agent.tags or []) if tag and tag.strip()
            ]

            # Get minute bars for this check (if requested)
            minute_bars = (
                minute_bars_map.get(check.id, []) if include_minute_bars else []
            )

            # Create status row
            status_row = StatusRow(
                check_id=check.id,
                agent_id=agent.id,
                check_name=check.display_name,
                check_type=check.check_type,
                target=check.target,
                agent_name=agent.agent_name
                or str(agent.id),  # Use agent_name for display, fallback to ID
                success=current.success if current else None,
                latency_ms=current.latency_ms if current else None,
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
    def build_dashboard_urls(request: Request) -> DashboardUrls:
        """The dashboard's own actions, resolved by route name."""

        def u(name: str) -> str:
            return str(request.app.url_path_for(name))

        return DashboardUrls(
            page=u("status_page"),
            create_form=u("check_create_form"),
            summary=u("status_summary_partial"),
            bulk_action=u("bulk_action"),
            bulk_modify=u("bulk_modify"),
        )

    @staticmethod
    def attach_row_urls(
        request: Request, rows: list[StatusRow], *, hours: int = 4
    ) -> list[StatusRow]:
        """Resolve each row's detail href by route name, for rows about to render.

        .path, not str(): url_for returns an absolute url from the request's
        scheme/host, which behind TLS-terminating nginx can emit http:// into an
        https:// page.
        """
        for row in rows:
            path = request.url_for("check_detail_panel", check_id=row.check_id).path
            row._detail_url = f"{path}?hours={hours}"
        return rows

    @staticmethod
    async def get_status_summary(db: AsyncSession) -> dict[str, Any]:
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

        enabled = data["enabled_checks"]

        def share(count: int) -> float:
            """That count as a percentage of the enabled checks."""
            return round(count / enabled * 100, 1) if enabled else 0.0

        unknown_count = enabled - data["up_count"] - data["down_count"]

        return {
            "total_checks": data["total_checks"],
            "enabled_checks": enabled,
            "up_count": data["up_count"],
            "down_count": data["down_count"],
            "unknown_count": unknown_count,
            # The card shows each count as a share of the enabled checks; the
            # percentage is computed here so the template only renders it.
            "up_pct": share(data["up_count"]),
            "down_pct": share(data["down_count"]),
            "unknown_pct": share(unknown_count),
            "active_agents": data["active_agents"],
            "overall_success_rate": round(overall_success_rate, 2),
        }

    @staticmethod
    async def build_oob_status_context(
        db: AsyncSession, request: Request, current_user: AuthedUser
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
            per_page = await SettingsCoreService.get_setting(
                db, "general.default_page_size", 50
            )
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
        StatusViewService.attach_row_urls(request, status_rows)
        return {
            "request": request,
            "current_user": current_user,
            "summary": summary,
            "status_rows": status_rows,
        }

    @staticmethod
    async def build_dashboard_context(
        db: AsyncSession,
        *,
        request: Request,
        current_user: AuthedUser,
        agent_id: UUID | None,
        check_type: str | None,
        status: str | None,
        tags: str | None,
        search: str | None,
        page: int,
        per_page: int | None,
        include_minute_bars: bool,
        page_title: str,
    ) -> dict[str, Any]:
        """Build the full template context for /dashboard."""
        if per_page is None:
            per_page = await SettingsCoreService.get_setting(
                db, "general.default_page_size", 50
            )
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
        StatusViewService.attach_row_urls(request, status_rows)
        all_agents = await AgentCoreService.get_admitted_agents(db)
        all_types = await CheckCoreService.get_distinct_check_types(db)
        all_tags = await CheckCoreService.get_all_tags_combined(db)

        filters = {
            "agent_id": str(agent_id) if agent_id else None,
            "check_type": check_type,
            "status": status,
            "tags": tags,
            "search": search,
        }
        pagination = build_pagination(
            page=page, per_page=per_page, total=total, filters=filters
        )

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
            "dashboard_urls": StatusViewService.build_dashboard_urls(request),
        }

    @staticmethod
    async def build_summary_partial_context(
        db: AsyncSession, *, request: Request, current_user: AuthedUser
    ) -> dict[str, Any]:
        """Build context for the /internal/summary HTMX partial."""
        return {
            "request": request,
            "current_user": current_user,
            "summary": await StatusViewService.get_status_summary(db),
        }
