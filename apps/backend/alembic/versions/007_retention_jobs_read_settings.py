"""retention_jobs_read_settings — the artifact and notification-log cleanup jobs take their days from the settings.

The jobs were seeded with `parameters = {"days_to_keep": 30}`, and job
configurations are seeded once and never overwritten. The job functions now
read the retention settings and take no parameters, so the seeded parameter is
dropped from existing rows, and a description still saying "30 days" is
rewritten. Only the seeded value and text are touched.

Revision ID: 2c6f9e1d4b83
Revises: 8a4e2c7b1f90
Create Date: 2026-10-25

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision = "2c6f9e1d4b83"
down_revision = "8a4e2c7b1f90"
branch_labels = None
depends_on = None

SEEDED_PARAMETERS = '{"days_to_keep": 30}'

OLD_DESCRIPTIONS = {
    "cleanup_notification_logs": "Delete notification log records older than 30 days",
    "cleanup_check_artifacts": "Delete check artifacts (screenshots, logs) older than 30 days",
}
NEW_DESCRIPTIONS = {
    "cleanup_notification_logs": "Delete notification log records older than the Notification Logs Retention setting",
    "cleanup_check_artifacts": "Delete check artifacts (screenshots, logs) older than the Artifacts Retention setting",
}


def _rewrite(connection: Connection, job_key: str, *, upgrade: bool) -> None:
    if upgrade:
        connection.execute(
            sa.text(
                "UPDATE job_configurations SET parameters = NULL "
                "WHERE job_key = :key AND parameters = CAST(:seeded AS jsonb)"
            ),
            {"key": job_key, "seeded": SEEDED_PARAMETERS},
        )
        from_, to = OLD_DESCRIPTIONS[job_key], NEW_DESCRIPTIONS[job_key]
    else:
        connection.execute(
            sa.text(
                "UPDATE job_configurations SET parameters = CAST(:seeded AS jsonb) "
                "WHERE job_key = :key AND parameters IS NULL"
            ),
            {"key": job_key, "seeded": SEEDED_PARAMETERS},
        )
        from_, to = NEW_DESCRIPTIONS[job_key], OLD_DESCRIPTIONS[job_key]
    connection.execute(
        sa.text(
            "UPDATE job_configurations SET description = :to "
            "WHERE job_key = :key AND description = :from_"
        ),
        {"to": to, "key": job_key, "from_": from_},
    )


def upgrade() -> None:
    for job_key in OLD_DESCRIPTIONS:
        _rewrite(op.get_bind(), job_key, upgrade=True)


def downgrade() -> None:
    for job_key in OLD_DESCRIPTIONS:
        _rewrite(op.get_bind(), job_key, upgrade=False)
