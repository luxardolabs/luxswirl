"""silence_notifications — notification rows that no check result triggered.

The no_data and agent_offline alerts fire on the absence of data, so their
notification rows reference no check result: check_result_id and
check_result_timestamp become nullable. An agent_offline row is about an agent,
not a check, so it records the agent in agent_id, indexed with alert_id for the
per-alert, per-agent de-duplication lookup.

Revision ID: b3b0189fbbe3
Revises: 31d253521b47
Create Date: 2026-10-23

"""

import sqlalchemy as sa
from alembic import op

revision = "b3b0189fbbe3"
down_revision = "31d253521b47"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("notification_logs", "check_result_id", nullable=True)
    op.alter_column("notification_logs", "check_result_timestamp", nullable=True)
    op.add_column(
        "notification_logs",
        sa.Column(
            "agent_id",
            sa.Uuid(),
            nullable=True,
            comment="Agent an agent_offline notification is about (NULL for check notifications)",
        ),
    )
    op.create_index(
        "idx_notification_logs_alert_agent",
        "notification_logs",
        ["alert_id", "agent_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_notification_logs_alert_agent", table_name="notification_logs")
    op.drop_column("notification_logs", "agent_id")
    # Rows written without a check result cannot satisfy NOT NULL again.
    op.execute(
        "DELETE FROM notification_logs "
        "WHERE check_result_id IS NULL OR check_result_timestamp IS NULL"
    )
    op.alter_column("notification_logs", "check_result_timestamp", nullable=False)
    op.alter_column("notification_logs", "check_result_id", nullable=False)
