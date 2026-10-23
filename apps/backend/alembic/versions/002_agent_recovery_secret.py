"""agent_recovery_secret — bind key recovery to a per-agent secret.

The registration key is shared by every agent install, so on its own it cannot
prove which agent is recovering. Registration issues each agent a random recovery secret, returned once and
stored here as its SHA-256 hex digest; recover-key needs it.

Agents registered before this revision have no hash (NULL) and cannot recover:
they re-register and are approved again. Nothing is backfilled, because no
secret exists to hash.

Revision ID: 2c806c09128d
Revises: c24bfe691550
Create Date: 2026-10-22

"""

import sqlalchemy as sa
from alembic import op

revision = "2c806c09128d"
down_revision = "c24bfe691550"
branch_labels = None
depends_on = None

_COMMENT = (
    "SHA-256 hex of the per-agent recovery secret issued at registration "
    "(NULL for agents registered before it existed: they cannot recover)"
)


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column(
            "recovery_secret_hash",
            sa.String(length=64),
            nullable=True,
            comment=_COMMENT,
        ),
    )


def downgrade() -> None:
    op.drop_column("agents", "recovery_secret_hash")
