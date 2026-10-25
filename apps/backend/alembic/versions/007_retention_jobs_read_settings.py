"""retention_jobs_read_settings — the artifact and notification-log cleanup jobs take their days from the settings.

The job functions now read the retention settings and take no parameters, and
the scheduler calls a job as `func(**parameters)`. So every job row that runs
one of them (whatever its job_key) loses its parameters, whatever they were; a
row left with any would fail on every run.
A description still saying "30 days" is rewritten. Downgrading gives a row with
no parameters the seeded 30 days again.

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

FUNCTIONS = ["cleanup_check_artifacts", "cleanup_notification_logs"]
SEEDED_PARAMETERS = '{"days_to_keep": 30}'

OLD_DESCRIPTIONS = {
    "cleanup_notification_logs": "Delete notification log records older than 30 days",
    "cleanup_check_artifacts": "Delete check artifacts (screenshots, logs) older than 30 days",
}
NEW_DESCRIPTIONS = {
    "cleanup_notification_logs": "Delete notification log records older than the Notification Logs Retention setting",
    "cleanup_check_artifacts": "Delete check artifacts (screenshots, logs) older than the Artifacts Retention setting",
}


def _describe(
    connection: Connection, from_: dict[str, str], to: dict[str, str]
) -> None:
    """Rewrite a description that is still `from_`'s text for its function."""
    for function_name, old in from_.items():
        connection.execute(
            sa.text(
                "UPDATE job_configurations SET description = :to "
                "WHERE function_name = :fn AND description = :from_"
            ),
            {"to": to[function_name], "fn": function_name, "from_": old},
        )


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE job_configurations "
            "SET parameters = NULL "
            "WHERE function_name IN :fns AND parameters IS NOT NULL"
        ).bindparams(sa.bindparam("fns", expanding=True)),
        {"fns": FUNCTIONS},
    )
    _describe(connection, OLD_DESCRIPTIONS, NEW_DESCRIPTIONS)


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE job_configurations SET parameters = CAST(:seeded AS jsonb) "
            "WHERE function_name IN :fns AND parameters IS NULL"
        ).bindparams(sa.bindparam("fns", expanding=True)),
        {"seeded": SEEDED_PARAMETERS, "fns": FUNCTIONS},
    )
    _describe(connection, NEW_DESCRIPTIONS, OLD_DESCRIPTIONS)
