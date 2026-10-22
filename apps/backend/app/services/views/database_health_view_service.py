"""
Database health view service — context building for the database-health
admin pages.
"""

from typing import Any

from fastapi import Request
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.auth_schema import AuthedUser
from app.services.core.timescale_core_service import TimescaleCoreService

logger = get_logger("luxswirl.web.services.database_health")


def _chart_data_url(request: Request) -> str:
    """The growth chart's data endpoint, resolved by route name. The chart's
    script reads it from the canvas (`data-url`), so the path has one home."""
    return str(request.app.url_path_for("database_growth_chart_data"))


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
            "refresh_url": str(request.app.url_path_for("database_health_refresh")),
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
