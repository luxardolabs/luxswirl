"""Rate limiting configuration for LuxSwirl Server.

Provides a centralized Limiter instance keyed on the trusted-proxy-aware
client IP — so per-client buckets work correctly behind nginx / traefik / k8s
ingress without exposing rate-limit bypass via attacker-supplied X-Forwarded-For.

See core.request_helpers.client_ip_from_request for the trust model.
"""

import hashlib
import itertools
import time
from dataclasses import dataclass, field, replace
from uuid import UUID

from limits import RateLimitItem, parse_many
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
    """The limit on registration-key checks: agent registrations, and the
    credentials refused on key recoveries and pending heartbeats."""
    return _current.registration


# The buckets counting refused credentials, where the right credential is
# presented over and over and must never run out. Only a refusal is counted in
# the limit's window. A check still in flight is held apart, in this process,
# until it is settled: a burst of concurrent guesses cannot all pass a count
# none has made yet, and right credentials in flight together never spend the
# window.
#
# A pending agent's heartbeat: its recovery secret, or the shared registration
# key from an agent build that sends no secret. A pending agent heartbeats with
# the right credential every interval. Each (client address, agent) pair has
# its own count, so an agent whose credential keeps being refused locks out
# only itself; an unknown agent id is refused before any credential is checked.
_HEARTBEAT_PENDING_REFUSALS = "heartbeat-pending-credential-refusals"
# A key recovery: the registration key, then the agent's recovery secret. Every
# agent behind one address recovers after an approval, a regenerate or a
# revoke. One count per client address, whatever agent id the path names, so a
# guess cannot open a fresh budget by naming another agent.
_KEY_RECOVERY_REFUSALS = "key-recovery-credential-refusals"

# Checks in flight, per bucket: the monotonic time each began. Every path that
# reserves settles in a `finally`; a check that still is not settled after
# IN_FLIGHT_SECONDS stops holding room. That is far past any credential check:
# each of its statements is cut at the database's statement timeout.
IN_FLIGHT_SECONDS = 300.0
_in_flight: dict[tuple[str, ...], dict[int, float]] = {}
_next_attempt = itertools.count()

# The wait answered when the window still has room but checks in flight fill
# it: they settle in about a request's time.
BUSY_RETRY_AFTER_SECONDS = 2


def _held(keys: tuple[str, ...], now: float) -> dict[int, float]:
    """The bucket's checks in flight, those past IN_FLIGHT_SECONDS dropped."""
    held = {
        n: began
        for n, began in _in_flight.get(keys, {}).items()
        if now - began < IN_FLIGHT_SECONDS
    }
    if held:
        _in_flight[keys] = held
    else:
        _in_flight.pop(keys, None)
    return held


@dataclass
class CredentialAttempt:
    """One credential check in flight, holding room in its bucket until it is
    settled. Settled once; a second settle does nothing."""

    limits: tuple[RateLimitItem, ...]
    keys: tuple[str, ...]
    number: int
    settled: bool = field(default=False, init=False)

    def settle(self, *, refused: bool) -> None:
        """End the check: a refusal is counted in every part of the limit's
        window, whether or not its room was still held; a credential not
        refused leaves no trace."""
        if self.settled:
            return
        self.settled = True
        held = _in_flight.get(self.keys, {})
        held.pop(self.number, None)
        if not held:
            _in_flight.pop(self.keys, None)
        if refused:
            for limit in self.limits:
                limiter.limiter.hit(limit, *self.keys)


def _reserve_refusal_count(keys: tuple[str, ...]) -> CredentialAttempt | int:
    """Hold room for one credential check under `keys`, in every part of the
    registration limit, before the credential is read.

    Returns the attempt, to `settle()` once the credential is checked, or the
    seconds to wait (holding nothing): until the window ends when its refusals
    alone spend a part of the limit, else BUSY_RETRY_AFTER_SECONDS when checks
    in flight fill the room left.
    """
    limits = tuple(parse_many(registration_rate_limit()))
    now = time.monotonic()
    held = _held(keys, now)
    spent: list[int] = []
    busy = False
    for limit in limits:
        reset_time, remaining = limiter.limiter.get_window_stats(limit, *keys)
        if remaining <= 0:
            # A window that ended since the last refusal has a reset time
            # already past, so the wait is at least a second.
            spent.append(max(1, int(reset_time - time.time())))
        elif remaining <= len(held):
            busy = True
    if spent:
        return max(spent)
    if busy:
        return BUSY_RETRY_AFTER_SECONDS
    number = next(_next_attempt)
    held[number] = now
    _in_flight[keys] = held
    return CredentialAttempt(limits=limits, keys=keys, number=number)


