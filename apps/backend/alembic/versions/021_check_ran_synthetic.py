"""check_ran_synthetic — record that a check is or has ever been synthetic.

A synthetic check's results are a script's output about the pages it opened, and
a reader who is not shown the script (a viewer) reads them as the type of what
failed alone. A check retyped away from synthetic keeps those results, so the
reading is decided by whether the check ever ran a script, not by its type now.

Backfilled for every check that is synthetic now, every check that still holds a
script or owns an artifact (only a synthetic run uploads a screenshot or a trace),
and every other check with a stored result that a synthetic run wrote (one retyped
earlier):

- its metrics are a JSON object with a top-level `synthetic` key. Only the top
  level: an http check's metrics carry its response headers, whose values can be
  the word `synthetic`. A row PostgreSQL cannot read as JSON counts only when it
  opens with that key, as the agent's metrics do stored.
- or its error starts as the agent writes a synthetic run's failure
  (`SYNTHETIC_ERROR_STARTS`, app/core/synthetic_result.py, as of this revision). A
  run whose metrics outgrew the server's limit reached it as the agent's size note,
  with no synthetic section, so its error is all that says a script ran.

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

# How the agent starts a synthetic run's failure, fixed here as of this revision.
_SYNTHETIC_ERROR_STARTS = (
    "Script execution failed: ",
    "Script compilation failed: ",
    "Playwright initialization failed: ",
    "Script execution timed out after ",
    "Script must define an async function called 'run_check(page)'",
    "run_check() must return a dictionary",
    "Failed to capture screenshot: ",
    "Failed to capture trace: ",
)

# Reads text as jsonb, or NULL where PostgreSQL refuses it, so no row can fail the
# upgrade. A plpgsql handler rather than pg_input_is_valid, which needs PostgreSQL
# 16: the installation guide supports PostgreSQL 14 and later.
_JSONB_OR_NULL = """
CREATE FUNCTION pg_temp.luxswirl_jsonb_or_null(value text) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    RETURN value::jsonb;
EXCEPTION WHEN others THEN
    RETURN NULL;
END
$$
"""

# A CASE fixes the order: the text match first (cheap, and most rows fail it),
# then the cast only of text that is valid JSON. Text that is no valid JSON (Python
# writes NaN, which PostgreSQL refuses) counts when it opens with the key, as the
# agent's `{"synthetic": ...}` does stored.
_BACKFILL = """
UPDATE checks SET ran_synthetic = true
WHERE check_type = 'synthetic'
OR script_code IS NOT NULL
OR EXISTS (SELECT 1 FROM check_artifacts a WHERE a.check_id = checks.id)
OR EXISTS (
    SELECT 1 FROM check_results r
    WHERE r.check_id = checks.id
    AND (
        EXISTS (
            SELECT 1 FROM unnest(CAST(:starts AS text[])) AS s(start)
            WHERE starts_with(r.error, s.start)
        )
        OR CASE
            WHEN r.metrics IS NULL OR r.metrics NOT LIKE '%"synthetic"%' THEN false
            WHEN pg_temp.luxswirl_jsonb_or_null(r.metrics) IS NULL
                THEN r.metrics LIKE '{"synthetic":%'
            WHEN jsonb_typeof(pg_temp.luxswirl_jsonb_or_null(r.metrics)) <> 'object'
                THEN false
            ELSE (pg_temp.luxswirl_jsonb_or_null(r.metrics) -> 'synthetic') IS NOT NULL
        END
    )
)
"""


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
    op.execute(_JSONB_OR_NULL)
    op.execute(sa.text(_BACKFILL).bindparams(starts=list(_SYNTHETIC_ERROR_STARTS)))
    op.execute("DROP FUNCTION pg_temp.luxswirl_jsonb_or_null(text)")


def downgrade() -> None:
    op.drop_column("checks", "ran_synthetic")
