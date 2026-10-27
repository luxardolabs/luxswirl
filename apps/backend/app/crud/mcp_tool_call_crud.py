"""
MCP tool call CRUD - the audit log of agent calls over /mcp.

`log_call` is ported from LuxWX's `mcp_tool_call_crud.log_call`.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enum_model import MCPDenialCode
from app.models.mcp_tool_call_model import MCPToolCall


class MCPToolCallCRUD:
    """Database writes for the MCP tool-call audit log."""

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
