"""Request every registered route once.

The point is not the response — it is that the handler RAN, so
`filterwarnings = error` fires there. A dependency that deprecates an API today
and raises on the next pin is otherwise invisible: not a CVE for the auditor,
not a type error for mypy, and nothing in the lock diff says which page it broke.

So the assertion is narrow on purpose: a route may legitimately answer 404, 409
or 422 for the placeholder body this sends. It may not answer 5xx — that is the
handler blowing up, which is exactly the signal being bought.
"""

from __future__ import annotations

import re

import pytest
from _route_table import iter_routes
from app.core.rate_limit import limiter
from app.main import app

pytestmark = [pytest.mark.route_smoke, pytest.mark.integration]

# Routes with no behaviour to assert, or that would end the session under test.
SKIP_PATHS = frozenset(
    {
        "/health",  # liveness probe — exercised by the container healthcheck
        "/metrics",  # prometheus scrape — its own test asserts the exposition
        # Requested by test_logout_ends_the_session below, on its own session:
        # doing it here would revoke the cookie the rest of the sweep rides on.
        "/logout",
    }
)

# The first-admin /setup is 404 once any user exists, signed in or not; its form is rendered by
# test_setup_wizard_renders_on_an_empty_install.
SETUP_PATHS = frozenset({"/setup"})

# Each ends the session the sweep rides on, so each is requested by its own
# test below on a fresh login. Inside the sweep, POST /change-password
# (FORM_BODY carries the real password) ended it mid-run and every web POST
# after it answered 307 -> /login without reaching its handler.
SESSION_ENDING = frozenset(
    {
        ("POST", "/change-password"),
        ("POST", "/profile/change-password"),
    }
)

# A body that satisfies the common required form/JSON fields. Routes needing
# something specific still reach their handler and answer 422, which is a
# request — the point of the sweep.
FORM_BODY = {
    "username": "route-smoke-admin",
    "password": "RouteSmoke123!",
    "confirm_password": "RouteSmoke123!",
    "current_password": "RouteSmoke123!",
    "new_password": "RouteSmokeNew123!",
    "name": "route-smoke",
    "display_name": "route_smoke_probe",
    "check_type": "ping",
    "target": "127.0.0.1",
    "interval": "60",
    "value": "60",
    "trigger_type": "status_change",
    "provider_type": "webhook",
    "friendly_name": "route-smoke",
    "config_json": "{}",
    "slug": "route-smoke-2",
    "timeout": "10",
    "job_type": "network_scan",
    "role": "viewer",
}


def _routes():
    """(method, path) for every registered HTTP route worth requesting."""
    seen = []
    for route, path in iter_routes(app.routes):
        methods = getattr(route, "methods", None)
        if not path or not methods or path in SKIP_PATHS:
            continue
        for method in sorted(methods):
            if method in ("HEAD", "OPTIONS") or (method, path) in SESSION_ENDING:
                continue
            seen.append((method, path))
    return sorted(set(seen))


# Path endings that destroy the seeded world. Deleting an agent cascades away
# the seeded check, job and artifact; run before the reads and writes, it
# turned every one of their routes into a 404.
DESTRUCTIVE_SUFFIXES = ("/delete", "/revoke", "/revoke-key", "/logout-all")


def _phase(method: str, path: str) -> int:
    """Reads, then writes, then whatever destroys the seeded rows."""
    if method == "DELETE" or path.endswith(DESTRUCTIVE_SUFFIXES):
        return 2
    return 0 if method == "GET" else 1


def _ordered_routes() -> list[tuple[str, str]]:
    return sorted(_routes(), key=lambda route: (_phase(*route), route))


def _fill(path: str, ids: dict[str, str]) -> str:
    return re.sub(r"\{([a-z_]+)\}", lambda m: ids.get(m.group(1), "route-smoke"), path)


