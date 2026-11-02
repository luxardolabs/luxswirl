"""KeyedLimiter holds every part of a multi-part limit, as slowapi does on the admin API."""

from __future__ import annotations

from app.core.config import settings
from app.core.rate_limit import KeyedLimiter, mcp_token_calls
from limits import parse_many

SURFACE = "test-surface"
CLIENT = "client-a"


def _remaining(limiter: KeyedLimiter, index: int) -> int:
    return limiter.strategy.get_window_stats(
        limiter.limits[index], SURFACE, CLIENT
    ).remaining


def test_every_part_of_a_multi_part_limit_holds() -> None:
    limiter = KeyedLimiter("5/minute;2/hour")
    assert limiter.retry_after(SURFACE, CLIENT) is None
    assert limiter.retry_after(SURFACE, CLIENT) is None
    # The minute part has room; the hour part is spent, so the wait is the hour's.
    wait = limiter.retry_after(SURFACE, CLIENT)
    assert wait is not None
    assert wait > 60


def test_a_refused_request_gives_back_what_it_counted() -> None:
    limiter = KeyedLimiter("2/minute;3/hour")
    assert limiter.retry_after(SURFACE, CLIENT) is None
    assert limiter.retry_after(SURFACE, CLIENT) is None
    assert limiter.retry_after(SURFACE, CLIENT) is not None
    # The refused request left the hour part where the two served ones put it.
    assert _remaining(limiter, 1) == 1


def test_the_longest_wait_is_answered_when_several_parts_are_spent() -> None:
    limiter = KeyedLimiter("1/minute;1/hour")
    assert limiter.retry_after(SURFACE, CLIENT) is None
    wait = limiter.retry_after(SURFACE, CLIENT)
    assert wait is not None
    assert wait > 60


def test_failures_count_in_every_part() -> None:
    limiter = KeyedLimiter("5/minute;2/hour")
    assert limiter.over_limit(SURFACE, CLIENT) is None
    limiter.count(SURFACE, CLIENT)
    assert limiter.over_limit(SURFACE, CLIENT) is None
    limiter.count(SURFACE, CLIENT)
    wait = limiter.over_limit(SURFACE, CLIENT)
    assert wait is not None
    assert wait > 60


def test_counts_are_per_client() -> None:
    limiter = KeyedLimiter("1/minute;5/hour")
    assert limiter.retry_after(SURFACE, CLIENT) is None
    assert limiter.retry_after(SURFACE, "client-b") is None


def test_mcp_token_calls_holds_every_part_of_the_admin_api_limit() -> None:
    assert mcp_token_calls.limits == tuple(
        parse_many(settings.security.admin_api_rate_limit)
    )
