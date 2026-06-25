"""
Database health view service — context building for the database-health
admin pages.
"""

from typing import Any

from fastapi import Request
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user_model import User
from app.services.core.timescale_core_service import TimescaleCoreService

logger = get_logger("luxswirl.web.services.database_health")


class DatabaseHealthViewService:
    """View-layer wrapper for the database-health admin endpoints."""

    @staticmethod
    async def build_health_page_context(
        db: AsyncSession, request: Request, current_user: User
    ) -> dict[str, Any]:
        """Full-page context for /database-health."""
        health = await TimescaleCoreService.get_health_summary(db)
        return {
            "request": request,
            "current_user": current_user,
            "health": health,
            "page_title": "Database Health & Metrics",
        }

    @staticmethod
    async def build_health_refresh_context(
        db: AsyncSession, request: Request, current_user: User
    ) -> dict[str, Any]:
        """HTMX-partial context for /database-health/refresh."""
        health = await TimescaleCoreService.get_health_summary(db)
        return {
            "request": request,
            "current_user": current_user,
            "health": health,
        }

    @staticmethod
    async def get_growth_chart_data(db: AsyncSession, hours: int) -> list[dict]:
        """JSON payload for the growth chart."""
        return await TimescaleCoreService.get_growth_chart_data(db, hours)
