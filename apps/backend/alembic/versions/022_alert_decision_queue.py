"""alert_decision_queue — decide alerts in a worker, not in the report.

A report stores its results and queues the key of each one an enabled alert
evaluates (`alert_decision_queue`); the alert decision worker decides them per
check, oldest first, and records in `alert_decision_cursors` the timestamp of
the newest result decided, so a result no newer (one that arrives after a newer
result was decided) is stale and never decided.

Every check starts with its cursor at the upgrade: the results stored before
it had their alerts decided by the reports that stored them, so one sent again
afterwards is stale and is never decided twice. A check created later has no
cursor until its first decision.

Revision ID: 9b4d6f1e3a72
Revises: 7c3e9a1d5b26
Create Date: 2026-11-15

"""

import sqlalchemy as sa
from alembic import op

revision = "9b4d6f1e3a72"
down_revision = "7c3e9a1d5b26"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "alert_decision_queue",
        sa.Column("check_id", sa.Uuid(), nullable=False, comment="The result's check"),
        sa.Column(
            "result_timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="The result's timestamp: with check_id, its key in check_results",
        ),
        sa.ForeignKeyConstraint(
            ["check_id"],
            ["checks.id"],
            name=op.f("fk_alert_decision_queue_check_id_checks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "check_id", "result_timestamp", name=op.f("pk_alert_decision_queue")
        ),
    )
    op.create_table(
        "alert_decision_cursors",
        sa.Column("check_id", sa.Uuid(), nullable=False, comment="The check"),
        sa.Column(
            "decided_through",
            sa.DateTime(timezone=True),
            nullable=False,
            comment=(
                "Timestamp of the newest result whose alerts are decided: a result "
                "no newer is stale and never decided"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["check_id"],
            ["checks.id"],
            name=op.f("fk_alert_decision_cursors_check_id_checks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("check_id", name=op.f("pk_alert_decision_cursors")),
    )
    op.execute(
        "INSERT INTO alert_decision_cursors (check_id, decided_through) "
        "SELECT id, now() FROM checks"
    )


def downgrade() -> None:
    op.drop_table("alert_decision_cursors")
    op.drop_table("alert_decision_queue")
