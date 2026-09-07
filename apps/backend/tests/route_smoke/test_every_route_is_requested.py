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
    from app.main import app

    seen = []
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods or path in SKIP_PATHS:
            continue
        for method in sorted(methods):
            if method in ("HEAD", "OPTIONS"):
                continue
            seen.append((method, path))
    return sorted(set(seen))


def _fill(path: str, ids: dict[str, str]) -> str:
    return re.sub(r"\{([a-z_]+)\}", lambda m: ids.get(m.group(1), "route-smoke"), path)


async def test_every_route_answers_without_blowing_up(smoke_world):
    client = smoke_world["client"]
    ids = smoke_world["ids"]
    headers = {**smoke_world["web_headers"], **smoke_world["api_headers"]}

    failures = []
    attempted = []
    for method, path in _routes():
        url = _fill(path, ids)
        kwargs = {"headers": headers}
        if method in ("POST", "PUT", "PATCH"):
            # Send both shapes: form routes read the body, JSON routes ignore it
            # and answer 422, which still reached the handler's dependencies.
            kwargs["data"] = FORM_BODY
        attempted.append((method, path))
        try:
            response = await client.request(method, url, **kwargs)
        except Exception as exc:  # a raise here is the deprecation case
            failures.append(f"{method} {path} raised {type(exc).__name__}: {exc}")
            continue
        if response.status_code >= 500:
            failures.append(f"{method} {path} -> {response.status_code}")

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
