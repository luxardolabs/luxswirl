"""
Admin API audit core service - the log of every authenticated request to
/api/v1/admin, as `MCPAuditCoreService` is for /mcp.

A row keeps who called, the method, the route template, the status and how long it
took; never the raw path, the query string or the body. Rows older than the
retention are pruned by the job purge task, with the MCP rows.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.datetime_utils import utc_now
from app.crud.api_call_crud import ApiCallCRUD
from app.models.api_call_model import ApiCall
from app.models.enum_model import HttpMethod
from app.schemas.api_activity_schema import ActivityCaller, ActivityFilters, ApiCallRow
from app.services.core.mcp_audit_core_service import (
    AUDIT_RETENTION,
    storable_address,
    storable_text,
    window_start,
)

# The longest client name kept: enough to tell clients apart, never a payload.
USER_AGENT_LENGTH = 256
# The route column's width.
ROUTE_LENGTH = 255


class ApiAuditCoreService:
    """Record, read and prune admin API calls."""

    @staticmethod
    async def log_call(
        db: AsyncSession,
        *,
        user_id: UUID | None,
        api_token_id: UUID | None,
        method: HttpMethod,
        route: str,
        status_code: int,
        duration_ms: int,
        ip_address: str | None,
        user_agent: str | None,
    ) -> None:
        """Append one audit row for a call the token `api_token_id` made."""
        await ApiCallCRUD.log_call(
            db,
            ApiCall(
                user_id=user_id,
                api_token_id=api_token_id,
                method=method,
                route=storable_text(route)[:ROUTE_LENGTH],
                status_code=status_code,
                duration_ms=duration_ms,
                ip_address=storable_address(ip_address),
                user_agent=(
                    storable_text(user_agent)[:USER_AGENT_LENGTH]
                    if user_agent
                    else None
                ),
            ),
        )

    @staticmethod
    async def purge_expired(db: AsyncSession) -> int:
        """Delete the calls older than the retention; how many were deleted."""
        return await ApiCallCRUD.delete_older_than(db, utc_now() - AUDIT_RETENTION)

    @staticmethod
    async def list_calls(
        db: AsyncSession, filters: ActivityFilters, limit: int
    ) -> list[ApiCallRow]:
        """The newest calls matching `filters` (one user's when `filters.user_id`)."""
        rows = await ApiCallCRUD.list_calls(
            db, window_start(filters.days), filters, limit
        )
        return [
            ApiCallRow(
                id=call.id,
                created_at=call.created_at,
                user_id=call.user_id,
                username=username,
                api_token_id=call.api_token_id,
                token_name=token_name,
                method=call.method,
                route=call.route,
                status_code=call.status_code,
                duration_ms=call.duration_ms,
                ip_address=(
                    str(call.ip_address) if call.ip_address is not None else None
                ),
                user_agent=call.user_agent,
            )
            for call, username, token_name in rows
        ]

    @staticmethod
    async def routes(db: AsyncSession, days: int, user_id: UUID | None) -> list[str]:
        """Every route called in the window (by `user_id`'s tokens, when set)."""
        return list(await ApiCallCRUD.routes(db, window_start(days), user_id))

    @staticmethod
    async def callers(db: AsyncSession, days: int) -> list[ActivityCaller]:
        """Each user and token pair with calls in the window."""
        return [
            ActivityCaller(
                user_id=user_id,
                username=username,
                api_token_id=token_id,
                token_name=token_name,
            )
            for user_id, username, token_id, token_name in await ApiCallCRUD.callers(
                db, window_start(days)
            )
        ]
