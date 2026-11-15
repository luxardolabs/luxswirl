"""agent_recovery_secret_voided_at — record that a revoke voided an agent's
recovery secret.

The shared registration key stands in for the pending credential only for an
agent that never had a recovery secret (registered by a build from before
secrets). A revoke clears the secret's hash, which left a revoked agent's row
looking like one of those. This column tells them apart: a row whose secret
was voided is spoken for by no credential.

Nothing is backfilled: a row already revoked cannot be told from one that never
had a secret.

Revision ID: 5d2b8e4f7a19
Revises: 3f9c7a1b5e82
Create Date: 2026-11-14

"""

import sqlalchemy as sa
from alembic import op

revision = "5d2b8e4f7a19"
down_revision = "3f9c7a1b5e82"
branch_labels = None
depends_on = None

_COMMENT = (
    "When a revoke voided the agent's recovery secret (NULL if none was "
    "voided): no credential speaks for the agent's pending heartbeat"
)


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column(
            "recovery_secret_voided_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=_COMMENT,
        ),
    )


def downgrade() -> None:
    op.drop_column("agents", "recovery_secret_voided_at")
