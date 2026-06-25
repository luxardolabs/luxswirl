"""
Security Headers Middleware - Adds security headers to all HTTP responses.

This module implements defense-in-depth security measures through HTTP headers.
"""

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import settings


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Add security headers to all responses.

    Headers added:
    - X-Frame-Options: Prevents clickjacking
    - X-Content-Type-Options: Prevents MIME sniffing
    - X-XSS-Protection: Enables browser XSS filter (legacy)
    - Strict-Transport-Security: Forces HTTPS (production only)
    - Content-Security-Policy: Restricts resource loading
    - Referrer-Policy: Controls referer information leakage
    - Permissions-Policy: Disables unnecessary browser features
    """

    async def dispatch(self, request: Request, call_next):
        """Add security headers to response."""
        response: Response = await call_next(request)

        # Prevent clickjacking attacks
        response.headers["X-Frame-Options"] = "DENY"

        # Prevent MIME type sniffing
        response.headers["X-Content-Type-Options"] = "nosniff"

        # Enable XSS filter in older browsers (mostly legacy)
        response.headers["X-XSS-Protection"] = "1; mode=block"

        # Strict HTTPS enforcement (only in production with HTTPS)
        # WARNING: This can break local dev if enabled incorrectly
        if settings.server.environment == "production":
            # max-age=31536000 = 1 year
            # includeSubDomains = apply to all subdomains
            # preload = allow browser preload list inclusion (optional)
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

        # Content Security Policy - Primary defense against XSS
        # All resources loaded from same origin only (self-hosted)
        csp_directives = [
            "default-src 'self'",  # Default: only from our domain
            "script-src 'self' 'unsafe-inline' 'unsafe-eval'",  # Scripts: our domain + inline (HTMX) + eval (Alpine.js)
            "style-src 'self' 'unsafe-inline'",  # Styles: our domain + inline
            "img-src 'self' data: https:",  # Images: our domain + data URIs + any HTTPS
            "font-src 'self' data:",  # Fonts: our domain + data URIs
            "connect-src 'self'",  # AJAX/fetch/WebSocket: only our domain
            "frame-ancestors 'none'",  # Cannot be iframed (redundant with X-Frame-Options)
            "base-uri 'self'",  # Restrict <base> tag to prevent base tag hijacking
            "form-action 'self'",  # Forms can only submit to our domain
            "object-src 'none'",  # No Flash/Java/plugins
            "upgrade-insecure-requests",  # Auto-upgrade HTTP to HTTPS in production
        ]
        response.headers["Content-Security-Policy"] = "; ".join(csp_directives)

        # Referrer policy - balance between privacy and functionality
        # strict-origin-when-cross-origin:
        #   - Same origin: send full URL
        #   - HTTPS → HTTPS: send origin only
        #   - HTTPS → HTTP: send nothing
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        # Permissions Policy - disable unused browser features
        # Reduces attack surface by disabling APIs we don't use
        permissions = [
            "geolocation=()",  # No location access
            "microphone=()",  # No microphone access
            "camera=()",  # No camera access
            "payment=()",  # No payment API
            "usb=()",  # No USB access
            "magnetometer=()",  # No magnetometer
            "accelerometer=()",  # No accelerometer
            "gyroscope=()",  # No gyroscope
        ]
        response.headers["Permissions-Policy"] = ", ".join(permissions)

        return response
