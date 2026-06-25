"""
Dashboard rendering — view-model assembly for status-page dashboards.
"""

from typing import Any
from uuid import UUID

from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.datetime_utils import utc_now
from app.models.check_model import Check
from app.models.status_page_model import StatusPage
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.check_core_service import CheckCoreService
from app.services.core.check_result_core_service import CheckResultCoreService
from app.services.views.status_view_service import StatusViewService

logger = get_logger("luxswirl.web.services.dashboard")


class DashboardRender:
    """Dashboard view-model assembly."""

    @staticmethod
    def sort_checks(
        checks: list[Check],
        sort_by: str = "manual",
        sort_direction: str = "asc",
    ) -> list[Check]:
        """
        Sort checks based on criteria with intelligent secondary sorts.

        Multi-level sorting:
        - Status: secondary sort by latency (fastest first when both up/down)
        - Latency: secondary sort by status (up first for same latency)
        - Name: secondary sort by status (up first for same name)
        - Uptime: secondary sort by status (up first for same uptime)

        Args:
            checks: List of Check objects with status info attached
            sort_by: Sort criteria (manual, name, status, latency, uptime)
            sort_direction: Sort direction (asc, desc)

        Returns:
            Sorted list of checks
        """
        if sort_by == "manual" or not checks:
            return checks  # Keep original order

        reverse = sort_direction == "desc"

        if sort_by == "name":
            # Primary: name, Secondary: status (up first)
            return sorted(
                checks,
                key=lambda c: (
                    c.display_name.lower() if not reverse else c.display_name.lower(),
                    not (c.latest_success if hasattr(c, "latest_success") else False),
                ),
                reverse=reverse,
            )

        elif sort_by == "status":
            # Primary: status, Secondary: latency (fastest first)
            return sorted(
                checks,
                key=lambda c: (
                    c.latest_success if hasattr(c, "latest_success") else False,
                    (c.latency_ms if hasattr(c, "latency_ms") and c.latency_ms else 999999),
                ),
                reverse=reverse,
            )

        elif sort_by == "latency":
            # Primary: latency, Secondary: status (up first)
            return sorted(
                checks,
                key=lambda c: (
                    (c.latency_ms if hasattr(c, "latency_ms") and c.latency_ms else 999999),
                    not (c.latest_success if hasattr(c, "latest_success") else False),
                ),
                reverse=reverse,
            )

        elif sort_by == "uptime":
            # Primary: uptime %, Secondary: status (up first)
            return sorted(
                checks,
                key=lambda c: (
                    c.uptime_24h if hasattr(c, "uptime_24h") else 100.0,
                    not (c.latest_success if hasattr(c, "latest_success") else False),
                ),
                reverse=reverse,
            )

        return checks

    @staticmethod
    async def get_check_details_map(db: AsyncSession, check_ids: list[UUID]) -> dict[UUID, Check]:
        """
        Get check details with status information for a list of check IDs.

        Args:
            db: Database session
            check_ids: List of check IDs to fetch

        Returns:
            Dictionary mapping check_id to Check object with status info
        """
        if not check_ids:
            return {}

        # Get status rows for specific checks only (optimized - no need to fetch all 1000)
        status_rows, _ = await StatusViewService.get_all_checks_status(
            db, check_ids=check_ids, limit=len(check_ids)
        )

        # Build status map for quick lookup
        status_map = {row.check_id: row for row in status_rows}

        # Get check objects via core service
        checks = await CheckCoreService.get_checks_by_ids(db, check_ids)

        # Build result map with status info
        check_details_map = {}
        for check in checks:
            if check.id in status_map:
                row = status_map[check.id]
                setattr(check, "latest_status", row.status_text.lower())  # noqa: B010
                setattr(check, "latest_success", row.success)  # noqa: B010
                setattr(check, "latency_ms", row.latency_ms)  # noqa: B010

            check_details_map[check.id] = check

        return check_details_map

    @staticmethod
    async def get_filtered_checks_with_status(
        db: AsyncSession,
        agent_id: UUID | None = None,
        check_type: str | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        search: str | None = None,
        limit: int = 1000,
    ) -> list[Check]:
        """
        Get filtered checks with status information.

        Args:
            db: Database session
            agent_id: Filter by agent_id
            check_type: Filter by check_type
            status: Filter by health (up/down/unknown)
            tags: Filter by tags (overlap with check or agent tags)
            search: Search in check_name or target
            limit: Max results

        Returns:
            List of Check objects with status info attached
        """
        # Use StatusViewService to get checks with status
        status_rows, _ = await StatusViewService.get_all_checks_status(
            db=db,
            agent_id=agent_id,
            check_type=check_type,
            status=status,
            tags=tags,
            search=search,
            limit=limit,
        )

        # Get check IDs from status rows
        check_ids = [row.check_id for row in status_rows]

        if not check_ids:
            return []

        # Get check objects via core service
        checks = await CheckCoreService.get_checks_by_ids(db, check_ids)

        # Attach status info to checks and maintain order
        checks_dict = {check.id: check for check in checks}
        ordered_checks = []

        for row in status_rows:
            check = checks_dict.get(row.check_id)
            if check:
                setattr(check, "latest_status", row.status_text.lower())  # noqa: B010
                setattr(check, "latest_success", row.success)  # noqa: B010
                setattr(check, "latency_ms", row.latency_ms)  # noqa: B010
                ordered_checks.append(check)

        return ordered_checks

    @staticmethod
    async def get_check_with_status(db: AsyncSession, check_id: UUID) -> Check | None:
        """
        Get a single check with status information.

        Args:
            db: Database session
            check_id: Check UUID

        Returns:
            Check object with status info or None
        """
        check_map = await DashboardRender.get_check_details_map(db, [check_id])
        return check_map.get(check_id)

    @staticmethod
    def extract_check_ids_from_items(items: list[dict[str, Any]]) -> list[UUID]:
        """
        Extract all check IDs from status page items (including nested checks in groups).

        Args:
            items: Status page items array

        Returns:
            List of check UUIDs (invalid UUIDs are filtered out)
        """
        check_ids = []
        for item in items:
            if item.get("type") == "check" and item.get("check_id"):
                check_id_raw = item.get("check_id")
                # Validate and convert to UUID
                try:
                    check_id = UUID(check_id_raw) if isinstance(check_id_raw, str) else check_id_raw
                    if isinstance(check_id, UUID):
                        check_ids.append(check_id)
                except ValueError, TypeError, AttributeError:
                    logger.warning(
                        "Invalid check_id in status page items",
                        extra={"check_id_raw": check_id_raw},
                    )
                    continue
            elif item.get("type") == "group" and "checks" in item:
                # Container group with nested checks
                for check_id_raw in item.get("checks", []):
                    try:
                        check_id = (
                            UUID(check_id_raw) if isinstance(check_id_raw, str) else check_id_raw
                        )
                        if isinstance(check_id, UUID):
                            check_ids.append(check_id)
                    except ValueError, TypeError, AttributeError:
                        logger.warning(
                            "Invalid check_id in group checks",
                            extra={"check_id_raw": check_id_raw},
                        )
                        continue
        return check_ids

    @staticmethod
    async def get_checks_matching_filter(db: AsyncSession, filter: dict[str, Any]) -> list[Check]:
        """
        Get checks matching a dynamic filter configuration.

        Args:
            db: Database session
            filter: Filter configuration dict with agent_id, check_type, tags, search

        Returns:
            List of Check objects with status info
        """
        agent_id = filter.get("agent_id")
        check_type = filter.get("check_type")
        tags = filter.get("tags") or None
        search = filter.get("search")

        return await DashboardRender.get_filtered_checks_with_status(
            db=db,
            agent_id=agent_id,
            check_type=check_type,
            tags=tags,
            search=search,
            limit=1000,
        )

    @staticmethod
    async def calculate_check_uptime(
        db: AsyncSession,
        check_id: UUID,
        hours: int = 168,  # 7 days default
    ) -> float:
        """
        Calculate uptime percentage for a check over a time period.

        Args:
            db: Database session
            check_id: Check UUID
            hours: Hours to look back

        Returns:
            Uptime percentage (0-100)
        """
        # Get stats via core service
        row = await CheckResultCoreService.get_check_detail_stats(db, check_id, hours)

        total = (row.total or 0) if row else 0
        successful = int(row.successful or 0) if row else 0

        if total == 0:
            return 100.0  # No data means assume up

        return round((successful / total) * 100, 2)

    @staticmethod
    async def generate_status_bars(
        db: AsyncSession,
        check_id: UUID,
        minutes: int = 30,
    ) -> list[dict[str, Any]]:
        """
        Generate status bars (one bar per minute) for a configurable time range.

        Args:
            db: Database session
            check_id: Check UUID
            minutes: Number of minutes to show (default 30)

        Returns:
            List of minute bars with status and counts
        """
        now = utc_now()
        minute_bars = []

        # Get raw data with 2-minute buffer via core service
        results = await CheckResultCoreService.get_raw_results_for_status_bar(
            db, check_id, minutes=minutes + 2
        )

        # Find the closest result for each minute
        minute_bars_dict = {}
        for minute_offset in range(minutes):
            # Find result closest to this exact minute
            closest_result = None
            min_distance = float("inf")

            for result_row in results:
                exact_minutes_ago = (now - result_row.timestamp).total_seconds() / 60
                distance = abs(exact_minutes_ago - minute_offset)

                if distance < min_distance:
                    min_distance = distance
                    closest_result = result_row

            minute_bars_dict[minute_offset] = closest_result

        # Create bars (most recent first)
        for minute_offset in range(minutes):
            closest_result = minute_bars_dict.get(minute_offset)
            if closest_result:
                minute_bars.append(
                    {
                        "success": closest_result.success,
                        "count": 1,
                        "avg_latency": round(closest_result.latency_ms or 0, 1),
                        "http_status_code": closest_result.http_status_code,
                        "minutes_ago": minute_offset,
                    }
                )
            else:
                # No data for this minute - show as unknown
                minute_bars.append(
                    {
                        "success": None,
                        "count": 0,
                        "avg_latency": 0,
                        "http_status_code": None,
                        "minutes_ago": minute_offset,
                    }
                )

        return minute_bars

    @staticmethod
    async def calculate_checks_uptime_bulk(
        db: AsyncSession,
        check_ids: list[UUID],
        hours: int = 168,
    ) -> dict[UUID, dict[str, Any]]:
        """
        Calculate uptime percentage for multiple checks in a single query.
        Also returns last failure timestamp for display.

        TODO: Add caching here (15-30 second cache recommended for public pages)
              to avoid recalculating on every page load. Consider using
              functools.lru_cache with time-based expiration or a proper
              cache backend like Redis for multi-instance deployments.

        Args:
            db: Database session
            check_ids: List of check UUIDs
            hours: Hours to look back

        Returns:
            Dictionary mapping check_id to dict with 'uptime' and 'last_failure_at' keys
        """
        if not check_ids:
            return {}

        # Get bulk stats via core service
        rows = await CheckResultCoreService.get_uptime_stats_bulk(db, check_ids, hours)

        # Build uptime map
        uptime_map = {}
        for row in rows:
            total = row.total or 0
            successful = int(row.successful or 0)
            if total == 0:
                uptime_pct = 100.0
            else:
                uptime_pct = round((successful / total) * 100, 2)

            uptime_map[row.check_id] = {
                "uptime": uptime_pct,
                "last_failure_at": None,  # Will fill in next
            }

        # Fill in 100% for checks with no data
        for check_id in check_ids:
            if check_id not in uptime_map:
                uptime_map[check_id] = {
                    "uptime": 100.0,
                    "last_failure_at": None,
                }

        # Get last failure timestamp for checks with uptime < 100%
        failed_check_ids = [
            cid
            for cid, data in uptime_map.items()
            if data["uptime"] is not None and data["uptime"] < 100.0
        ]
        if failed_check_ids:
            # Get last failure timestamps via core service
            failure_rows = await CheckResultCoreService.get_last_failure_timestamps_bulk(
                db, failed_check_ids, hours
            )

            for row in failure_rows:
                if row.check_id in uptime_map:
                    uptime_map[row.check_id]["last_failure_at"] = row.last_failure_at

        return uptime_map

    @staticmethod
    def _get_bucket_size(minutes: int) -> int:
        """
        Determine bucket size based on time range to keep bar count reasonable.
        Always returns a number of bars that renders well visually (~30-90 bars).

        Returns:
            Bucket size in minutes
        """
        if minutes <= 60:
            return 1  # 30-60 bars
        elif minutes <= 240:
            return 5  # 24-48 bars
        elif minutes <= 480:
            return 10  # 24-48 bars
        else:
            return 30  # 48 bars for 24h

    @staticmethod
    async def generate_status_bars_bulk(
        db: AsyncSession,
        check_ids: list[UUID],
        minutes: int = 30,
    ) -> dict[UUID, list[dict[str, Any]]]:
        """
        Generate status bars for multiple checks using adaptive bucketing.
        For short ranges (<=30m): 1-minute bars.
        For longer ranges: aggregated buckets (5m/10m/30m) via SQL.

        TODO: Add caching here (15-30 second cache recommended for public pages).

        Args:
            db: Database session
            check_ids: List of check UUIDs
            minutes: Number of minutes to show (default 30)

        Returns:
            Dictionary mapping check_id to list of bars
        """
        if not check_ids:
            return {}

        bucket_minutes = DashboardRender._get_bucket_size(minutes)
        num_bars = minutes // bucket_minutes
        now = utc_now()

        if bucket_minutes == 1:
            # Original per-minute approach for short ranges (optimized algorithm)
            all_results = await CheckResultCoreService.get_results_for_status_bars_bulk(
                db, check_ids, minutes=minutes + 2
            )

            # Group results by check_id
            results_by_check: dict[UUID, list] = {}
            for result_row in all_results:
                if result_row.check_id not in results_by_check:
                    results_by_check[result_row.check_id] = []
                results_by_check[result_row.check_id].append(result_row)

            bars_map = {}
            for check_id in check_ids:
                results = results_by_check.get(check_id, [])

                # Pre-compute minute offsets for O(n) instead of O(n*m)
                minute_bars_dict: dict[int, Any] = {}
                for result_row in results:
                    exact_minutes_ago = (now - result_row.timestamp).total_seconds() / 60
                    nearest_minute = round(exact_minutes_ago)
                    if 0 <= nearest_minute < minutes:
                        if nearest_minute not in minute_bars_dict:
                            minute_bars_dict[nearest_minute] = result_row
                        else:
                            # Keep the one closest to the exact minute
                            existing = minute_bars_dict[nearest_minute]
                            existing_dist = abs(
                                (now - existing.timestamp).total_seconds() / 60 - nearest_minute
                            )
                            new_dist = abs(exact_minutes_ago - nearest_minute)
                            if new_dist < existing_dist:
                                minute_bars_dict[nearest_minute] = result_row

                minute_bars = []
                for minute_offset in range(minutes):
                    closest_result = minute_bars_dict.get(minute_offset)
                    if closest_result:
                        minute_bars.append(
                            {
                                "success": closest_result.success,
                                "count": 1,
                                "avg_latency": round(closest_result.latency_ms or 0, 1),
                                "http_status_code": closest_result.http_status_code,
                                "minutes_ago": minute_offset,
                            }
                        )
                    else:
                        minute_bars.append(
                            {
                                "success": None,
                                "count": 0,
                                "avg_latency": 0,
                                "http_status_code": None,
                                "minutes_ago": minute_offset,
                            }
                        )
                bars_map[check_id] = minute_bars
            return bars_map

        # Bucketed approach for longer ranges — aggregation done in SQL
        all_buckets = await CheckResultCoreService.get_bucketed_status_bars_bulk(
            db, check_ids, minutes=minutes + bucket_minutes, bucket_minutes=bucket_minutes
        )

        # Group by check_id, index by bucket offset
        buckets_by_check: dict[UUID, dict[int, Any]] = {}
        for row in all_buckets:
            check_id = row.check_id
            if check_id not in buckets_by_check:
                buckets_by_check[check_id] = {}
            # Calculate which bar slot this bucket belongs to
            bucket_age_minutes = (now - row.bucket_start).total_seconds() / 60
            bar_index = int(bucket_age_minutes / bucket_minutes)
            if 0 <= bar_index < num_bars:
                buckets_by_check[check_id][bar_index] = row

        bars_map = {}
        for check_id in check_ids:
            check_buckets = buckets_by_check.get(check_id, {})
            bars = []
            for bar_idx in range(num_bars):
                bucket = check_buckets.get(bar_idx)
                if bucket:
                    total = (bucket.success_count or 0) + (bucket.fail_count or 0)
                    has_failure = (bucket.fail_count or 0) > 0
                    bars.append(
                        {
                            "success": not has_failure if total > 0 else None,
                            "count": total,
                            "avg_latency": float(bucket.avg_latency or 0),
                            "http_status_code": bucket.http_status_code,
                            "minutes_ago": bar_idx * bucket_minutes,
                        }
                    )
                else:
                    bars.append(
                        {
                            "success": None,
                            "count": 0,
                            "avg_latency": 0,
                            "http_status_code": None,
                            "minutes_ago": bar_idx * bucket_minutes,
                        }
                    )
            bars_map[check_id] = bars
        return bars_map

    @staticmethod
    async def build_dashboard_context(
        db: AsyncSession,
        status_page: StatusPage,
    ) -> dict[str, Any]:
        """
        Build complete context for rendering dashboard items.
        Consolidates all the data fetching logic that was scattered in routers.

        Args:
            db: Database session
            status_page: StatusPage object

        Returns:
            Dictionary with check_details_map, all_agents, all_types, filter_group_matches
        """
        # Get check IDs from dashboard items
        check_ids = DashboardRender.extract_check_ids_from_items(status_page.items)
        logger.info(
            "Extracted check IDs from items",
            extra={"count": len(check_ids), "check_ids": [str(c) for c in check_ids]},
        )

        # Get check details with status
        check_details_map_uuid = await DashboardRender.get_check_details_map(db, check_ids)
        logger.info(
            "Got checks in details map",
            extra={"count": len(check_details_map_uuid)},
        )

        # Convert UUID keys to strings for template lookup (items JSON stores UUIDs as strings)
        check_details_map = {
            str(uuid_key): check for uuid_key, check in check_details_map_uuid.items()
        }

        # Get all agents and types for dropdowns
        all_agents = await AgentCoreService.get_admitted_agents(db)
        all_types = await CheckCoreService.get_distinct_check_types(db)

        # Build filter group matches and container group sorted checks
        filter_group_matches = {}
        container_group_sorted_checks = {}

        for idx, item in enumerate(status_page.items):
            if item.get("type") == "group":
                sort_by = item.get("sort_by", "manual")
                sort_direction = item.get("sort_direction", "asc")

                if "filter" in item:
                    # Dynamic filter group
                    matching_checks = await DashboardRender.get_checks_matching_filter(
                        db, item.get("filter", {})
                    )
                    # Apply sorting
                    matching_checks = DashboardRender.sort_checks(
                        matching_checks, sort_by, sort_direction
                    )
                    filter_group_matches[idx] = matching_checks

                elif "checks" in item and sort_by != "manual":
                    # Container group with non-manual sorting
                    check_ids_str = item.get("checks", [])
                    check_objects = []
                    for check_id_str in check_ids_str:
                        if check_id_str in check_details_map:
                            check_objects.append(check_details_map[check_id_str])

                    # Apply sorting
                    sorted_checks = DashboardRender.sort_checks(
                        check_objects, sort_by, sort_direction
                    )
                    # Store sorted check IDs
                    container_group_sorted_checks[idx] = [str(c.id) for c in sorted_checks]

        return {
            "check_details_map": check_details_map,
            "all_agents": all_agents,
            "all_types": all_types,
            "filter_group_matches": filter_group_matches,
            "container_group_sorted_checks": container_group_sorted_checks,
        }

    @staticmethod
    def format_time_range_label(minutes: int) -> str:
        """Convert minutes to a human-readable label (e.g., 30 -> '30m', 60 -> '1h')."""
        labels = {30: "30m", 60: "1h", 120: "2h", 240: "4h", 480: "8h", 1440: "24h"}
        return labels.get(minutes, f"{minutes}m")

    @staticmethod
    async def render_public_dashboard(
        db: AsyncSession,
        status_page: StatusPage,
        time_range_minutes: int = 30,
    ) -> dict[str, Any]:
        """
        Render public dashboard with all checks and stats.
        Optimized with bulk queries to avoid N+1 problem.

        Args:
            db: Database session
            status_page: StatusPage object
            time_range_minutes: Number of minutes for status bars (default 30)

        Returns:
            Dictionary with rendered dashboard data
        """
        # PHASE 1: Collect all check IDs and load check objects
        check_map = {}  # Maps check_id -> Check object
        items_with_checks = []  # List of (item, check_ids) tuples

        for item in status_page.items:
            item_type = item.get("type")

            if item_type == "check":
                check_id_str = item.get("check_id")
                if check_id_str:
                    try:
                        check_id = (
                            UUID(check_id_str) if isinstance(check_id_str, str) else check_id_str
                        )
                        check = await DashboardRender.get_check_with_status(db, check_id)
                        if check:
                            check_map[check_id_str] = check
                            items_with_checks.append((item, [check_id_str]))
                    except ValueError, TypeError:
                        logger.warning(
                            "Invalid check_id in public dashboard",
                            extra={"check_id_raw": check_id_str},
                        )
                        continue

            elif item_type == "group":
                group_checks = []
                if "filter" in item:
                    matching_checks = await DashboardRender.get_checks_matching_filter(
                        db, item.get("filter", {})
                    )
                    group_checks = matching_checks
                elif "checks" in item:
                    check_ids = item.get("checks", [])
                    for check_id_str in check_ids:
                        try:
                            check_id = (
                                UUID(check_id_str)
                                if isinstance(check_id_str, str)
                                else check_id_str
                            )
                            check = await DashboardRender.get_check_with_status(db, check_id)
                            if check:
                                group_checks.append(check)
                        except ValueError, TypeError:
                            logger.warning(
                                "Invalid check_id in group checks (public)",
                                extra={"check_id_raw": check_id_str},
                            )
                            continue

                # Add to check_map (use string keys for template lookup)
                group_check_ids = []
                for check in group_checks:
                    check_id_str = str(check.id)
                    check_map[check_id_str] = check
                    group_check_ids.append(check_id_str)

                items_with_checks.append((item, group_check_ids))

        # PHASE 2: Bulk calculate stats for ALL checks (5 queries total instead of 5*N)
        # Convert string keys to UUID objects for bulk queries
        all_check_ids_uuid = [
            UUID(check_id_str) if isinstance(check_id_str, str) else check_id_str
            for check_id_str in check_map.keys()
        ]

        if all_check_ids_uuid:
            # Bulk uptime calculations (4 queries)
            uptime_24h_map = await DashboardRender.calculate_checks_uptime_bulk(
                db, all_check_ids_uuid, hours=24
            )
            uptime_7d_map = await DashboardRender.calculate_checks_uptime_bulk(
                db, all_check_ids_uuid, hours=168
            )
            uptime_30d_map = await DashboardRender.calculate_checks_uptime_bulk(
                db, all_check_ids_uuid, hours=720
            )
            uptime_90d_map = await DashboardRender.calculate_checks_uptime_bulk(
                db, all_check_ids_uuid, hours=2160
            )

            # Bulk status bars (1 query)
            bars_map = await DashboardRender.generate_status_bars_bulk(
                db, all_check_ids_uuid, minutes=time_range_minutes
            )

            # Attach stats to check objects
            # check_map has string keys, but bulk methods return UUID keys - convert for lookup
            for check_id_str, check in check_map.items():
                check_id_uuid = (
                    UUID(check_id_str) if isinstance(check_id_str, str) else check_id_str
                )

                uptime_24h_data = uptime_24h_map.get(
                    check_id_uuid, {"uptime": 100.0, "last_failure_at": None}
                )
                uptime_7d_data = uptime_7d_map.get(
                    check_id_uuid, {"uptime": 100.0, "last_failure_at": None}
                )
                uptime_30d_data = uptime_30d_map.get(
                    check_id_uuid, {"uptime": 100.0, "last_failure_at": None}
                )
                uptime_90d_data = uptime_90d_map.get(
                    check_id_uuid, {"uptime": 100.0, "last_failure_at": None}
                )

                setattr(check, "uptime_24h", uptime_24h_data["uptime"])  # noqa: B010
                setattr(check, "uptime_7d", uptime_7d_data["uptime"])  # noqa: B010
                setattr(check, "uptime_30d", uptime_30d_data["uptime"])  # noqa: B010
                setattr(check, "uptime_90d", uptime_90d_data["uptime"])  # noqa: B010
                setattr(check, "last_failure_at", uptime_24h_data["last_failure_at"])  # noqa: B010
                setattr(check, "minute_bars", bars_map.get(check_id_uuid, []))  # noqa: B010

        # PHASE 3: Build rendered items
        rendered_items = []
        all_checks = []

        for item, check_ids in items_with_checks:
            item_type = item.get("type")

            if item_type == "check":
                check = check_map.get(check_ids[0])
                if check:
                    rendered_items.append({"type": "check", "check": check})
                    all_checks.append(check)

            elif item_type == "group":
                group_name = item.get("name", "Unnamed Group")
                group_checks = [check_map[cid] for cid in check_ids if cid in check_map]

                # Apply sorting if configured
                sort_by = item.get("sort_by", "manual")
                sort_direction = item.get("sort_direction", "asc")
                group_checks = DashboardRender.sort_checks(group_checks, sort_by, sort_direction)

                rendered_items.append(
                    {
                        "type": "group",
                        "name": group_name,
                        "checks": group_checks,
                        "sort_by": sort_by,
                        "sort_direction": sort_direction,
                    }
                )
                all_checks.extend(group_checks)

        # Calculate overall stats
        total_checks = len(all_checks)
        checks_up = sum(1 for c in all_checks if hasattr(c, "latest_success") and c.latest_success)
        checks_down = total_checks - checks_up

        # Calculate overall uptime
        if total_checks > 0:
            overall_uptime = round((checks_up / total_checks) * 100, 1)
        else:
            overall_uptime = 100.0

        # Determine overall status
        if checks_down == 0:
            overall_status = "operational"
        elif checks_down < total_checks:
            overall_status = "degraded"
        else:
            overall_status = "outage"

        bucket_size = DashboardRender._get_bucket_size(time_range_minutes)
        num_bars = time_range_minutes // bucket_size

        return {
            "overall_status": overall_status,
            "overall_uptime": overall_uptime,
            "total_checks": total_checks,
            "checks_up": checks_up,
            "checks_down": checks_down,
            "rendered_items": rendered_items,
            "time_range_minutes": time_range_minutes,
            "time_range_label": DashboardRender.format_time_range_label(time_range_minutes),
            "num_bars": num_bars,
            "bucket_minutes": bucket_size,
        }
