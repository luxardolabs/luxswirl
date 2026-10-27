"""Rate limiting configuration for LuxSwirl Server.

Provides a centralized Limiter instance keyed on the trusted-proxy-aware
client IP — so per-client buckets work correctly behind nginx / traefik / k8s
ingress without exposing rate-limit bypass via attacker-supplied X-Forwarded-For.

See core.request_helpers.client_ip_from_request for the trust model.
"""

import hashlib
import time

from limits import RateLimitItem, RateLimitItemPerMinute, parse
from limits.storage import MemoryStorage
from limits.strategies import FixedWindowRateLimiter
from slowapi import Limiter
from starlette.requests import Request

from app.core.config import settings
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
limiter = Limiter(key_func=_rate_limit_key)


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

    def reset(self) -> None:
        """Forget every count (the test suite's reset, as `limiter.reset()`)."""
        self.storage.reset()


# Credential attempts one client may make per minute on a credential surface that is
# not a route this limiter can decorate (the /mcp mount authenticates in its own ASGI
# middleware). Counted before the check; a token proven in the last minute skips it.
credential_attempts = KeyedLimiter(RateLimitItemPerMinute(10))
# Requests one API token may make per minute over /mcp: the admin API's own budget.
mcp_token_calls = KeyedLimiter(parse(settings.security.admin_api_rate_limit))
