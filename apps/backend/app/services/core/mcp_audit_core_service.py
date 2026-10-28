"""
MCP audit core service - the log of every tool call an AI agent makes over /mcp.

Arguments are kept with every credential masked (a check's target or connection
string carries them) and a synthetic script by its size alone, since the log is for
seeing what an agent did, not for replaying it. Rows older than the retention are
pruned by the job purge task.
"""

import ipaddress
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.credential_mask import mask_credentials
from app.core.datetime_utils import utc_now
from app.crud.mcp_tool_call_crud import MCPToolCallCRUD
from app.models.enum_model import MCPDenialCode
from app.models.mcp_tool_call_model import MCPToolCall
from app.schemas.api_activity_schema import ActivityCaller, ActivityFilters, McpCallRow
from app.schemas.api_token_schema import ApiPrincipal

# How long a tool call stays in the audit log.
AUDIT_RETENTION = timedelta(days=90)
# The longest tool name the audit log keeps (the column's width); an agent may send any.
TOOL_NAME_LENGTH = 100


def masked_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """A tool call's arguments as the audit log keeps them: every credential masked,
    a synthetic script by its size alone."""
    return {storable_text(key): _masked(key, value) for key, value in arguments.items()}


def _masked(key: object, value: object) -> object:
    if key == "script_code" and isinstance(value, str):
        return f"<{len(value)} characters>"
    if isinstance(value, dict):
        return {storable_text(str(k)): _masked(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_masked(None, item) for item in value]
    if isinstance(value, str):
        return storable_text(mask_credentials(value))
    return value


def storable_text(text: str) -> str:
    """`text` without the NUL characters Postgres refuses in text and JSON."""
    return text.replace("\x00", "")


def storable_address(value: str | None) -> str | None:
    """`value` when it is an IP address; None otherwise (no peer, a unix socket)."""
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


class MCPAuditCoreService:
    """Record and prune MCP tool calls."""

    @staticmethod
    async def log_call(
        db: AsyncSession,
        *,
        principal: ApiPrincipal | None,
        tool_name: str,
        arguments: dict[str, Any],
        success: bool,
        error_message: str | None,
        duration_ms: int,
        denial_code: MCPDenialCode | None,
        ip_address: str | None,
        user_agent: str | None,
    ) -> None:
        """Append one audit row for a call `principal` made."""
        await MCPToolCallCRUD.log_call(
            db,
            user_id=principal.user.id if principal else None,
            api_token_id=principal.token_id if principal else None,
            tool_name=storable_text(tool_name)[:TOOL_NAME_LENGTH],
            arguments=masked_arguments(arguments),
            success=success,
            error_message=(
                storable_text(mask_credentials(error_message))
                if error_message
                else None
            ),
            duration_ms=duration_ms,
            ip_address=storable_address(ip_address),
            user_agent=storable_text(user_agent) if user_agent else None,
            denial_code=denial_code,
        )

    @staticmethod
    async def purge_expired(db: AsyncSession) -> int:
        """Delete the calls older than the retention; how many were deleted."""
        return await MCPToolCallCRUD.delete_older_than(db, utc_now() - AUDIT_RETENTION)

    # ── Reads (the activity screens) ──────────────────────────────────────────

    @staticmethod
    async def list_calls(
        db: AsyncSession, filters: ActivityFilters, limit: int
    ) -> list[McpCallRow]:
        """The newest calls matching `filters` (one user's when `filters.user_id`)."""
        rows = await MCPToolCallCRUD.list_calls(
            db, window_start(filters.days), filters, limit
        )
        return [_row(call, username, token_name) for call, username, token_name in rows]

    @staticmethod
    async def get_call(db: AsyncSession, call_id: UUID) -> McpCallRow | None:
        """One call, its arguments masked again on the way out; None when none."""
        found = await MCPToolCallCRUD.get_call(db, call_id)
        if found is None:
            return None
        call, username, token_name = found
        return _row(call, username, token_name)

    @staticmethod
    async def tool_names(
        db: AsyncSession, days: int, user_id: UUID | None
    ) -> list[str]:
        """Every tool called in the window (by `user_id`'s tokens, when set)."""
        return list(await MCPToolCallCRUD.tool_names(db, window_start(days), user_id))

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
            for user_id, username, token_id, token_name in await MCPToolCallCRUD.callers(
                db, window_start(days)
            )
        ]


def window_start(days: int) -> datetime:
    """When an activity window of `days` days opens."""
    return utc_now() - timedelta(days=days)


def _row(call: MCPToolCall, username: str | None, token_name: str | None) -> McpCallRow:
    return McpCallRow(
        id=call.id,
        created_at=call.created_at,
        user_id=call.user_id,
        username=username,
        api_token_id=call.api_token_id,
        token_name=token_name,
        tool_name=call.tool_name,
        success=call.success,
        denial_code=call.denial_code,
        error_message=call.error_message,
        duration_ms=call.duration_ms,
        ip_address=str(call.ip_address) if call.ip_address is not None else None,
        user_agent=call.user_agent,
        # Masked when written; masked again here, so a row written before a masking
        # rule existed never shows a credential.
        arguments=masked_arguments(call.arguments or {}),
    )
