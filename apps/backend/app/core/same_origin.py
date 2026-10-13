# luxarch:same-origin asset v1 - DO NOT edit this marker line; it is how repo.emitted_assets_current knows your copy is current. Re-emit with `luxarch --emit same-origin`.
"""The app refuses a cross-site POST: the one cross-site guard (FLEET-AUTH-SESSION-STANDARD §5).

Emitted by ``luxarch --emit same-origin``. Drop it in at ``app/core/same_origin.py``; it is enforced
by ``fw.auth_posts_refuse_cross_site``.

Why: ``SameSite=Lax`` stops a cross-site page from sending the victim's cookie with a POST, but it
does not stop the response's ``Set-Cookie``. A hostile page that auto-submits a login form signs the
visitor into the ATTACKER's account (login CSRF), and everything they then type lands there. A
browser says where a request came from: ``Sec-Fetch-Site`` and ``Origin``. A request whose source
is another site is refused before any route runs.

Wire it on the app, with the same ALLOWED_HOSTS the trusted-host middleware uses:

    from app.core.same_origin import SameOriginMiddleware

    app.add_middleware(SameOriginMiddleware, allowed_hosts=settings.ALLOWED_HOSTS)

An unsafe-method request (POST, PUT, PATCH, DELETE) is refused with 403 when:

* ``Sec-Fetch-Site`` is ``cross-site``; or
* ``Origin`` is ``null``, or names a host the app does not serve (``*.example.com`` admits that
  domain's subdomains); every value of a repeated header is judged; or
* neither header is present (an old browser) and ``Referer`` names a host the app does not serve.

A request with neither header (a webhook, a server-to-server call, a CLI) is not a browser carrying a
victim's cookies and passes. An endpoint that a third party must POST to from its own page (an OAuth
provider's ``response_mode=form_post`` callback) is listed in ``cross_site_paths``; such a route has
to prove the browser started the flow itself, by checking the OAuth ``state`` against a cookie.
"""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlsplit

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _host_allowed(host: str, allowed_hosts: Sequence[str]) -> bool:
    host = host.lower().rstrip(".")
    for pattern in allowed_hosts:
        p = pattern.lower().rstrip(".")
        if p.startswith("*.") and (host.endswith(p[1:]) and host != p[2:]):
            return True
        if host == p:
            return True
    return False


def _foreign(url: str, allowed_hosts: Sequence[str]) -> bool:
    if url.strip().lower() == "null":
        return True
    host = urlsplit(url.strip()).hostname
    return host is None or not _host_allowed(host, allowed_hosts)


def is_cross_site(
    headers: dict[str, list[str]] | dict[str, str], allowed_hosts: Sequence[str]
) -> bool:
    """Whether a request's own headers say it came from another site. Every value of a repeated
    header is judged; with neither `Sec-Fetch-Site` nor `Origin` (an old browser), `Referer`."""

    def values(name: str) -> list[str]:
        v = headers.get(name)
        if v is None:
            return []
        return [v] if isinstance(v, str) else list(v)

    fetch_site = values("sec-fetch-site")
    if any(v.strip().lower() == "cross-site" for v in fetch_site):
        return True
    origins = values("origin")
    if origins:
        return any(_foreign(o, allowed_hosts) for o in origins)
    if not fetch_site:
        return any(_foreign(r, allowed_hosts) for r in values("referer"))
    return False


class SameOriginMiddleware:
    """Refuse an unsafe-method request a browser sent from another site."""

    def __init__(
        self,
        app: ASGIApp,
        allowed_hosts: Sequence[str],
        cross_site_paths: Sequence[str] = (),
    ) -> None:
        if not allowed_hosts or any(h.strip() == "*" for h in allowed_hosts):
            # `*` would treat every site as this one; empty would refuse every browser POST
            raise ValueError(
                "SameOriginMiddleware needs the app's real ALLOWED_HOSTS (not empty, not *)"
            )
        self.app = app
        self.allowed_hosts = tuple(allowed_hosts)
        self.cross_site_paths = frozenset(cross_site_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope["method"] in _UNSAFE
            and scope["path"] not in self.cross_site_paths
        ):
            headers: dict[str, list[str]] = {}
            for k, v in scope["headers"]:
                headers.setdefault(k.decode("latin-1").lower(), []).append(
                    v.decode("latin-1")
                )
            if is_cross_site(headers, self.allowed_hosts):
                response = PlainTextResponse(
                    "Cross-site request refused", status_code=403
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
