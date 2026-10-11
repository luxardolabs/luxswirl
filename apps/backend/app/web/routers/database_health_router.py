"""
Database Health router — admin monitoring & metrics for the database.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.services.views.database_health_view_service import DatabaseHealthViewService
from app.web.deps import AdminUserWeb
from app.web.templates import templates

logger = get_logger("luxswirl.web.database_health")

router = APIRouter(tags=["Web UI - Database Health"], include_in_schema=False)


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.get("/database-health", response_class=HTMLResponse)
async def database_health_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: AdminUserWeb,
):
    """Database health page — comprehensive database metrics and monitoring."""
    context = await DatabaseHealthViewService.build_health_page_context(
        db, request, current_user
    )
    return templates.TemplateResponse(request, "pages/database_health.html", context)


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.get("/database-health/refresh", response_class=HTMLResponse)
async def database_health_refresh(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: AdminUserWeb,
):
    """HTMX endpoint — refresh database health metrics."""
    context = await DatabaseHealthViewService.build_health_refresh_context(
        db, request, current_user
    )
    return templates.TemplateResponse(request, "partials/database_health.html", context)
