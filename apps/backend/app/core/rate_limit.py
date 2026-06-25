"""Rate limiting configuration for LuxSwirl Server.

Provides a centralized Limiter instance keyed on the trusted-proxy-aware
client IP — so per-client buckets work correctly behind nginx / traefik / k8s
ingress without exposing rate-limit bypass via attacker-supplied X-Forwarded-For.

See core.request_helpers.client_ip_from_request for the trust model.
"""

from slowapi import Limiter

from app.core.request_helpers import client_ip_from_request


def _rate_limit_key(request) -> str:
    """slowapi key function: real client IP, with trusted-proxy XFF handling."""
    return client_ip_from_request(request)


# Global limiter — imported by routers that need @limiter.limit(...) decorators.
limiter = Limiter(key_func=_rate_limit_key)
