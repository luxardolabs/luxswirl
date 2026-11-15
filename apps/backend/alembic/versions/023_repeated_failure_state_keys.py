"""repeated_failure_state_keys — a repeated_failure alert's down and up are two states.

A repeated_failure alert's notifications record their state key in
`notification_logs.notification_hash`, which de-duplicates the next
notification against it. The key tells the outage (`repeated_failure:down`) from
the recovery (`repeated_failure:up`). Every row stored before carries
`repeated_failure:active`, written only by an outage notification (a recovery
was de-duplicated against it), so each becomes `repeated_failure:down`: an
alert whose outage is ongoing across the upgrade is not announced again.

Revision ID: 3e8b5c2a9d14
Revises: 9b4d6f1e3a72
Create Date: 2026-11-15

"""

from alembic import op

revision = "3e8b5c2a9d14"
down_revision = "9b4d6f1e3a72"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE notification_logs SET notification_hash = 'repeated_failure:down' "
        "WHERE notification_hash = 'repeated_failure:active'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE notification_logs SET notification_hash = 'repeated_failure:active' "
        "WHERE notification_hash IN ('repeated_failure:down', 'repeated_failure:up')"
    )
