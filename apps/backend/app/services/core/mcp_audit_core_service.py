"""
MCP audit core service - the log of every tool call an AI agent makes over /mcp.

Arguments are kept with every credential masked (a check's target or connection
string carries them) and a synthetic script by its size alone, since the log is for
seeing what an agent did, not for replaying it. Rows older than the retention are
pruned by the job purge task.
"""

from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.credential_mask import mask_credentials
from app.core.datetime_utils import utc_now
from app.crud.mcp_tool_call_crud import MCPToolCallCRUD
from app.models.enum_model import MCPDenialCode
from app.schemas.api_token_schema import ApiPrincipal

# How long a tool call stays in the audit log.
AUDIT_RETENTION = timedelta(days=90)


def masked_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """A tool call's arguments as the audit log keeps them: every credential masked,
    a synthetic script by its size alone."""
    return {key: _masked(key, value) for key, value in arguments.items()}


def _masked(key: object, value: object) -> object:
    if key == "script_code" and isinstance(value, str):
        return f"<{len(value)} characters>"
    if isinstance(value, dict):
        return {str(k): _masked(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_masked(None, item) for item in value]
    if isinstance(value, str):
        return mask_credentials(value)
    return value


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
            tool_name=tool_name,
            arguments=masked_arguments(arguments),
            success=success,
            error_message=mask_credentials(error_message),
            duration_ms=duration_ms,
            ip_address=ip_address,
            user_agent=user_agent,
            denial_code=denial_code,
        )

    @staticmethod
    async def purge_expired(db: AsyncSession) -> int:
        """Delete the calls older than the retention; how many were deleted."""
        return await MCPToolCallCRUD.delete_older_than(db, utc_now() - AUDIT_RETENTION)
