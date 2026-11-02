"""Rate limiting configuration for LuxSwirl Server.

Provides a centralized Limiter instance keyed on the trusted-proxy-aware
client IP — so per-client buckets work correctly behind nginx / traefik / k8s
ingress without exposing rate-limit bypass via attacker-supplied X-Forwarded-For.

See core.request_helpers.client_ip_from_request for the trust model.
"""

import hashlib
import time
from dataclasses import dataclass, replace
from uuid import UUID

from limits import RateLimitItem, RateLimitItemPerMinute, parse, parse_many
from limits.storage import MemoryStorage
from limits.strategies import FixedWindowRateLimiter
from slowapi import Limiter
from starlette.requests import Request

from app.core.config import settings
from app.core.rate_limit_format import check_rate_limit
from app.core.request_helpers import client_ip_from_request


def _rate_limit_key(request: Request) -> str:
    """slowapi key function: real client IP, with trusted-proxy XFF handling."""
    return client_ip_from_request(request)


def api_token_rate_limit_key(request: Request) -> str:
    """slowapi key function for the admin API: the calling token, by its hash.

    The route's dependency has already authenticated the token when the limit
    is checked, so every bucket is one real token. The limiter's storage and
    its log lines keep a digest of the token under this key's own label: never
    the token, nor the hash the database stores.
    """
    _, _, token = request.headers.get("authorization", "").partition(" ")
    digest = hashlib.sha256(b"rate-limit:" + token.strip().encode("utf-8"))
    return "api-token:" + digest.hexdigest()


# Global limiter — imported by routers that need @limiter.limit(...) decorators.
# One count per limited function, not per concrete path: a path parameter the
# caller picks (`/users/{user_id}/reset-password`) would otherwise open a fresh
# budget for every value (FLEET-RATE-LIMIT-STANDARD §3).
limiter = Limiter(key_func=_rate_limit_key, key_style="endpoint")


@dataclass(frozen=True)
class RateLimits:
    """The per-route limits the Settings page governs, as `count/period` strings.

    Each starts at its environment setting. `SettingsCoreService` loads the
    stored settings at startup and replaces a limit whenever its setting is
    set or reset, by any path.
    """

    login: str = settings.security.login_rate_limit
    api: str = settings.security.api_rate_limit
    registration: str = settings.security.registration_rate_limit


_current = RateLimits()


def current_rate_limits() -> RateLimits:
    """The limits in effect now."""
    return _current


def update_rate_limits(
    login: str | None = None,
    api: str | None = None,
    registration: str | None = None,
) -> None:
    """Replace the limits given; the others are kept.

    Raises:
        ValueError: A limit given is not a rate limit (see `check_rate_limit`).
    """
    global _current
    changes = {
        name: check_rate_limit(value)
        for name, value in (
            ("login", login),
            ("api", api),
            ("registration", registration),
        )
        if value is not None
    }
    _current = replace(_current, **changes)


# The limit each decorated route reads on every request it counts.
def login_rate_limit() -> str:
    """The limit on password checks: /login and the change-password form."""
    return _current.login


def api_rate_limit() -> str:
    """The limit on the Prometheus /metrics endpoint."""
    return _current.api


def registration_rate_limit() -> str:
    """The limit on registration-key checks: agent registration and key recovery."""
    return _current.registration


# The buckets counting registration keys refused on the heartbeat, which falls
# back to that key for an agent without its own. Only refusals count: a pending
# agent heartbeats with the right key every interval and must not run out.
#
# Each (client address, agent) pair holds the registration budget, so an agent
# whose key keeps being refused locks out only itself. The address as a whole
# holds twice that budget across all its agents: one stuck agent spends at most
# half of it, and agent ids an address has learned (an unnamed agent's id is a
# /metrics label) never multiply what it may guess. An unknown agent id is
# refused before any key is checked. These budgets are the heartbeat's own:
# /register and /recover-key keep theirs.
_HEARTBEAT_REGISTRATION_REFUSALS = "heartbeat-registration-key-refusals"
_ADDRESS_SHARE = 2


@dataclass(frozen=True)
class _Counted:
    """One count made in one part of a limit, under its identifiers."""

    limit: RateLimitItem
    keys: tuple[str, ...]


