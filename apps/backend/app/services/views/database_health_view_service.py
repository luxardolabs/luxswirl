"""
Database health view service — context building for the database-health
admin pages.
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.auth_schema import AuthedUser
from app.schemas.setting_schema import DatabaseGrowthResponse
from app.services.core.timescale_core_service import TimescaleCoreService
from app.shared.logger import get_logger

logger = get_logger("luxswirl.web.services.database_health")


def _chart_data_url(request: Request) -> str:
    """The growth chart's data endpoint, resolved by route name. The chart's
    script reads it from the canvas (`data-url`), so the path has one home."""
    return str(request.app.url_path_for("database_health.chart_data"))


class DatabaseHealthViewService:
    """View-layer wrapper for the database-health admin endpoints."""

    @staticmethod
    async def build_health_page_context(
        db: AsyncSession, request: Request, current_user: AuthedUser
    ) -> dict[str, Any]:
        """Full-page context for /database-health."""
        health = await TimescaleCoreService.get_health_summary(db)
        return {
            "request": request,
            "current_user": current_user,
            "health": health,
            "page_title": "Database Health & Metrics",
            "refresh_url": str(request.app.url_path_for("database_health.refresh")),
            "chart_data_url": _chart_data_url(request),
        }

    @staticmethod
    async def build_health_refresh_context(
        db: AsyncSession, request: Request, current_user: AuthedUser
    ) -> dict[str, Any]:
        """HTMX-partial context for /database-health/refresh."""
        health = await TimescaleCoreService.get_health_summary(db)
        return {
            "request": request,
            "current_user": current_user,
            "health": health,
            "chart_data_url": _chart_data_url(request),
        }

    @staticmethod
    async def build_growth_chart_context(
        db: AsyncSession, request: Request, hours: int
    ) -> dict[str, Any]:
        """Context for the growth chart's data fragment: storage by table category.

        The points ride in the fragment's markup (a data attribute), which the
        chart's script parses; the route stays an HTML route.
        """
        data = await TimescaleCoreService.get_growth_chart_data(db, hours)
        chart = DatabaseGrowthResponse.model_validate({"data": data})
        return {
            "request": request,
            "chart_points": [point.model_dump() for point in chart.data],
        }
