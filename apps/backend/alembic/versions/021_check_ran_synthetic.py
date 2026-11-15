"""check_ran_synthetic — record that a check is or has ever been synthetic.

A synthetic check's results are a script's output about the pages it opened, and
a reader who is not shown the script (a viewer) reads them as the type of what
failed alone. A check retyped away from synthetic keeps those results, so the
reading is decided by whether the check ever ran a script, not by its type now.

Backfilled for every check that is synthetic now, and for every other check with
a stored result whose metrics name a synthetic section (one retyped earlier).

Revision ID: 7c3e9a1d5b26
Revises: 7c3e9a2d4b61
Create Date: 2026-11-15

"""

import sqlalchemy as sa
from alembic import op

revision = "7c3e9a1d5b26"
down_revision = "7c3e9a2d4b61"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "checks",
        sa.Column(
            "ran_synthetic",
            sa.Boolean(),
            nullable=False,
            server_default="false",
            comment="Whether the check is or has ever been a synthetic check",
        ),
    )
    op.execute(
        "UPDATE checks SET ran_synthetic = true WHERE check_type = 'synthetic' "
        "OR EXISTS (SELECT 1 FROM check_results r WHERE r.check_id = checks.id "
        "AND r.metrics LIKE '%\"synthetic\"%')"
    )


def downgrade() -> None:
    op.drop_column("checks", "ran_synthetic")