async def test_every_route_answers_without_blowing_up(smoke_world):
    client = smoke_world["client"]
    ids = smoke_world["ids"]
    headers = smoke_world["web_headers"]

    failures = []
    attempted = []
    for method, path in _ordered_routes():
        url = _fill(path, ids)
        kwargs = {"headers": headers}
        if method in ("POST", "PUT", "PATCH"):
            # The JSON API reads a JSON body; sent as a form, every one of its
            # writes answered 422 before its handler ran.
            body_kind = "json" if path.startswith("/api/") else "data"
            kwargs[body_kind] = FORM_BODY
        attempted.append((method, path))
        try:
            response = await client.request(method, url, **kwargs)
        except Exception as exc:  # a raise here is the deprecation case
            failures.append(f"{method} {path} raised {type(exc).__name__}: {exc}")
            continue
        if response.status_code >= 500:
            failures.append(f"{method} {path} -> {response.status_code}")
        # The sweep rides an admin session: a bounce to the login page means
        # the session is gone and the handler never ran.
        if (
            response.headers.get("location", "").startswith("/login")
            and path not in SETUP_PATHS
        ):
            failures.append(f"{method} {path} -> {response.status_code} to /login")

    # Self-check: the sweep is only worth anything if it actually issued a
    # request for every route it enumerated.
    missed = {f"{m} {p}" for m, p in _routes()} - {f"{m} {p}" for m, p in attempted}
    assert not missed, f"enumerated but never requested: {sorted(missed)}"

    assert not failures, "routes that blew up:\n  " + "\n  ".join(sorted(failures))


async def test_logout_ends_the_session(smoke_world):
    """Logout gets its own session — inside the sweep it would revoke the cookie
    every other route is riding on."""
    client = smoke_world["client"]
    headers = smoke_world["web_headers"]

    assert (await client.get("/logout", headers=headers)).status_code < 500
    assert (await client.post("/logout", headers=headers)).status_code < 500


async def test_login_page_renders_for_an_anonymous_visitor(smoke_world):
    """The sweep rides an admin cookie, so /login only ever answered 302 to /.

    A 3xx means the handler never ran and login.html was never rendered — the
    one page every unauthenticated visitor sees. Requested here with no session
    header, which is the state it is written for.
    """
    response = await smoke_world["client"].get("/login")
    assert response.status_code == 200, response.status_code
    assert b"password" in response.content.lower()


async def test_setup_wizard_renders_on_an_empty_install(empty_install):
    """/setup is 404 the moment any user exists, which is every other test's
    world — so its template had no test that ever rendered it."""
    response = await empty_install.get("/setup")
    assert response.status_code == 200, response.status_code
    assert b"password" in response.content.lower()


NEW_PASSWORD = "RouteSmokeNew123!"


async def _logs_in(client, password: str) -> bool:
    """Whether the real login route issues a session for `password`."""
    limiter.reset()
    response = await client.post(
        "/login", data={"username": "route-smoke-admin", "password": password}
    )
    return "luxswirl_session=" in response.headers.get("set-cookie", "")


async def _signed_out(client, headers: dict[str, str]) -> bool:
    response = await client.get("/dashboard", headers=headers)
    return response.headers.get("location", "").startswith("/login")


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (
            "/change-password",
            {"data": {"new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD}},
        ),
        (
            "/profile/change-password",
            {"data": {"new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD}},
        ),
    ],
)
async def test_a_password_change_takes_and_ends_every_session(smoke_world, path, body):
    """Each password-change route, on its own session: the sweep's admin
    session ends with it, which inside the sweep logged out every route after."""
    client = smoke_world["client"]
    headers = smoke_world["web_headers"]
    old_password = smoke_world["admin_password"]
    for fields in body.values():
        fields["current_password"] = old_password
    limiter.reset()

    response = await client.post(path, headers=headers, **body)

    assert response.status_code < 400, (response.status_code, response.text[:300])
    assert await _signed_out(client, headers)
    assert not await _logs_in(client, old_password)
    assert await _logs_in(client, NEW_PASSWORD)