def reserve_pending_heartbeat_attempt(
    client: str, agent_id: UUID
) -> CredentialAttempt | int:
    """Hold room for one credential check on pending `agent_id`'s heartbeat
    from `client` (see `_reserve_refusal_count`)."""
    return _reserve_refusal_count((_HEARTBEAT_PENDING_REFUSALS, client, str(agent_id)))


def reserve_key_recovery_attempt(client: str) -> CredentialAttempt | int:
    """Hold room for one key recovery's credential checks from `client` (see
    `_reserve_refusal_count`)."""
    return _reserve_refusal_count((_KEY_RECOVERY_REFUSALS, client))


def buckets_in_flight() -> int:
    """How many buckets hold a check in flight now."""
    return len(_in_flight)


def reset_in_flight() -> None:
    """Forget every check in flight, as `limiter.reset()` forgets the counts."""
    _in_flight.clear()


class KeyedLimiter:
    """A per-key count for a surface this limiter cannot decorate, such as the /mcp mount.

    Port of LuxWX's `KeyedLimiter`. Every part of a multi-part limit
    (`300/minute;1000/hour`) holds, as slowapi's decorators hold them. Each call
    counts one request in every part and answers in the same step, with no await
    between, so a concurrent burst cannot slip past the limit before it is counted.
    LuxSwirl's limiter keeps its counts in process memory, and so does this.
    """

    def __init__(self, limit: str) -> None:
        self.limits = tuple(parse_many(check_rate_limit(limit)))
        self.storage = MemoryStorage()
        self.strategy = FixedWindowRateLimiter(self.storage)

    def _wait(self, spent: list[RateLimitItem], surface: str, client: str) -> int:
        """Seconds until every spent part has room: the longest of their waits.

        A window that ended since the refused count has a reset time already
        past, so the wait is at least a second.
        """
        return max(
            max(
                1,
                int(
                    self.strategy.get_window_stats(limit, surface, client).reset_time
                    - time.time()
                ),
            )
            for limit in spent
        )

    def retry_after(self, surface: str, client: str) -> int | None:
        """Count one request in every part; seconds to wait when any part is
        over its limit (and then the count is given back), None while all are under.
        """
        spent = [
            limit
            for limit in self.limits
            if not self.strategy.hit(limit, surface, client)
        ]
        if not spent:
            return None
        for limit in self.limits:
            key = limit.key_for(surface, client)
            if self.storage.get(key) > 0:
                self.storage.incr(key, limit.get_expiry(), amount=-1)
        return self._wait(spent, surface, client)

    def over_limit(self, surface: str, client: str) -> int | None:
        """Seconds to wait when `client` is already at the limit of any part;
        None while under every part.

        Counts nothing: for a limit on failures, counted with `count` once one fails.
        """
        spent = [
            limit
            for limit in self.limits
            if not self.strategy.test(limit, surface, client)
        ]
        if not spent:
            return None
        return self._wait(spent, surface, client)

    def count(self, surface: str, client: str) -> None:
        """Count one event (a failure) against `client`, in every part."""
        for limit in self.limits:
            self.strategy.hit(limit, surface, client)

    def reset(self) -> None:
        """Forget every count (the test suite's reset, as `limiter.reset()`)."""
        self.storage.reset()


# Failed credentials one client may present per minute on a credential surface that is
# not a route this limiter can decorate (the /mcp mount authenticates in its own ASGI
# middleware). Only a refused token counts; a client at the limit is refused before
# its token is looked up.
credential_attempts = KeyedLimiter("10/minute")
# Requests one API token may make over /mcp: the admin API's own budget, every part
# of it, read once at import as the admin routers' decorators read it.
mcp_token_calls = KeyedLimiter(settings.security.admin_api_rate_limit)
