"""audit_mcp_tool_calls — one row per tool call an AI agent makes over /mcp.

The calling user and token are kept as references that null out when either is
deleted, so the audit history outlives them. Downgrading drops the table, and the
history with it.

Revision ID: 3c7e1a9d5f20
Revises: 8e4a2c6f1b39
Create Date: 2026-10-27

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "3c7e1a9d5f20"
down_revision = "8e4a2c6f1b39"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_mcp_tool_calls",
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
            "tool_name",
            sa.String(length=100),
            nullable=False,
            comment="The tool the agent called",
        ),
        sa.Column(
            "arguments",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
            comment="The call's arguments, credentials masked",
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
            comment="The calling client's own name",
        ),
        sa.Column(
            "success",
            sa.Boolean(),
            nullable=False,
            comment="Whether the tool answered without error",
        ),
        sa.Column(
            "error_message",
            sa.Text(),
            nullable=True,
            comment="What went wrong, when it did",
        ),
        sa.Column(
            "duration_ms",
            sa.Integer(),
            nullable=True,
            comment="How long the call took",
        ),
        sa.Column(
            "denial_code",
            sa.Enum(
                "unknown_tool",
                "role_insufficient",
                "scope_insufficient",
                name="mcpdenialcode",
                native_enum=False,
                length=32,
            ),
            nullable=True,
            comment="Why the gate refused the call (NULL = not a denial)",
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
    op.create_index(
        "idx_audit_mcp_tool_calls_created_at", "audit_mcp_tool_calls", ["created_at"]
    )
    op.create_index(
        "idx_audit_mcp_tool_calls_api_token",
        "audit_mcp_tool_calls",
        ["api_token_id", "created_at"],
    )
    op.create_index(
        "idx_audit_mcp_tool_calls_tool_name", "audit_mcp_tool_calls", ["tool_name"]
    )
    op.create_index(
        "idx_audit_mcp_tool_calls_denial",
        "audit_mcp_tool_calls",
        ["created_at"],
        postgresql_where=sa.text("denial_code IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_audit_mcp_tool_calls_denial", table_name="audit_mcp_tool_calls")
    op.drop_index(
        "idx_audit_mcp_tool_calls_tool_name", table_name="audit_mcp_tool_calls"
    )
    op.drop_index(
        "idx_audit_mcp_tool_calls_api_token", table_name="audit_mcp_tool_calls"
    )
    op.drop_index(
        "idx_audit_mcp_tool_calls_created_at", table_name="audit_mcp_tool_calls"
    )
    op.drop_table("audit_mcp_tool_calls")
