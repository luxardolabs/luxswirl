"""
When a check or an agent counts as silent.

One rule for the dashboard and the silence alerts: a check is silent once its
newest result is older than `missed_intervals` of its own interval plus a grace
for the agent's report batching; an agent is offline once its newest heartbeat
is older than `missed_intervals` of its heartbeat interval plus the same grace.
"""

from datetime import timedelta

# The agent runs a check with no interval of its own every 60 s.
DEFAULT_CHECK_INTERVAL_SECONDS = 60
DEFAULT_MISSED_INTERVALS = 3
# Results reach the server in batches (the agent's report_interval, 10 s by
# default) and a check may run up to its timeout before it reports, so a check
# exactly on time can still look a little late.
GRACE_SECONDS = 60


def silent_after(
    interval_seconds: int | None,
    missed_intervals: int = DEFAULT_MISSED_INTERVALS,
) -> timedelta:
    """How long without data before something reporting every
    `interval_seconds` counts as silent."""
    interval = interval_seconds or DEFAULT_CHECK_INTERVAL_SECONDS
    return timedelta(seconds=interval * missed_intervals + GRACE_SECONDS)
