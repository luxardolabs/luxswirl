"""api_tokens — each user's bearer tokens for the admin API.

A token belongs to one user and is deleted with them. Only its SHA-256 is kept.
Downgrading drops the table, and every token with it.

Revision ID: 6b3d8f2a1c57
Revises: 2c6f9e1d4b83
Create Date: 2026-10-26

"""

import sqlalchemy as sa
from alembic import op

revision = "6b3d8f2a1c57"
down_revision = "2c6f9e1d4b83"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_tokens",
        sa.Column(
            "user_id",
            sa.Uuid(),
            nullable=False,
            comment="The user this token acts as",
        ),
        sa.Column(
            "name",
            sa.String(length=100),
            nullable=False,
            comment="What the token is for, named by its owner",
        ),
        sa.Column(
            "token_hash",
            sa.String(length=64),
            nullable=False,
            comment="SHA-256 hash of the token",
        ),
        sa.Column(
            "token_prefix",
            sa.String(length=12),
            nullable=False,
            comment="The token's first characters, to recognise it in a list",
        ),
        sa.Column(
            "last_used_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When this token last authenticated a request",
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When this token stops working (NULL = never)",
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When this token was revoked (NULL = not revoked)",
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_api_tokens_user_id", "api_tokens", ["user_id"], unique=False)
    op.create_index(
        op.f("ix_api_tokens_token_hash"), "api_tokens", ["token_hash"], unique=True
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_api_tokens_token_hash"), table_name="api_tokens")
    op.drop_index("idx_api_tokens_user_id", table_name="api_tokens")
    op.drop_table("api_tokens")
