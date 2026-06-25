"""
Database Health router — admin monitoring & metrics for the database.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_deps import AdminUserWeb
from app.db import get_db
from app.services.views.database_health_view_service import DatabaseHealthViewService
from app.web.templates_config import templates

logger = get_logger("luxswirl.web.database_health")

router = APIRouter(tags=["Web UI - Database Health"], include_in_schema=False)


@router.get("/database-health", response_class=HTMLResponse)
async def database_health_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: AdminUserWeb,
):
    """Database health page — comprehensive database metrics and monitoring."""
    try:
        context = await DatabaseHealthViewService.build_health_page_context(
            db, request, current_user
        )
        return templates.TemplateResponse(request, "pages/database_health.html", context)
    except Exception as e:
        logger.error("Error getting database health", exc_info=True)
        return templates.TemplateResponse(
            request,
            "pages/error.html",
            {
                "current_user": current_user,
                "error": str(e),
                "page_title": "Error",
            },
            status_code=500,
        )


@router.get("/database-health/refresh", response_class=HTMLResponse)
async def database_health_refresh(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: AdminUserWeb,
):
    """HTMX endpoint — refresh database health metrics."""
    try:
        context = await DatabaseHealthViewService.build_health_refresh_context(
            db, request, current_user
        )
        return templates.TemplateResponse(request, "partials/database_health.html", context)
    except Exception as e:
        logger.error("Error getting database health", exc_info=True)
        return HTMLResponse(
            content=f'<div class="text-red-500">Error loading database health: {str(e)}</div>',
            status_code=500,
        )


@router.get("/database-health/chart-data")
async def database_growth_chart_data(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: AdminUserWeb,
    hours: Annotated[int, Query(ge=1, le=8760, description="Hours of data to fetch")] = 168,
):
    """JSON endpoint — get database growth chart data."""
    try:
        data = await DatabaseHealthViewService.get_growth_chart_data(db, hours)
        return JSONResponse(content={"data": data})
    except Exception as e:
        logger.error("Error getting growth chart data", exc_info=True)
        return JSONResponse(content={"error": str(e)}, status_code=500)
