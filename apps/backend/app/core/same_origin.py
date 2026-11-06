# luxarch:same-origin asset v7 - DO NOT edit this marker line; it is how repo.emitted_assets_current knows your copy is current. Re-emit with `luxarch --emit same-origin`.
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

A websocket handshake is judged by the same test and closed with 1008 before the app accepts it: a
browser sends the visitor's cookies with a handshake from any page and always says where it came from
in ``Origin``, so another site's page could otherwise open an authenticated socket (cross-site
websocket hijacking). For a handshake only, ``ws`` is ``http`` and ``wss`` is ``https``: in the
scope, in ``X-Forwarded-Proto`` (a proxy may forward the handshake's own scheme), in ``Origin`` and
in ``Referer``. A plain request names its site with ``http`` or ``https`` alone, so a ``ws``/``wss``
Origin or Referer on one is another site, and ``X-Forwarded-Proto: ws`` on one is no scheme (the
scope's is used). An unsafe-method request (POST, PUT, PATCH, DELETE) is refused with 403 when:

* ``Sec-Fetch-Site`` is ``cross-site``; or
* ``Sec-Fetch-Site`` is ``same-site`` and the page's host is the request's own; or
* ``Origin`` is ``null``, is not a serialized origin, names a host the app does not serve
  (``*.example.com`` admits that domain's subdomains), or is ``http://`` while the request itself
  arrived over https; every value of a repeated header is judged; or
* there is no ``Origin`` and ``Sec-Fetch-Site`` does not say ``same-origin`` (or ``none``, the user
  typed it), and ``Referer`` is not this site by the same test.

v2: a site is a scheme and a host. v1 compared the host only, so ``Origin: http://<this host>`` passed
on an https app: a network attacker serving plain http to a browser that sends no ``Sec-Fetch-Site``
(Safari before 16.4) could log the visitor in to the attacker's account. The request's own scheme is
``X-Forwarded-Proto`` when the proxy that ends TLS sets it (the fleet's nginx does:
``proxy_set_header X-Forwarded-Proto $scheme``), else the ASGI scope's. A browser cannot set that
header on a cross-site form, and a client that can carries no victim's cookie, so it is trusted for
this question. An ``https`` Origin is always this site's scheme (a network attacker cannot serve it);
an ``http`` one only when the request itself is http (a dev server). Behind a proxy that ends TLS
and sets no ``X-Forwarded-Proto``, the app sees http and an ``http`` Origin passes, as in v1: set it.
An ``Origin`` must be exactly ``scheme://host[:port]``, as a browser serializes it (v1 read
``https://evil.example\\@<host>`` as ``<host>``). ``Referer`` is judged whenever ``Origin`` is absent
and ``Sec-Fetch-Site`` is not ``same-origin`` or ``none`` (v1 skipped it under ``same-site``, a
sibling subdomain the app does not serve).

A port can be another site, and browsers send a cookie to every port of a host, so a page from another
app on ``localhost`` could drive this one. ``Sec-Fetch-Site`` is the witness: it says ``same-origin`` for
this site's own form whatever a proxy did to ``Host``, and ``same-site`` from the request's own host name
is another port. The request's host is ``Host`` or the first hop of ``X-Forwarded-Host``. An app whose own
front end posts from another port of its host lists those paths in ``cross_site_paths``.

The proxy has to name the host the browser asked for: ``proxy_set_header Host $host`` (or ``$http_host``)
and ``X-Forwarded-Host $host``. A proxy that writes a fixed name there (``$server_name``, or nginx's default
``Host``, which is ``proxy_pass``'s) makes a real POST from the sibling so named read as this rule's case.

``Host``'s port is not compared: a proxy may drop or rewrite it (``proxy_set_header Host $host``,
``$host:$server_port``, a published port), and a real login would be refused. So a page on another port
passes where the browser sends no ``Sec-Fetch-Site``: a websocket handshake in Chromium, and plain http to
a host other than loopback in every browser. So does a sibling host on another port, and a page on this
host behind a proxy that names neither it nor ``X-Forwarded-Host``.

The 403 carries a fixed set of security headers itself (``Cache-Control: no-store``,
``X-Content-Type-Options: nosniff``, ``X-Frame-Options: DENY``, ``Referrer-Policy: no-referrer`` and
a ``default-src 'none'`` Content-Security-Policy): it is added after the app's own headers
middleware, so its refusal never passes through that one (v5). A refused websocket handshake gets
the same 403 where the server supports the ASGI denial-response extension, and is otherwise closed
with 1008 before it is accepted (the server then answers a bare 403).

A request with neither header (a webhook, a server-to-server call, a CLI) is not a browser carrying a
victim's cookies and passes. An endpoint that a third party must POST to from its own page (an OAuth
provider's ``response_mode=form_post`` callback) is listed in ``cross_site_paths``; such a route has
to prove the browser started the flow itself, by checking the OAuth ``state`` against a cookie. An
entry ending in ``/`` exempts its subtree (``"/api/v1/events/"`` exempts ``/api/v1/events/{id}``; it
does not exempt ``/api/v1/events`` itself): for a surface no cookie authenticates (an HMAC- or
key-authenticated API a WebView SDK posts to with its own ``Origin``). v3 added it; v1 and v2 matched
exact paths only.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from urllib.parse import urlsplit

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_SCHEMES = frozenset({"http", "https"})
_WS_SCHEMES = {
    "ws": "http",
    "wss": "https",
}  # a handshake's scheme asks the same site question; a plain request's never says ws/wss
# v5: set on every refusal, which runs outside the app's own headers middleware.
REFUSAL_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}
# A serialized origin, as a browser sends it: scheme, host, optional port, nothing else
_ORIGIN = re.compile(
    r"^(https?)://([A-Za-z0-9._-]+|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?$", re.IGNORECASE
)
# A handshake's Origin may also carry the socket's own scheme (a native client)
_WS_ORIGIN = re.compile(
    r"^(https?|wss?)://([A-Za-z0-9._-]+|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?$",
    re.IGNORECASE,
)
# A request's own Host or X-Forwarded-Host: a name or a bracketed address, and any port
_HOST = re.compile(r"^([A-Za-z0-9._-]+|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?$")


def _host_allowed(host: str, allowed_hosts: Sequence[str]) -> bool:
    host = host.lower().strip("[]").rstrip(".")
    for pattern in allowed_hosts:
        p = pattern.lower().strip().strip("[]").rstrip(".")
        if p.startswith("*.") and (host.endswith(p[1:]) and host != p[2:]):
            return True
        if host == p:
            return True
    return False


def _this_site(
    scheme: str,
    host: str,
    allowed_hosts: Sequence[str],
    own_scheme: str,
    websocket: bool = False,
) -> bool:
    """https is this site's scheme always; http only when the request itself is http. For a
    websocket handshake, wss is https and ws is http."""
    scheme = scheme.lower()
    if websocket:
        scheme = _WS_SCHEMES.get(scheme, scheme)
    if scheme not in _SCHEMES or (scheme == "http" and own_scheme != "http"):
        return False
    return _host_allowed(host, allowed_hosts)


def _is_own_host(host: str, own_hosts: Sequence[str]) -> bool:
    """Whether a page's host name is the one the request came in on."""
    host = host.lower().strip("[]").rstrip(".")
    for own in own_hosts:
        m = _HOST.match(own.strip())
        if m and m.group(1).lower().strip("[]").rstrip(".") == host:
            return True
    return False


def _foreign_origin(
    origin: str,
    allowed_hosts: Sequence[str],
    own_scheme: str,
    websocket: bool = False,
    own_hosts: Sequence[str] = (),
) -> bool:
    """`own_hosts` is given only for a `same-site` request: on one of them, it is another port."""
    m = (_WS_ORIGIN if websocket else _ORIGIN).match(origin.strip())
    if m is None:  # `null`, or not a serialized origin (a path, userinfo, a space)
        return True
    if _is_own_host(m.group(2), own_hosts):
        return True
    return not _this_site(m.group(1), m.group(2), allowed_hosts, own_scheme, websocket)


def _foreign_referer(
    referer: str,
    allowed_hosts: Sequence[str],
    own_scheme: str,
    websocket: bool = False,
    own_hosts: Sequence[str] = (),
) -> bool:
    try:
        parts = urlsplit(referer.strip())
        # `.port` raises on a port that does not parse; port 0 is no server's
        if "@" in parts.netloc or not parts.hostname or parts.port == 0:
            return True
    except ValueError:
        return True
    if _is_own_host(parts.hostname, own_hosts):
        return True
    return not _this_site(
        parts.scheme, parts.hostname, allowed_hosts, own_scheme, websocket
    )


def is_cross_site(
    headers: Mapping[str, str] | Mapping[str, list[str]],
    allowed_hosts: Sequence[str],
    *,
    scheme: str | None = None,
    websocket: bool = False,
) -> bool:
    """Whether a request's own headers say it came from another site. Every value of a repeated
    header is judged, whatever the case of its name (a Starlette `Headers` is read with getlist).
    The request's own scheme is `X-Forwarded-Proto`'s when set, else `scheme` (the ASGI scope's;
    https when not given). With no `Origin`, `Referer` is judged unless `Sec-Fetch-Site` says
    same-origin. `websocket=True` judges a handshake: ws and wss count as http and https in the
    scheme, X-Forwarded-Proto, Origin and Referer; a plain request accepts http and https only."""
    getlist = getattr(headers, "getlist", None)

    def values(name: str) -> list[str]:
        if callable(getlist):
            return [str(v) for v in getlist(name)]
        out: list[str] = []
        for k, v in headers.items():
            if k.lower() == name:
                out += [v] if isinstance(v, str) else list(v)
        return out

    forwarded = [v.strip().lower() for v in values("x-forwarded-proto")]
    # one value, or the first hop's (a proxy chain appends: `https, http`)
    first = forwarded[0].split(",")[0].strip() if forwarded else ""
    own_scheme = (scheme or "https").lower()
    if websocket:
        first = _WS_SCHEMES.get(first, first)
        own_scheme = _WS_SCHEMES.get(own_scheme, own_scheme)
    own_scheme = first if first in _SCHEMES else own_scheme
    fetch_site = [v.strip().lower() for v in values("sec-fetch-site")]
    if "cross-site" in fetch_site:
        return True
    own_hosts = (
        values("host") + [v.split(",")[0] for v in values("x-forwarded-host")]
        if "same-site" in fetch_site
        else []
    )
    origins = values("origin")
    if origins:
        return any(
            _foreign_origin(o, allowed_hosts, own_scheme, websocket, own_hosts)
            for o in origins
        )
    if fetch_site and all(v in ("same-origin", "none") for v in fetch_site):
        return False
    return any(
        _foreign_referer(r, allowed_hosts, own_scheme, websocket, own_hosts)
        for r in values("referer")
    )


class SameOriginMiddleware:
    """Refuse an unsafe-method request, or a websocket handshake, a browser sent from another site."""

    def __init__(
        self,
        app: ASGIApp,
        allowed_hosts: Sequence[str],
        cross_site_paths: Sequence[str] = (),
    ) -> None:
        if (
            isinstance(allowed_hosts, str)
            or not allowed_hosts
            or any(h.strip() == "*" for h in allowed_hosts)
        ):
            # `*` would treat every site as this one; empty would refuse every browser POST; a bare
            # string would be read a character at a time
            raise ValueError(
                "SameOriginMiddleware needs the app's real ALLOWED_HOSTS as a list (not a string, "
                "not empty, not *)"
            )
        if isinstance(cross_site_paths, str) or any(
            not p.startswith("/") or p == "/" for p in cross_site_paths
        ):
            # `/` would exempt every path; a bare string would be read a character at a time
            raise ValueError(
                "cross_site_paths takes paths starting with `/` (a subtree ends in `/`), not `/`"
            )
        self.app = app
        self.allowed_hosts = tuple(allowed_hosts)
        self.cross_site_paths = frozenset(
            p for p in cross_site_paths if not p.endswith("/")
        )
        self.cross_site_prefixes = tuple(p for p in cross_site_paths if p.endswith("/"))

    def _exempt(self, path: str) -> bool:
        return path in self.cross_site_paths or path.startswith(
            self.cross_site_prefixes
        )

    def _cross_site(self, scope: Scope) -> bool:
        headers: dict[str, list[str]] = {}
        for k, v in scope["headers"]:
            headers.setdefault(k.decode("latin-1").lower(), []).append(
                v.decode("latin-1")
            )
        return is_cross_site(
            headers,
            self.allowed_hosts,
            scheme=scope.get("scheme"),
            websocket=scope["type"] == "websocket",
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket" and not self._exempt(scope["path"]):
            if self._cross_site(scope):
                if "websocket.http.response" in (scope.get("extensions") or {}):
                    # v5: where the server can send an HTTP answer, the 403 with the headers
                    await send(
                        {
                            "type": "websocket.http.response.start",
                            "status": 403,
                            "headers": [
                                (k.lower().encode("latin-1"), v.encode("latin-1"))
                                for k, v in REFUSAL_HEADERS.items()
                            ]
                            + [(b"content-type", b"text/plain; charset=utf-8")],
                        }
                    )
                    await send(
                        {
                            "type": "websocket.http.response.body",
                            "body": b"Cross-site request refused",
                        }
                    )
                    return
                # closing before the app accepts refuses the handshake (the server answers 403)
                await receive()  # websocket.connect
                await send({"type": "websocket.close", "code": 1008})
                return
        elif (
            scope["type"] == "http"
            and scope["method"] in _UNSAFE
            and not self._exempt(scope["path"])
            and self._cross_site(scope)
        ):
            response = PlainTextResponse(
                "Cross-site request refused", status_code=403, headers=REFUSAL_HEADERS
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
