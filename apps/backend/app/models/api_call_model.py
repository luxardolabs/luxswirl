"""
Admin API call model - the audit log of every authenticated request to /api/v1/admin.

Shaped like `MCPToolCall` (`audit_mcp_tool_calls`), so the two token surfaces read
alike on the activity screens.
"""

from uuid import UUID

from sqlalchemy import ForeignKey, Index, Integer, String, Text, Uuid
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base_model import UUIDBaseModel, str_enum
from app.models.enum_model import HttpMethod


class ApiCall(UUIDBaseModel):
    """One row per authenticated admin API request, whatever its status.

    Written after the response, in a transaction of its own, so a request's own
    rollback never loses its row and a failed write never fails the request. Keeps
    the route TEMPLATE, never the raw path, and never the query string or the body.
    `created_at` is when the call was made.
    """

    __tablename__ = "audit_api_calls"
    __table_args__ = (
        Index("idx_audit_api_calls_created_at", "created_at"),
        Index("idx_audit_api_calls_api_token", "api_token_id", "created_at"),
        Index("idx_audit_api_calls_user", "user_id", "created_at"),
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
    method: Mapped[HttpMethod] = mapped_column(
        str_enum(HttpMethod, 10), nullable=False, comment="The request's HTTP method"
    )
    route: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="The route template called, e.g. /api/v1/admin/checks/{check_id}",
    )

    # Outcome
    status_code: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="The response's HTTP status"
    )
    duration_ms: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="How long the call took"
    )

    # Origin: where a leaked token is being used from.
    ip_address: Mapped[str | None] = mapped_column(
        INET, nullable=True, comment="Where the call came from"
    )
    user_agent: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="The calling client's own name, truncated"
    )
