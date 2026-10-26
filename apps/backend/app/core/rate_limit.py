"""Rate limiting configuration for LuxSwirl Server.

Provides a centralized Limiter instance keyed on the trusted-proxy-aware
client IP — so per-client buckets work correctly behind nginx / traefik / k8s
ingress without exposing rate-limit bypass via attacker-supplied X-Forwarded-For.

See core.request_helpers.client_ip_from_request for the trust model.
"""

import hashlib

from slowapi import Limiter
from starlette.requests import Request

from app.core.request_helpers import client_ip_from_request


def _rate_limit_key(request: Request) -> str:
    """slowapi key function: real client IP, with trusted-proxy XFF handling."""
    return client_ip_from_request(request)


def api_token_rate_limit_key(request: Request) -> str:
    """slowapi key function for the admin API: the calling token, by its hash.

    The route's dependency has already authenticated the token when the limit
    is checked, so every bucket is one real token. The hash, not the token, is
    what the limiter's storage keeps.
    """
    _, _, token = request.headers.get("authorization", "").partition(" ")
    return "api-token:" + hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


# Global limiter — imported by routers that need @limiter.limit(...) decorators.
limiter = Limiter(key_func=_rate_limit_key)
