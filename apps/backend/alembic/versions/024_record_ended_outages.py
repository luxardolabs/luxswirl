"""record_ended_outages — record the end of each outage nobody was told had ended.

An alert de-duplicates its notifications against its last row for a check, so
the next outage is announced only when that row says the check came back up.
Every alert that does not notify on recovery now records the end of an outage
it announced as a `suppressed` row with the clear state's key. Rows stored
before have none: an alert that does not notify on recovery, on a check one
agent runs, whose last row (a parent's outage aside) is an outage or a latency
breach while the check's newest stored result has ended it, gets one now, so
the check's next outage is announced.

The check's newest result is judged as the alert judges a check one agent runs:
a success ends a status outage, and the newest result with a latency on the
safe side ends a latency breach. A state key stored as its hash (a threshold
too long for the column) is left as it is.

Revision ID: 8d2f6a4c1b37
Revises: 3e8b5c2a9d14
Create Date: 2026-11-15

"""

from alembic import op

revision = "8d2f6a4c1b37"
down_revision = "3e8b5c2a9d14"
branch_labels = None
depends_on = None

NOTE = "Recovered; recovery not announced"

# The alert's last row for each check, a parent's outage over it aside: for an
# alert that does not notify on recovery (one that does announces the end, from
# the results it decides), a check one agent runs (a replicated or distributed
# check is judged across its agents, which its first all-clear result does), and
# a check with no result still queued for its alerts (the decision of those is
# the alert's).
_LAST = """
WITH last AS (
    SELECT DISTINCT ON (n.alert_id, n.check_id)
        n.alert_id, n.check_id, n.notification_provider_id, n.notification_hash
    FROM notification_logs n
    JOIN alerts a ON a.id = n.alert_id AND a.notify_on_recovery IS FALSE
    JOIN checks c ON c.id = n.check_id AND c.assignment_mode = 'manual'
    WHERE n.notification_hash IS NOT NULL
      AND n.notification_hash <> 'parent_down'
      AND NOT EXISTS (
          SELECT 1 FROM alert_decision_queue q WHERE q.check_id = n.check_id
      )
    ORDER BY n.alert_id, n.check_id, n.sent_at DESC, n.decision_seq DESC
)
"""

_STATUS = (
    _LAST
    + f"""
INSERT INTO notification_logs (
    id, alert_id, notification_provider_id, check_result_id,
    check_result_timestamp, check_id, check_success, check_latency_ms,
    status, error_message, notification_hash, sent_at
)
SELECT gen_random_uuid(), last.alert_id, last.notification_provider_id, r.id,
       r.timestamp, last.check_id, r.success, r.latency_ms,
       'suppressed', '{NOTE}',
       CASE last.notification_hash
           WHEN 'status:down' THEN 'status:up'
           ELSE 'repeated_failure:up'
       END,
       now()
FROM last
CROSS JOIN LATERAL (
    SELECT id, timestamp, success, latency_ms FROM check_results
    WHERE check_id = last.check_id
    ORDER BY timestamp DESC LIMIT 1
) r
WHERE last.notification_hash IN ('status:down', 'repeated_failure:down')
  AND r.success
"""
)

# A latency breach key: threshold:latency_ms:<operator>:<number>.
_THRESHOLD = (
    _LAST
    + f"""
, breach AS (
    SELECT last.*,
           split_part(notification_hash, ':', 3) AS operator,
           split_part(notification_hash, ':', 4)::double precision AS value
    FROM last
    WHERE notification_hash ~ '^threshold:latency_ms:(>|>=|<|<=|==):-?[0-9]+(\\.[0-9]+)?$'
)
INSERT INTO notification_logs (
    id, alert_id, notification_provider_id, check_result_id,
    check_result_timestamp, check_id, check_success, check_latency_ms,
    status, error_message, notification_hash, sent_at
)
SELECT gen_random_uuid(), breach.alert_id, breach.notification_provider_id, r.id,
       r.timestamp, breach.check_id, r.success, r.latency_ms,
       'suppressed', '{NOTE}', breach.notification_hash || ':' || 'ok', now()
FROM breach
CROSS JOIN LATERAL (
    SELECT id, timestamp, success, latency_ms FROM check_results
    WHERE check_id = breach.check_id AND latency_ms IS NOT NULL
    ORDER BY timestamp DESC LIMIT 1
) r
WHERE NOT CASE breach.operator
    WHEN '>' THEN r.latency_ms > breach.value
    WHEN '>=' THEN r.latency_ms >= breach.value
    WHEN '<' THEN r.latency_ms < breach.value
    WHEN '<=' THEN r.latency_ms <= breach.value
    ELSE r.latency_ms = breach.value
END
"""
)


def upgrade() -> None:
    op.execute(_STATUS)
    op.execute(_THRESHOLD)


def downgrade() -> None:
    # The rows recorded here are the same as the ones the alerts now write;
    # they say what happened, so the downgrade leaves them.
    pass
