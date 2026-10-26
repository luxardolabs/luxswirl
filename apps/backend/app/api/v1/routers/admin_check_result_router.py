"""
Admin API check result router - a check's results, history and summary.

Every route reads, so any API token may call it; a viewer's reads are masked
by the core service.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ApiUser
from app.core.config import settings
from app.core.rate_limit import api_token_rate_limit_key, limiter
from app.db import get_db
from app.schemas.admin_check_result_schema import (
    AdminCheckResultDetail,
    CheckHistory,
    CheckResultPage,
    CheckSummary,
)
from app.services.core.admin_check_result_core_service import (
    AdminCheckResultCoreService,
)

router = APIRouter(prefix="/checks", tags=["Admin API: Check results"])

LIMIT = settings.security.admin_api_rate_limit

# How far back a history or summary reads: an hour to 30 days.
Hours = Annotated[int, Query(ge=1, le=720)]


@router.get(
    "/{check_id}/results",
    response_model=CheckResultPage,
    name="admin_list_check_results",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_list_check_results(
    request: Request,
    check_id: UUID,
    principal: ApiUser,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    since: datetime | None = None,
    until: datetime | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=200)] = 50,
) -> CheckResultPage:
    """One page of the check's results, newest first: the last 24 hours unless
    `since`/`until` say otherwise (at most 31 days)."""
    return await AdminCheckResultCoreService.list_results(
        db,
        principal.user.role,
        check_id,
        since=since,
        until=until,
        page=page,
        per_page=per_page,
    )


@router.get(
    "/{check_id}/results/{result_id}",
    response_model=AdminCheckResultDetail,
    name="admin_get_check_result",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_get_check_result(
    request: Request,
    check_id: UUID,
    result_id: UUID,
    principal: ApiUser,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> AdminCheckResultDetail:
    """One result of the check, with its response body and artifacts' metadata."""
    return await AdminCheckResultCoreService.get_result(
        db, principal.user.role, check_id, result_id
    )


@router.get(
    "/{check_id}/history",
    response_model=CheckHistory,
    name="admin_get_check_history",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_get_check_history(
    request: Request,
    check_id: UUID,
    principal: ApiUser,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    hours: Hours = 24,
) -> CheckHistory:
    """The check's results over the last `hours`, in time buckets."""
    return await AdminCheckResultCoreService.history(
        db, principal.user.role, check_id, hours
    )


@router.get(
    "/{check_id}/summary",
    response_model=CheckSummary,
    name="admin_get_check_summary",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_get_check_summary(
    request: Request,
    check_id: UUID,
    principal: ApiUser,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    hours: Hours = 24,
) -> CheckSummary:
    """Counts, uptime and latency over the last `hours`, and the latest result."""
    return await AdminCheckResultCoreService.summary(
        db, principal.user.role, check_id, hours
    )
