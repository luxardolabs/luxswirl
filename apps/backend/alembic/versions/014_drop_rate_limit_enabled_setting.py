"""settings — remove security.rate_limit_enabled.

The rate limits on routes that check a password or key cannot be switched off,
so the setting governs nothing and leaves the Settings page. Downgrading
changes nothing: the setting's seed belongs to the version that has it, and
that version's startup creates the row again.

Revision ID: 7c2e5a9d4b18
Revises: 3e7b1d9f5a24
Create Date: 2026-10-28

"""

import sqlalchemy as sa
from alembic import op

revision = "7c2e5a9d4b18"
down_revision = "3e7b1d9f5a24"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text("DELETE FROM settings WHERE key = :key").bindparams(
            key="security.rate_limit_enabled"
        )
    )


def downgrade() -> None:
    pass
