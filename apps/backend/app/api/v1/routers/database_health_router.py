"""
Database health API endpoints.

The growth chart is a JSON contract consumed by app/static/js/database-health.js.
It lives here, not in the web router, because a web router serves HTML only
(fw.web_routers_single_surface) — one router, one surface.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AdminUser
from app.db import get_db
from app.schemas.setting_schema import DatabaseGrowthResponse
from app.services.core.timescale_core_service import TimescaleCoreService

logger = get_logger("luxswirl.api.database_health")

router = APIRouter(tags=["Database Health"])


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.get("/database-health/chart-data", response_model=DatabaseGrowthResponse)
async def database_growth_chart_data(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: AdminUser,
    hours: Annotated[
        int, Query(ge=1, le=8760, description="Hours of data to fetch")
    ] = 168,
) -> DatabaseGrowthResponse:
    """Storage growth by table category over the requested window."""
    data = await TimescaleCoreService.get_growth_chart_data(db, hours)
    return DatabaseGrowthResponse.model_validate({"data": data})
