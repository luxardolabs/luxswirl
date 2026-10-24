"""
Check detail service - provides detailed check information for side panel.
"""

import copy
import json
from collections import defaultdict
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.datetime_utils import utc_now
from app.core.ssl_expiry import ssl_expiry_fields
from app.core.synthetic_timings import build_synthetic_view
from app.schemas.check_result_schema import RecentCheckResult
from app.services.core.check_core_service import CheckCoreService
from app.services.core.check_result_core_service import CheckResultCoreService
from app.services.core.settings_core_service import SettingsCoreService
from app.shared.logger import get_logger

logger = get_logger("luxswirl.web.services.check_detail")


# The certificate card's classes and label for each ssl_expiry_fields state. An
# unreadable expiry is grey "Unknown", never the green of a valid certificate.
SSL_CERT_TONES: dict[str, dict[str, str]] = {
    "expired": {
        "card": "bg-red-500/10 border-red-500/30",
        "text": "text-red-400",
        "badge": "bg-red-600/20 text-red-300",
        "label": "Expired",
    },
    "critical": {
        "card": "bg-orange-500/10 border-orange-500/30",
        "text": "text-orange-400",
        "badge": "bg-orange-600/20 text-orange-300",
        "label": "Critical",
    },
    "soon": {
        "card": "bg-yellow-500/10 border-yellow-500/30",
        "text": "text-yellow-400",
        "badge": "bg-yellow-600/20 text-yellow-300",
        "label": "Expiring Soon",
    },
    "valid": {
        "card": "bg-green-500/5 border-green-500/20",
        "text": "text-green-400",
        "badge": "bg-green-600/20 text-green-300",
        "label": "Valid",
    },
    "unknown": {
        "card": "bg-slate-500/10 border-slate-500/30",
        "text": "text-slate-300",
        "badge": "bg-slate-600/20 text-slate-300",
        "label": "Unknown",
    },
}


# Every key the synthetic panel reads, with what "not reported" looks like.
SYNTHETIC_PANEL_DEFAULTS: dict[str, Any] = {
    "status": "unknown",
    "steps": [],
    "console_errors": [],
    "request_failures": [],
    "browser_timing": {},
    "total_execution_time_ms": None,
    "track_durations": {},
    "track_timings": {},
}


class ResultProxy:
    """One bucket of history, shaped like a result for the panel's template."""

    def __init__(
        self,
        timestamp: datetime,
        success: bool | None,
        latency_ms: float | None,
        error: str | None,
        metrics: dict[str, Any] | str | None,
        check_id: UUID | None = None,
    ) -> None:
        self.timestamp = timestamp
        self.success = success
        self.latency_ms = latency_ms
        self.error = error
        self._metrics = metrics
        self.check_id = check_id

    def get_metrics(self) -> dict[str, Any]:
        """The bucket's metrics as a dict; JSON text is decoded, junk is {}."""
        if not self._metrics:
            return {}
        if isinstance(self._metrics, str):
            try:
                decoded = json.loads(self._metrics)
            except json.JSONDecodeError, TypeError:
                return {}
            return decoded if isinstance(decoded, dict) else {}
        return self._metrics


def build_check_urls(request: Request, check_id: UUID, hours: int) -> dict[str, str]:
    """Finished hrefs for one check's panel actions, resolved by route NAME.

    .path, not str(): url_for returns an absolute url from the request's scheme/host,
    which behind TLS-terminating nginx can emit http:// into an https:// page.
    """

    def u(name: str, **kw: object) -> str:
        return str(request.url_for(name, **kw).path)

    return {
        "toggle": u("toggle_check", check_id=check_id),
        "detail": f"{u('check_detail_panel', check_id=check_id)}?hours={hours}",
        # The range selector posts its own ?hours=, so it needs the bare path.
        "detail_base": u("check_detail_panel", check_id=check_id),
        "clone_form": u("clone_form", check_id=check_id),
        "dependents": u("dependents_panel", check_id=check_id),
        "edit_form": u("check_edit_form", check_id=check_id),
        "delete": u("web_delete_check", check_id=check_id),
    }


