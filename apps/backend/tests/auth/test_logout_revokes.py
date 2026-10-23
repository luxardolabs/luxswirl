# luxarch:logout-revokes asserter v1 — DO NOT edit the marker line above.
#
# WHY THIS FILE EXISTS
# A logout that only deletes the cookie leaves the token valid wherever it was copied (a shared
# machine, a proxy log, a stolen backup) until it expires. FLEET-AUTH-SESSION-STANDARD §2.1: logout
# changes state the SERVER refuses a request on. Whether it does is a fact about the running app, so
# this asks the app: sign in, copy the cookie, log out, send the copied cookie again, and require
# the app to refuse it. `fw.logout_revokes_server_side` credits every logout path listed in
# LOGOUT_PATHS here, whatever the revocation's shape (a `sessions_valid_after` column, a session row,
# a denylist, a cache key; a refusal raised from a service; a write in a model method).
#
# WHAT IT CHECKS, per logout path
#   1. The signed-in client holds a cookie named in SESSION_COOKIES, and PROTECTED_PATH answers 2xx
#      with it (so the replay below can fail for the right reason, not because it never worked).
#   2. The logout answers below 500, sent as the browser would (same-origin headers).
#   3. PROTECTED_PATH, with ONLY the cookies copied before the logout, answers 401, 403 or a redirect.
#
# HOW TO WIRE IT
#   1) Save as tests/test_logout_revokes.py (anywhere under the suite's test root).
#   2) Set the EDIT THIS block: a fixture that yields a client already signed in by cookie (an
#      `httpx.AsyncClient` or `httpx.Client` / Starlette `TestClient`), each logout path, and a GET
#      path that needs the session. Nothing else changes: `fw.asserter_current` compares the rest
#      with the released text.
#   3) A logout reached by another method (`DELETE /session`) goes in LOGOUT_METHODS.
#   4) Keep it in a file pytest collects (`test_*.py`), not a conftest, and add nothing to the EDIT
#      THIS block but its five settings: `fw.logout_revokes_server_side` credits it only then.
from __future__ import annotations

import inspect
from typing import Any
from urllib.parse import urlsplit

import pytest

# --- EDIT THIS ---------------------------------------------------------------------------------------
# A fixture yielding a client signed in by cookie, freshly for each test.
SIGNED_IN_FIXTURE = "web_signed_in_client"
# The cookie(s) the app authenticates by (the repo's [csrf].session_cookies).
SESSION_COOKIES: tuple[str, ...] = ("luxswirl_session",)
# Every logout route's path, as the client requests it.
LOGOUT_PATHS: tuple[str, ...] = ("/logout",)
# A logout path answered by another method than POST: {"/session": "DELETE"}.
LOGOUT_METHODS: dict[str, str] = {}
# A GET path that answers 2xx signed in, and refuses (401, 403 or a redirect) signed out.
PROTECTED_PATH = "/dashboard"


# -----------------------------------------------------------------------------------------------------


async def _send(client: Any, method: str, path: str, **kw: Any) -> Any:
    response = client.request(method, path, follow_redirects=False, **kw)
    return await response if inspect.isawaitable(response) else response


def _same_origin(client: Any) -> dict[str, str]:
    base = urlsplit(str(client.base_url))
    return {"origin": f"{base.scheme}://{base.netloc}", "sec-fetch-site": "same-origin"}


@pytest.fixture
def signed_in_client(request: pytest.FixtureRequest) -> Any:
    """SIGNED_IN_FIXTURE, resolved at setup (an async fixture cannot be fetched inside the test's
    running event loop)."""
    return request.getfixturevalue(SIGNED_IN_FIXTURE)


@pytest.mark.parametrize("logout_path", LOGOUT_PATHS)
async def test_a_cookie_copied_before_logout_is_refused_after_it(
    signed_in_client: Any, logout_path: str
) -> None:
    client = signed_in_client
    before = await _send(client, "GET", PROTECTED_PATH)
    assert 200 <= before.status_code < 300, (
        f"GET {PROTECTED_PATH} answers {before.status_code} signed in: pick a path the session opens"
    )
    # copied after the request above, just before the logout: an app that re-issues its cookie on
    # use has retired the earlier one, and replaying that would pass a logout that revokes nothing
    copied = dict(client.cookies.items())
    assert any(name in copied for name in SESSION_COOKIES), (
        f"{SIGNED_IN_FIXTURE} holds none of {SESSION_COOKIES}: it is not signed in by cookie"
    )
    method = LOGOUT_METHODS.get(logout_path, "POST")
    out = await _send(client, method, logout_path, headers=_same_origin(client))
    assert out.status_code < 500, f"{method} {logout_path} answered {out.status_code}"
    client.cookies.clear()
    for name, value in copied.items():
        client.cookies.set(name, value)
    replay = await _send(client, "GET", PROTECTED_PATH)
    refused = replay.status_code in (401, 403) or 300 <= replay.status_code < 400
    assert refused, (
        f"after {method} {logout_path}, the cookie copied before it still opens GET {PROTECTED_PATH} "
        f"({replay.status_code}): the logout signs out in the browser only"
    )
