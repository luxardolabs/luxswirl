"""alerts.trigger_type comment — name the silence triggers.

The column's comment lists every trigger type the model accepts, the silence
triggers no_data and agent_offline among them. Comment only: no data or type
changes, so both directions are instant.

Revision ID: 3e7b1d9f5a24
Revises: 9a4c6e2b8d13
Create Date: 2026-10-28

"""

import sqlalchemy as sa
from alembic import op

revision = "3e7b1d9f5a24"
down_revision = "9a4c6e2b8d13"
branch_labels = None
depends_on = None

_WITHOUT_SILENCE = (
    "Type of trigger (status_change, threshold, repeated_failure, ssl_cert_expiry)"
)
_WITH_SILENCE = "Type of trigger (status_change, threshold, repeated_failure, ssl_cert_expiry, no_data, agent_offline)"


def upgrade() -> None:
    op.alter_column(
        "alerts",
        "trigger_type",
        existing_type=sa.String(length=50),
        existing_nullable=False,
        comment=_WITH_SILENCE,
        existing_comment=_WITHOUT_SILENCE,
    )


def downgrade() -> None:
    op.alter_column(
        "alerts",
        "trigger_type",
        existing_type=sa.String(length=50),
        existing_nullable=False,
        comment=_WITHOUT_SILENCE,
        existing_comment=_WITH_SILENCE,
    )
