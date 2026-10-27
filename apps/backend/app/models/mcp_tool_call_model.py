"""
MCP tool call model - the audit log of every call an AI agent makes over /mcp.

Port of LuxWX's `MCPToolCall` (LuxPM's `mcp_tool_calls`).
"""

from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, Uuid, text
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base_model import UUIDBaseModel, str_enum
from app.models.enum_model import MCPDenialCode


class MCPToolCall(UUIDBaseModel):
    """One row per MCP tool call, whether it succeeded, errored, or was denied.

    Written from the MCP server's `finally` block on the middleware-owned session, so
    an agent's traffic is reconstructible per user and per token. `created_at` is when
    the call was made.
    """

    __tablename__ = "audit_mcp_tool_calls"
    __table_args__ = (
        Index("idx_audit_mcp_tool_calls_created_at", "created_at"),
        Index("idx_audit_mcp_tool_calls_api_token", "api_token_id", "created_at"),
        Index("idx_audit_mcp_tool_calls_tool_name", "tool_name"),
        # The denials view scans by time for rows that ARE denials. Partial on
        # denial_code so it indexes only the (small) denial set, not every call.
        Index(
            "idx_audit_mcp_tool_calls_denial",
            "created_at",
            postgresql_where=text("denial_code IS NOT NULL"),
        ),
    )

    # Who called. Both null out so history survives the token or user being deleted.
    user_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="The user the calling token acts as",
    )
    api_token_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("api_tokens.id", ondelete="SET NULL"),
        nullable=True,
        comment="The calling API token",
    )

    # What was called
    tool_name: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="The tool the agent called"
    )
    # Tool arguments as sent by the agent, credentials masked. Shape varies per tool,
    # so there is no single schema to model: an explicit dict[str, Any] is the honest shape.
    arguments: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="{}",
        comment="The call's arguments, credentials masked",
    )

    # Origin. Security telemetry, not analytics: this credential lives on a third
    # party's machine, so "where did this call come from" is how a leaked token
    # becomes visible. Nullable: a call with no resolvable peer must still be audited.
    ip_address: Mapped[str | None] = mapped_column(
        INET, nullable=True, comment="Where the call came from"
    )
    user_agent: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="The calling client's own name"
    )

    # Outcome
    success: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="Whether the tool answered without error"
    )
    error_message: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="What went wrong, when it did"
    )
    duration_ms: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="How long the call took"
    )
    # Why it was denied — NULL for anything that wasn't a denial (a success, or a plain
    # tool error like a bad argument). `denial_code IS NOT NULL` is the exact predicate
    # for "this was a denial"; error_message keeps the human detail.
    denial_code: Mapped[MCPDenialCode | None] = mapped_column(
        str_enum(MCPDenialCode, 32),
        nullable=True,
        comment="Why the gate refused the call (NULL = not a denial)",
    )
