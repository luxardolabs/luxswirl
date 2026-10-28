"""
MCP tool call CRUD - the audit log of agent calls over /mcp.

`log_call` is ported from LuxWX's `mcp_tool_call_crud.log_call`; the reads behind
the activity screens from LuxWX's MCP usage queries.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, and_, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.api_token_model import ApiToken
from app.models.enum_model import AuditOutcome, MCPDenialCode
from app.models.mcp_tool_call_model import MCPToolCall
from app.models.user_model import User
from app.schemas.api_activity_schema import ActivityFilters


def _conditions(since: datetime, filters: ActivityFilters) -> list[ColumnElement[bool]]:
    """The WHERE clauses `filters` asks for, within the window from `since`."""
    conditions: list[ColumnElement[bool]] = [MCPToolCall.created_at >= since]
    if filters.user_id is not None:
        conditions.append(MCPToolCall.user_id == filters.user_id)
    if filters.api_token_id is not None:
        conditions.append(MCPToolCall.api_token_id == filters.api_token_id)
    if filters.tool_name:
        conditions.append(MCPToolCall.tool_name == filters.tool_name)
    if filters.outcome is AuditOutcome.SUCCESS:
        conditions.append(MCPToolCall.success.is_(True))
    elif filters.outcome is AuditOutcome.ERROR:
        conditions.append(
            and_(MCPToolCall.success.is_(False), MCPToolCall.denial_code.is_(None))
        )
    elif filters.outcome is AuditOutcome.DENIED:
        conditions.append(MCPToolCall.denial_code.is_not(None))
    return conditions


class MCPToolCallCRUD:
    """Database reads and writes for the MCP tool-call audit log."""

    @staticmethod
    async def log_call(
        db: AsyncSession,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        success: bool,
        user_id: UUID | None = None,
        api_token_id: UUID | None = None,
        error_message: str | None = None,
        duration_ms: int | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        denial_code: MCPDenialCode | None = None,
    ) -> None:
        """Append one audit row.

        No commit: the MCP auth middleware owns the transaction and commits once the
        response is served. Callers wrap this in the poison-safe try/rollback so a failed
        audit write can never swallow a good tool result.
        """
        db.add(
            MCPToolCall(
                user_id=user_id,
                api_token_id=api_token_id,
                tool_name=tool_name,
                arguments=arguments,
                success=success,
                error_message=error_message,
                duration_ms=duration_ms,
                ip_address=ip_address,
                user_agent=user_agent,
                denial_code=denial_code,
            )
        )
        await db.flush()

    @staticmethod
    async def delete_older_than(db: AsyncSession, cutoff: datetime) -> int:
        """Delete the calls made before `cutoff`; how many were deleted."""
        result = await db.execute(
            delete(MCPToolCall).where(MCPToolCall.created_at < cutoff)
        )
        return int(getattr(result, "rowcount", 0) or 0)

    # ── Reads (the activity screens) ──────────────────────────────────────────

    @staticmethod
    async def list_calls(
        db: AsyncSession, since: datetime, filters: ActivityFilters, limit: int
    ) -> Sequence[tuple[MCPToolCall, str | None, str | None]]:
        """The newest calls matching `filters`, each with its username and token name."""
        result = await db.execute(
            select(MCPToolCall, User.username, ApiToken.name)
            .outerjoin(User, User.id == MCPToolCall.user_id)
            .outerjoin(ApiToken, ApiToken.id == MCPToolCall.api_token_id)
            .where(*_conditions(since, filters))
            .order_by(MCPToolCall.created_at.desc(), MCPToolCall.id)
            .limit(limit)
        )
        return result.all()

    @staticmethod
    async def get_call(
        db: AsyncSession, call_id: UUID
    ) -> tuple[MCPToolCall, str | None, str | None] | None:
        """One call with its username and token name; None when there is none."""
        result = await db.execute(
            select(MCPToolCall, User.username, ApiToken.name)
            .outerjoin(User, User.id == MCPToolCall.user_id)
            .outerjoin(ApiToken, ApiToken.id == MCPToolCall.api_token_id)
            .where(MCPToolCall.id == call_id)
        )
        return result.first()

    @staticmethod
    async def tool_names(
        db: AsyncSession, since: datetime, user_id: UUID | None
    ) -> Sequence[str]:
        """Every tool called since `since` (by `user_id`'s tokens, when set)."""
        query = select(MCPToolCall.tool_name).where(MCPToolCall.created_at >= since)
        if user_id is not None:
            query = query.where(MCPToolCall.user_id == user_id)
        result = await db.scalars(query.distinct().order_by(MCPToolCall.tool_name))
        return result.all()

    @staticmethod
    async def callers(
        db: AsyncSession, since: datetime
    ) -> Sequence[tuple[UUID | None, str | None, UUID | None, str | None]]:
        """Each (user, token) pair with calls since `since`, with their names."""
        result = await db.execute(
            select(MCPToolCall.user_id, User.username, ApiToken.id, ApiToken.name)
            .outerjoin(User, User.id == MCPToolCall.user_id)
            .outerjoin(ApiToken, ApiToken.id == MCPToolCall.api_token_id)
            .where(MCPToolCall.created_at >= since)
            .distinct()
        )
        return result.all()
