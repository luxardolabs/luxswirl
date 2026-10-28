"""
Admin API call CRUD - the audit log of authenticated requests to /api/v1/admin.

Shaped like `MCPToolCallCRUD`, so the two surfaces read alike.
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import ColumnElement, and_, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.api_call_model import ApiCall
from app.models.api_token_model import ApiToken
from app.models.enum_model import AuditOutcome
from app.models.user_model import User
from app.schemas.api_activity_schema import DENIED_STATUSES, ActivityFilters


def _conditions(since: datetime, filters: ActivityFilters) -> list[ColumnElement[bool]]:
    """The WHERE clauses `filters` asks for, within the window from `since`."""
    conditions: list[ColumnElement[bool]] = [ApiCall.created_at >= since]
    if filters.user_id is not None:
        conditions.append(ApiCall.user_id == filters.user_id)
    if filters.api_token_id is not None:
        conditions.append(ApiCall.api_token_id == filters.api_token_id)
    if filters.route:
        conditions.append(ApiCall.route == filters.route)
    if filters.outcome is AuditOutcome.SUCCESS:
        conditions.append(ApiCall.status_code < 400)
    elif filters.outcome is AuditOutcome.ERROR:
        conditions.append(
            and_(
                ApiCall.status_code >= 400,
                ApiCall.status_code.not_in(DENIED_STATUSES),
            )
        )
    elif filters.outcome is AuditOutcome.DENIED:
        conditions.append(ApiCall.status_code.in_(DENIED_STATUSES))
    return conditions


class ApiCallCRUD:
    """Database reads and writes for the admin API call audit log."""

    @staticmethod
    async def log_call(db: AsyncSession, call: ApiCall) -> None:
        """Append one audit row. No commit: the caller's session owner commits."""
        db.add(call)
        await db.flush()

    @staticmethod
    async def delete_older_than(db: AsyncSession, cutoff: datetime) -> int:
        """Delete the calls made before `cutoff`; how many were deleted."""
        result = await db.execute(delete(ApiCall).where(ApiCall.created_at < cutoff))
        return int(getattr(result, "rowcount", 0) or 0)

    @staticmethod
    async def list_calls(
        db: AsyncSession, since: datetime, filters: ActivityFilters, limit: int
    ) -> Sequence[tuple[ApiCall, str | None, str | None]]:
        """The newest calls matching `filters`, each with its username and token name."""
        result = await db.execute(
            select(ApiCall, User.username, ApiToken.name)
            .outerjoin(User, User.id == ApiCall.user_id)
            .outerjoin(ApiToken, ApiToken.id == ApiCall.api_token_id)
            .where(*_conditions(since, filters))
            .order_by(ApiCall.created_at.desc(), ApiCall.id)
            .limit(limit)
        )
        return result.all()

    @staticmethod
    async def routes(
        db: AsyncSession, since: datetime, user_id: UUID | None
    ) -> Sequence[str]:
        """Every route called since `since` (by `user_id`'s tokens, when set)."""
        query = select(ApiCall.route).where(ApiCall.created_at >= since)
        if user_id is not None:
            query = query.where(ApiCall.user_id == user_id)
        result = await db.scalars(query.distinct().order_by(ApiCall.route))
        return result.all()

    @staticmethod
    async def callers(
        db: AsyncSession, since: datetime
    ) -> Sequence[tuple[UUID | None, str | None, UUID | None, str | None]]:
        """Each (user, token) pair with calls since `since`, with their names."""
        result = await db.execute(
            select(ApiCall.user_id, User.username, ApiToken.id, ApiToken.name)
            .outerjoin(User, User.id == ApiCall.user_id)
            .outerjoin(ApiToken, ApiToken.id == ApiCall.api_token_id)
            .where(ApiCall.created_at >= since)
            .distinct()
        )
        return result.all()
