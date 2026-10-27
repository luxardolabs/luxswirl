"""api_tokens.scopes — what a token may do, narrowing its user's role.

Scopes are the fleet's `family:action` vocabulary (app/core/authorization).
Every token that already exists keeps what it could do: it is given `*`.
A token created from now on names its scopes; the column has no default.
Downgrading drops the column, and every token can again do all its user can.

Revision ID: 8e4a2c6f1b39
Revises: 6b3d8f2a1c57
Create Date: 2026-10-27

"""

import sqlalchemy as sa
from alembic import op

revision = "8e4a2c6f1b39"
down_revision = "6b3d8f2a1c57"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "api_tokens",
        sa.Column(
            "scopes",
            sa.ARRAY(sa.String(length=64)),
            server_default=sa.text("'{*}'"),
            nullable=False,
            comment="What the token may do: family:action scopes narrowing its user's role",
        ),
    )
    op.alter_column("api_tokens", "scopes", server_default=None)


def downgrade() -> None:
    op.drop_column("api_tokens", "scopes")
