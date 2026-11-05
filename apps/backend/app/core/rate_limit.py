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
# presented over and over and must never run out. Each check is counted before
# the credential is read and given back once it is not refused, so concurrent
# guesses cannot all pass a count none has made; a refusal keeps its count.
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


@dataclass(frozen=True)
class CredentialAttempt:
    """One credential check, already counted in every part of the limit.
    Concurrent checks with the right credential can see each other's counts,
    so the right credential goes free only once checked."""

    limits: tuple[RateLimitItem, ...]
    keys: tuple[str, ...]

    def release(self) -> None:
        """Give the count back: the credential was not refused.

        A window that ended since the count was made holds nothing of it, so
        nothing is given back there (the next window never starts below zero).
        """
        storage = limiter.limiter.storage
        for limit in self.limits:
            key = limit.key_for(*self.keys)
            if storage.get(key) > 0:
                storage.incr(key, limit.get_expiry(), amount=-1)


def _reserve_refusal_count(keys: tuple[str, ...]) -> CredentialAttempt | int:
    """Count one credential check under `keys`, in every part of the
    registration limit, before the credential is read.

    Returns the attempt, to `release()` unless the credential is refused, or,
    when any part of the limit is spent, the seconds to wait (and counts
    nothing).
    """
    limits = tuple(parse_many(registration_rate_limit()))
    spent = [limit for limit in limits if not limiter.limiter.hit(limit, *keys)]
    attempt = CredentialAttempt(limits=limits, keys=keys)
    if not spent:
        return attempt
    attempt.release()
    # A window that ended since the refused count has a reset time already
    # past, so the wait is at least a second.
    return max(
        max(
            1,
            int(
                limiter.limiter.get_window_stats(limit, *keys).reset_time - time.time()
            ),
        )
        for limit in spent
    )


def reserve_pending_heartbeat_attempt(
    client: str, agent_id: UUID
) -> CredentialAttempt | int:
    """Count one credential check on pending `agent_id`'s heartbeat from
    `client` (see `_reserve_refusal_count`)."""
    return _reserve_refusal_count((_HEARTBEAT_PENDING_REFUSALS, client, str(agent_id)))


def reserve_key_recovery_attempt(client: str) -> CredentialAttempt | int:
    """Count one key recovery's credential checks from `client` (see
    `_reserve_refusal_count`)."""
    return _reserve_refusal_count((_KEY_RECOVERY_REFUSALS, client))


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
