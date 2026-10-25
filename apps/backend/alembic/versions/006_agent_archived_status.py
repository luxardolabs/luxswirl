"""agent_archived_status — the approval status column's comment names `archived`.

The column is a plain VARCHAR (no native enum, no CHECK constraint), so the new
status needs no schema change; only the column comment that lists the statuses.
Downgrading makes every archived agent disabled, a status the earlier code reads.

Revision ID: 8a4e2c7b1f90
Revises: 5d1f0c2a9e47
Create Date: 2026-10-25

"""

import sqlalchemy as sa
from alembic import op

revision = "8a4e2c7b1f90"
down_revision = "5d1f0c2a9e47"
branch_labels = None
depends_on = None

OLD = "Approval status: pending, active, paused, disabled, rejected"
NEW = "Approval status: pending, active, paused, disabled, archived, rejected"


def _comment(comment: str, existing: str) -> None:
    op.alter_column(
        "agents",
        "approval_status",
        existing_type=sa.String(length=20),
        existing_nullable=False,
        existing_server_default="pending",
        comment=comment,
        existing_comment=existing,
    )


def upgrade() -> None:
    _comment(NEW, OLD)


def downgrade() -> None:
    op.execute(
        "UPDATE agents SET approval_status = 'disabled' "
        "WHERE approval_status = 'archived'"
    )
    _comment(OLD, NEW)