class CheckDetailViewService:
    """Service for check detail data."""

    @staticmethod
    def parse_time_range_to_hours(time_range: str) -> int:
        """Convert a time-range string ("4h"/"24h"/"3d"/"7d") to hours (default 4)."""
        if time_range.endswith("h"):
            return int(time_range[:-1])
        if time_range.endswith("d"):
            return int(time_range[:-1]) * 24
        return 4

    @staticmethod
    async def get_check_detail(
        db: AsyncSession, request: Request, check_id: UUID, hours: int = 4
    ) -> dict[str, Any] | None:
        """
        Get full check details including recent history.

        Args:
            db: Database session
            check_id: Check ID
            hours: Number of hours of history to fetch (default: 4)

        Returns:
            Dictionary with check details
        """
        # Get check with agent
        row = await CheckResultCoreService.get_check_with_agent(db, check_id)

        if not row:
            return None

        check, agent = row

        # Get recent results based on time range (bucketed by core service)
        raw_rows = await CheckResultCoreService.get_bucketed_history(
            db, check_id, hours
        )

        history_models = [
            ResultProxy(
                row.bucket_time,
                row.success,
                row.latency_ms,
                row.error,
                row.metrics,
                check_id,
            )
            for row in raw_rows
        ]

        # The operator configures these; the panel must agree with the values
        # stored at ingest, so both read the same settings.
        alert_defaults = await SettingsCoreService.get_alert_defaults(db)
        ssl_warning_days = alert_defaults.ssl_cert_warning_days
        ssl_critical_days = alert_defaults.ssl_cert_critical_days

        # Convert history to dicts and process SSL certs
        history: list[dict[str, Any]] = []
        for result in history_models:
            metrics = result.get_metrics()  # SSL cert already computed by the server
            history_item: dict[str, Any] = {
                "timestamp": result.timestamp.isoformat(),  # ISO string for JSON serialization
                "success": result.success,
                "latency_ms": result.latency_ms,
                "error": result.error,
                "additional_data": metrics,
            }

            # Complete the synthetic block the same way: the panel reads every
            # one of these keys, and a script that reports no page timings (the
            # agent's default) leaves the track_* keys out.
            synthetic = metrics.get("synthetic") if metrics else None
            if isinstance(synthetic, dict):
                for key, empty in SYNTHETIC_PANEL_DEFAULTS.items():
                    synthetic.setdefault(key, copy.copy(empty))

            # Complete the certificate the agent reported: the panel reads the
            # expiry flags unconditionally, so they are set for every cert.
            if metrics:
                response = metrics.get("response", {})
                ssl_cert = response.get("ssl_certificate")
                if ssl_cert:
                    # The panel renders the raw notAfter string trimmed; trim it
                    # here so the template only renders.
                    expiry = ssl_cert.get("expiration_date")
                    ssl_cert["expiration_display"] = (
                        expiry[:20] if expiry else "Unknown"
                    )
                    ssl_cert.update(
                        ssl_expiry_fields(
                            ssl_cert.get("expiration_date"),
                            warning_days=ssl_warning_days,
                            critical_days=ssl_critical_days,
                        )
                    )
                    ssl_cert["tone"] = SSL_CERT_TONES[ssl_cert["state"]]

            history.append(history_item)

        # Prepare chart data (all calculations done server-side)
        chart_data: dict[str, Any] = {
            "width": 800,
            "height": 200,
            "points": [],
            "path_length": 0.0,
            "curve_path": "",
            "labels": {
                "y_axis": {
                    "max": {"value": 0, "text": "0ms", "y": 15},
                    "middle": {"value": 0, "text": "0ms", "y": 105},
                    "min": {"value": 0, "text": "0ms", "y": 195},
                },
                "x_axis": {
                    "start": {"time": "", "text": "", "x": 10},
                    "middle": {"time": "", "text": "", "x": 400},
                    "end": {"time": "", "text": "", "x": 790},
                },
            },
            "has_data": False,
        }

        # Only calculate chart data if we have history with latency values
        # Data is already ordered ASC (oldest first) from database query
        if history:
            # Extract latency values
            latencies = [
                h["latency_ms"] for h in history if h["latency_ms"] is not None
            ]

            if latencies:
                chart_data["has_data"] = True

                # Calculate data bounds
                max_latency = max(latencies)
                min_latency = min(latencies)
                range_latency = (
                    max_latency - min_latency
                    if max_latency > min_latency
                    else max_latency
                )
                range_latency = range_latency if range_latency > 0 else 1

                # Build chart points with coordinates
                points = []
                num_points = len(history)
                for i, h in enumerate(history):
                    if h["latency_ms"] is not None:
                        x = i * (800 / (num_points - 1)) if num_points > 1 else 400
                        y = 180 - (
                            (h["latency_ms"] - min_latency) / range_latency * 160
                        )
                        points.append(
                            {
                                "x": x,
                                "y": y,
                                "success": h["success"],
                                "latency": h["latency_ms"],
                                "timestamp": h["timestamp"],
                            }
                        )

                chart_data["points"] = points

                # Build smooth Bezier curve path
                curve_points = []
                for i, point in enumerate(points):
                    if i == 0:
                        curve_points.append(f"M {point['x']},{point['y']}")
                    else:
                        prev_point = points[i - 1]
                        cp1_x = prev_point["x"] + (point["x"] - prev_point["x"]) * 0.5
                        cp1_y = prev_point["y"]
                        cp2_x = prev_point["x"] + (point["x"] - prev_point["x"]) * 0.5
                        cp2_y = point["y"]
                        curve_points.append(
                            f"C {cp1_x},{cp1_y} {cp2_x},{cp2_y} {point['x']},{point['y']}"
                        )

                chart_data["curve_path"] = " ".join(curve_points)

                # Calculate EXACT Bezier curve path length
                # We need to account for the curve, not just straight lines between points
                path_length = 0.0
                for i in range(1, len(points)):
                    prev_point = points[i - 1]
                    curr_point = points[i]

                    # Control points for this Bezier segment
                    cp1_x = prev_point["x"] + (curr_point["x"] - prev_point["x"]) * 0.5
                    cp1_y = prev_point["y"]
                    cp2_x = prev_point["x"] + (curr_point["x"] - prev_point["x"]) * 0.5
                    cp2_y = curr_point["y"]

                    # Approximate Bezier curve length using control polygon method
                    # Length is approximately the average of the chord and control polygon lengths
                    # Chord length (straight line from prev to curr)
                    dx_chord = curr_point["x"] - prev_point["x"]
                    dy_chord = curr_point["y"] - prev_point["y"]
                    chord_length = (dx_chord * dx_chord + dy_chord * dy_chord) ** 0.5

                    # Control polygon length (prev -> cp1 -> cp2 -> curr)
                    dx1 = cp1_x - prev_point["x"]
                    dy1 = cp1_y - prev_point["y"]
                    leg1 = (dx1 * dx1 + dy1 * dy1) ** 0.5

                    dx2 = cp2_x - cp1_x
                    dy2 = cp2_y - cp1_y
                    leg2 = (dx2 * dx2 + dy2 * dy2) ** 0.5

                    dx3 = curr_point["x"] - cp2_x
                    dy3 = curr_point["y"] - cp2_y
                    leg3 = (dx3 * dx3 + dy3 * dy3) ** 0.5

                    polygon_length = leg1 + leg2 + leg3

                    # Approximate curve length as average of chord and polygon
                    segment_length = (chord_length + polygon_length) / 2.0
                    path_length += segment_length

                # Add 10% safety margin to ensure animation completes fully
                chart_data["path_length"] = path_length * 1.1

                # Y-axis labels
                chart_data["labels"]["y_axis"]["max"] = {
                    "value": max_latency,
                    "text": f"{int(max_latency)}ms",
                    "y": 15,
                }
                chart_data["labels"]["y_axis"]["middle"] = {
                    "value": (max_latency + min_latency) / 2,
                    "text": f"{int((max_latency + min_latency) / 2)}ms",
                    "y": 105,
                }
                chart_data["labels"]["y_axis"]["min"] = {
                    "value": min_latency,
                    "text": f"{int(min_latency)}ms",
                    "y": 195,
                }

                # X-axis labels (ISO timestamps - template will format using user settings)
                if len(history) > 0:
                    chart_data["labels"]["x_axis"]["start"] = {
                        "time": history[0]["timestamp"],
                        "x": 10,
                    }
                    chart_data["labels"]["x_axis"]["middle"] = {
                        "time": history[len(history) // 2]["timestamp"],
                        "x": 400,
                    }
                    chart_data["labels"]["x_axis"]["end"] = {
                        "time": history[-1]["timestamp"],
                        "x": 790,
                    }

        # Create 30-minute status bar (30 bars, one per minute)
        # Always fetch raw data for last 30 minutes regardless of time range
        # TODO: Investigate why bars are not showing gray for minutes without data
        #       - Template syntax changed to use `is none` and `bar['success']`
        #       - Python logic creates `{"success": None, ...}` for empty minutes
        #       - Need to verify actual rendered HTML/CSS classes and Jinja2 template caching
        #       - See CHANGELOG.md Known Issues section for full investigation status
        now = utc_now()
        minute_bars = []

        # Get last 30 minutes of raw data for status bar
        status_bar_results = (
            await CheckResultCoreService.get_raw_results_for_status_bar(db, check_id)
        )

        # Convert status bar results to dicts for Recent Events table (same format as history)
        # Show all results from last 30 minutes (not just first 20) to match status bar
        recent_results = [
            {
                "timestamp": result.timestamp.isoformat(),  # ISO string for template
                "success": result.success,
                "latency_ms": result.latency_ms,
                "error": result.error,
            }
            for result in status_bar_results
        ]

        # Group results by minute
        minute_buckets: defaultdict[int, list[RecentCheckResult]] = defaultdict(list)
        for recent in status_bar_results:
            minutes_ago = int((now - recent.timestamp).total_seconds() / 60)
            if 0 <= minutes_ago < 30:
                minute_buckets[minutes_ago].append(recent)

        # Create 30 bars (most recent first)
        for minute_offset in range(30):
            results_in_minute = minute_buckets.get(minute_offset, [])
            if results_in_minute:
                # If any check failed in this minute, mark as down
                all_success = all(r.success for r in results_in_minute)
                avg_latency = sum(r.latency_ms or 0 for r in results_in_minute) / len(
                    results_in_minute
                )
                bar: dict[str, bool | int | float | None] = {
                    "success": all_success,
                    "count": len(results_in_minute),
                    "avg_latency": round(avg_latency, 1),
                    "minutes_ago": minute_offset,
                }
                minute_bars.append(bar)
                logger.info(
                    "Minute bar built",
                    extra={
                        "minute_offset": minute_offset,
                        "all_success": all_success,
                        "success_type": type(all_success).__name__,
                        "result_count": len(results_in_minute),
                    },
                )
            else:
                # No data for this minute - show as unknown
                bar = {
                    "success": None,
                    "count": 0,
                    "avg_latency": 0,
                    "minutes_ago": minute_offset,
                }
                minute_bars.append(bar)
                logger.info(
                    "Minute bar with no data",
                    extra={
                        "minute_offset": minute_offset,
                        "success": None,
                        "result_count": 0,
                    },
                )

        logger.info(
            "Total bars created",
            extra={"bar_count": len(minute_bars)},
        )
        none_bars = [b for b in minute_bars if b["success"] is None]
        true_bars = [b for b in minute_bars if b["success"] is True]
        false_bars = [b for b in minute_bars if b["success"] is False]
        logger.info(
            "Bar breakdown",
            extra={
                "none_count": len(none_bars),
                "true_count": len(true_bars),
                "false_count": len(false_bars),
            },
        )

        # Get statistics
        stats = await CheckResultCoreService.get_check_detail_stats(db, check_id, hours)

        uptime_pct = 0.0
        if stats.total and stats.total > 0:
            uptime_pct = (stats.successful / stats.total) * 100

        # Fetch artifacts for synthetic checks (linked via check_result_id foreign key)
        artifacts = []
        if check.check_type == "synthetic":
            # Get most recent result
            latest_check_result = (
                await CheckResultCoreService.get_latest_result_for_check(db, check_id)
            )

            if latest_check_result:
                # Fetch artifacts using proper foreign key relationship
                artifacts_models = (
                    await CheckResultCoreService.get_artifacts_for_result(
                        db,
                        check_id,
                        latest_check_result.id,
                        latest_check_result.timestamp,
                    )
                )

                # Convert to dicts without binary data
                artifacts = [
                    {
                        "id": str(artifact.id),
                        "artifact_type": artifact.artifact_type,
                        "filename": artifact.filename,
                        "size_bytes": artifact.size_bytes,
                        "created_at": artifact.created_at.isoformat(),
                        # The aware datetime, formatted at the edge by the
                        # format_datetime filter so it lands in the viewer's zone.
                        "created_at_dt": artifact.created_at,
                        "size_kb": round(artifact.size_bytes / 1024, 1),
                        "check_result_id": str(artifact.check_result_id),
                        # Finished hrefs, resolved by route name.
                        "view_url": request.url_for(
                            "view_artifact", artifact_id=artifact.id
                        ).path,
                        "viewer_url": request.url_for(
                            "trace_viewer", artifact_id=artifact.id
                        ).path,
                    }
                    for artifact in artifacts_models
                ]

        dependency_info = await CheckCoreService.get_dependency_info(db, check_id)

        latest_metrics = history[0]["additional_data"] if history else {}
        return {
            **build_synthetic_view((latest_metrics or {}).get("synthetic")),
            "check_urls": build_check_urls(request, check_id, hours),
            # The parent link points at a DIFFERENT check, so it needs its own href.
            "parent_check_url": (
                f"{request.url_for('check_detail_panel', check_id=dependency_info['parent_check'].id).path}"
                f"?hours={hours}"
                if dependency_info.get("parent_check")
                else None
            ),
            "check": check,
            "agent": agent,
            "history": history,
            "recent_results": recent_results,  # Last 30min raw data for Recent Events
            "minute_bars": minute_bars,
            "hours": hours,
            "chart_data": chart_data,
            "parent_check": dependency_info["parent_check"],
            "parent_latest_result": dependency_info["parent_latest_result"],
            "dependent_count": dependency_info["dependent_count"],
            "stats": {
                "total_checks": stats.total or 0,
                "successful": stats.successful or 0,
                "failed": (stats.total or 0) - (stats.successful or 0),
                "uptime_pct": round(uptime_pct, 2),
                "avg_latency_ms": (
                    round(stats.avg_latency, 2) if stats.avg_latency else None
                ),
                "min_latency_ms": (
                    round(stats.min_latency, 2) if stats.min_latency else None
                ),
                "max_latency_ms": (
                    round(stats.max_latency, 2) if stats.max_latency else None
                ),
            },
            "artifacts": artifacts,
        }

    @staticmethod
    async def resolve_default_chart_hours(db: AsyncSession) -> int:
        """Hours of history the detail chart shows when the caller didn't pick a range.

        Reads the configured default ("4h"/"24h"/"3d"/…) and converts it to hours —
        both halves of that are presentation decisions, so they live here rather than
        being reassembled by each router that needs a default.
        """
        time_range = await SettingsCoreService.get_setting(
            db, "general.default_chart_time_range", "4h"
        )
        return CheckDetailViewService.parse_time_range_to_hours(time_range)