@dataclass(frozen=True)
class RegistrationAttempt:
    """One heartbeat registration-key check, already counted in every part of
    both budgets, so concurrent checks cannot all pass a count none has made.
    Concurrent checks with the right key can see each other's counts, so the
    right key goes free only once checked."""

    counted: tuple[_Counted, ...]

    def release(self) -> None:
        """Give every count back: the key was not refused.

        A window that ended since the count was made holds nothing of it, so
        nothing is given back there (the next window never starts below zero).
        """
        storage = limiter.limiter.storage
        for each in self.counted:
            key = each.limit.key_for(*each.keys)
            if storage.get(key) > 0:
                storage.incr(key, each.limit.get_expiry(), amount=-1)


def _address_limit(limit: RateLimitItem) -> RateLimitItem:
    """`limit` with `_ADDRESS_SHARE` times its amount, over the same period."""
    return type(limit)(limit.amount * _ADDRESS_SHARE, limit.multiples)


def reserve_heartbeat_registration_attempt(
    client: str, agent_id: UUID
) -> RegistrationAttempt | int:
    """Count one registration-key check on `agent_id`'s heartbeat from
    `client`, in every part of the agent's and the address's budgets, before
    the key is read.

    Returns the attempt, to `release()` unless the key is refused, or, when
    any part of either budget is spent, the seconds to wait (and counts
    nothing).
    """
    agent_keys = (_HEARTBEAT_REGISTRATION_REFUSALS, client, str(agent_id))
    address_keys = (_HEARTBEAT_REGISTRATION_REFUSALS, client)
    limits = parse_many(registration_rate_limit())
    wanted = [_Counted(limit, agent_keys) for limit in limits] + [
        _Counted(_address_limit(limit), address_keys) for limit in limits
    ]
    counted: list[_Counted] = []
    for each in wanted:
        counted.append(each)
        if not limiter.limiter.hit(each.limit, *each.keys):
            RegistrationAttempt(tuple(counted)).release()
            return max(
                max(1, int(stats.reset_time - time.time()))
                for spent in wanted
                if not limiter.limiter.test(spent.limit, *spent.keys)
                for stats in [
                    limiter.limiter.get_window_stats(spent.limit, *spent.keys)
                ]
            )
    return RegistrationAttempt(tuple(counted))


class KeyedLimiter:
    """A per-key count for a surface this limiter cannot decorate, such as the /mcp mount.

    Port of LuxWX's `KeyedLimiter`. Each call counts one request and answers in the
    same step, so a concurrent burst cannot slip past the limit before it is counted.
    LuxSwirl's limiter keeps its counts in process memory, and so does this.
    """

    def __init__(self, limit: RateLimitItem) -> None:
        self.limit = limit
        self.storage = MemoryStorage()
        self.strategy = FixedWindowRateLimiter(self.storage)

    def retry_after(self, surface: str, client: str) -> int | None:
        """Count one request; seconds to wait when over the limit, None while under it."""
        if self.strategy.hit(self.limit, surface, client):
            return None
        stats = self.strategy.get_window_stats(self.limit, surface, client)
        return max(1, int(stats.reset_time - time.time()))

    def over_limit(self, surface: str, client: str) -> int | None:
        """Seconds to wait when `client` is already at the limit; None while under it.

        Counts nothing: for a limit on failures, counted with `count` once one fails.
        """
        if self.strategy.test(self.limit, surface, client):
            return None
        stats = self.strategy.get_window_stats(self.limit, surface, client)
        return max(1, int(stats.reset_time - time.time()))

    def count(self, surface: str, client: str) -> None:
        """Count one event (a failure) against `client`."""
        self.strategy.hit(self.limit, surface, client)

    def reset(self) -> None:
        """Forget every count (the test suite's reset, as `limiter.reset()`)."""
        self.storage.reset()


# Failed credentials one client may present per minute on a credential surface that is
# not a route this limiter can decorate (the /mcp mount authenticates in its own ASGI
# middleware). Only a refused token counts; a client at the limit is refused before
# its token is looked up.
credential_attempts = KeyedLimiter(RateLimitItemPerMinute(10))
# Requests one API token may make per minute over /mcp: the admin API's own budget.
mcp_token_calls = KeyedLimiter(parse(settings.security.admin_api_rate_limit))
