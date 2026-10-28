"""
Pydantic schemas for the API activity screens: the audited MCP tool calls and
admin API requests, as the admin page and My Account read them.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models.enum_model import AuditOutcome, HttpMethod, MCPDenialCode

# The statuses that mean the API refused the token rather than failed the call.
DENIED_STATUSES: tuple[int, ...] = (401, 403)


class ActivityFilters(BaseModel):
    """What narrows an activity list. `user_id` None is every user's calls."""

    model_config = ConfigDict(frozen=True)

    days: int
    user_id: UUID | None = None
    api_token_id: UUID | None = None
    tool_name: str | None = None
    route: str | None = None
    outcome: AuditOutcome | None = None


class McpCallRow(BaseModel):
    """One audited MCP tool call, with its user's and token's names."""

    id: UUID
    created_at: datetime
    user_id: UUID | None
    username: str | None
    api_token_id: UUID | None
    token_name: str | None
    tool_name: str
    success: bool
    denial_code: MCPDenialCode | None
    error_message: str | None
    duration_ms: int | None
    ip_address: str | None
    user_agent: str | None
    arguments: dict[str, Any]

    @property
    def outcome(self) -> AuditOutcome:
        """How the call ended: refused, failed, or answered."""
        if self.denial_code is not None:
            return AuditOutcome.DENIED
        return AuditOutcome.SUCCESS if self.success else AuditOutcome.ERROR


class ApiCallRow(BaseModel):
    """One audited admin API request, with its user's and token's names."""

    id: UUID
    created_at: datetime
    user_id: UUID | None
    username: str | None
    api_token_id: UUID | None
    token_name: str | None
    method: HttpMethod
    route: str
    status_code: int
    duration_ms: int | None
    ip_address: str | None
    user_agent: str | None

    @property
    def outcome(self) -> AuditOutcome:
        """How the call ended: refused (401/403), failed (any other 4xx/5xx), or
        answered."""
        if self.status_code in DENIED_STATUSES:
            return AuditOutcome.DENIED
        return AuditOutcome.ERROR if self.status_code >= 400 else AuditOutcome.SUCCESS


class ActivityCaller(BaseModel):
    """A user and token pair with calls in the window, with their names (None:
    deleted since)."""

    user_id: UUID | None
    username: str | None
    api_token_id: UUID | None
    token_name: str | None


class ActivityOption(BaseModel):
    """One choice in a filter dropdown."""

    value: str
    label: str
