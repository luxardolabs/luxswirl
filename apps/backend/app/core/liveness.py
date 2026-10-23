"""
When a check or an agent counts as silent.

One rule for the dashboard, the silence alerts and distribute failover: a check
is silent once its newest result is older than `missed_intervals` of its own
interval plus a grace for the agent's report batching; an agent is offline once
its newest heartbeat is older than `missed_intervals` of its heartbeat interval
plus the same grace.
"""

from datetime import datetime, timedelta

from app.core.config import settings
from app.models.agent_model import Agent
from app.models.enum_model import AgentStatus

# The agent runs a check with no interval of its own every 60 s.
DEFAULT_CHECK_INTERVAL_SECONDS = 60
DEFAULT_MISSED_INTERVALS = 3
# The most an alert may set: a larger value is a typo, and far enough out it
# overflows the datetime range.
MAX_MISSED_INTERVALS = 100
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


def heartbeat_interval(agent: Agent) -> int:
    """The interval `agent` heartbeats at: its own, or the server default."""
    return agent.heartbeat_interval or settings.server.default_heartbeat_interval


def offline_at(
    agent: Agent, missed_intervals: int = DEFAULT_MISSED_INTERVALS
) -> datetime:
    """When `agent` counts as offline unless it heartbeats again first.

    An agent that has never heartbeated is measured from its registration.
    """
    last = agent.last_seen or agent.created_at
    return last + silent_after(heartbeat_interval(agent), missed_intervals)


def agent_offline(
    agent: Agent, now: datetime, missed_intervals: int = DEFAULT_MISSED_INTERVALS
) -> bool:
    """Whether `agent` has missed `missed_intervals` heartbeats by `now`."""
    return now > offline_at(agent, missed_intervals)


def handed_off(agent: Agent, now: datetime) -> bool:
    """Whether `agent`'s distributed checks belong to another agent of their
    pool: it is offline, or its latest heartbeat said it was shutting down.

    A shutdown hands over at once, since nothing will run its checks; the
    silence alerts still wait for `agent_offline`, so a restart does not page.
    """
    return agent.status == AgentStatus.OFFLINE or agent_offline(agent, now)


def handed_off_at(agent: Agent) -> datetime:
    """When `agent`'s distributed checks went to another agent (see
    `handed_off`)."""
    if agent.status == AgentStatus.OFFLINE:
        return agent.last_seen or agent.created_at
    return offline_at(agent)
