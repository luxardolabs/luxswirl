"""audit_api_calls — one row per authenticated admin API request.

Shaped like audit_mcp_tool_calls: the calling user and token are kept as
references that null out when either is deleted, so the audit history outlives
them. The route is the template, never the raw path; no query string or body is
kept. Downgrading drops the table, and the history with it.

Revision ID: 9a4c6e2b8d13
Revises: 5b8d2f4a7c61
Create Date: 2026-10-28

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "9a4c6e2b8d13"
down_revision = "5b8d2f4a7c61"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_api_calls",
        sa.Column(
            "user_id",
            sa.Uuid(),
            nullable=True,
            comment="The user the calling token acts as",
        ),
        sa.Column(
            "api_token_id",
            sa.Uuid(),
            nullable=True,
            comment="The calling API token",
        ),
        sa.Column(
            "method",
            sa.Enum(
                "GET",
                "HEAD",
                "POST",
                "PUT",
                "PATCH",
                "DELETE",
                "OPTIONS",
                name="httpmethod",
                native_enum=False,
                length=10,
            ),
            nullable=False,
            comment="The request's HTTP method",
        ),
        sa.Column(
            "route",
            sa.String(length=255),
            nullable=False,
            comment="The route template called, e.g. /api/v1/admin/checks/{check_id}",
        ),
        sa.Column(
            "status_code",
            sa.Integer(),
            nullable=False,
            comment="The response's HTTP status",
        ),
        sa.Column(
            "duration_ms",
            sa.Integer(),
            nullable=True,
            comment="How long the call took",
        ),
        sa.Column(
            "ip_address",
            postgresql.INET(),
            nullable=True,
            comment="Where the call came from",
        ),
        sa.Column(
            "user_agent",
            sa.Text(),
            nullable=True,
            comment="The calling client's own name, truncated",
        ),
        sa.Column("id", sa.Uuid(), nullable=False, comment="UUID primary key"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Timestamp when record was created",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Timestamp when record was last updated",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["api_token_id"], ["api_tokens.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_audit_api_calls_created_at", "audit_api_calls", ["created_at"])
    op.create_index(
        "idx_audit_api_calls_api_token",
        "audit_api_calls",
        ["api_token_id", "created_at"],
    )
    op.create_index(
        "idx_audit_api_calls_user", "audit_api_calls", ["user_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("idx_audit_api_calls_user", table_name="audit_api_calls")
    op.drop_index("idx_audit_api_calls_api_token", table_name="audit_api_calls")
    op.drop_index("idx_audit_api_calls_created_at", table_name="audit_api_calls")
    op.drop_table("audit_api_calls")
