"""A reader who does not hold a check's credentials is shown its target's scheme,
host and port, never its userinfo, path or query.

A list of credential forms never ends: three rounds of attacks on `mask_credentials`
each found real forms that were not on it. So a viewer and the unauthenticated
`/metrics` read a target through a parser that keeps only what cannot carry a secret,
and fails closed (`***`) when it cannot tell where the host is.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from urllib.parse import urlparse

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.rate_limit import limiter
from app.core.target_origin import shown_target, target_origin
from app.main import app
from app.models.enum_model import UserRole
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.api_token_core_service import ApiTokenCoreService
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.check_core_service import CheckCoreService
from app.services.core.metrics_collector_core_service import MetricsCollectorCoreService
from fixtures.factories import make_agent, make_check, make_check_result, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

SECRET = "Zq9SECRETx7"

# Every form a credential has been found in by the attacks on the list mask, with
# the secret in each spelled SECRET.
HOSTILE = [
    f"https://login.example.com/oauth/token?client_secret={SECRET}",
    f"https://a.io/x?auth_token={SECRET}",
    f"https://a.io/x?api-key={SECRET}",
    f"https://a.io/x?x-api-key={SECRET}",
    f"https://a.io/x?key={SECRET}",
    f"https://a.io/x?sig={SECRET}",
    f"https://hooks.slack.com/services/T0/B0/{SECRET}",
    f"https://api.telegram.org/bot123:{SECRET}/send",
    f"https://ha.lan:8123/api/webhook/{SECRET}",
    f"https://a.io/{SECRET}/health",
    f"https://a.io/x?token=%2525{SECRET}",
    f"monitor:{SECRET}@db:5432",
    f"postgresql://monitor:{SECRET}@db.internal:5432/app",
    f"postgresql://monitor:ab/{SECRET}@db/app",
    f"postgresql://monitor:12/{SECRET}@db/app",
    f"postgresql://monitor:12?{SECRET}@db",
    f"postgresql://monitor:12#{SECRET}@db",
    f"postgresql://monitor:12 {SECRET}@db/app",
    f"mysql://u:p@{SECRET}@db:3306/x",
    f"jdbc:postgresql://db:5432/app;password=my {SECRET}",
    f"jdbc:sqlserver://db:1433;databaseName=x;password={SECRET}",
    f"https://a.io/?password=ab&{SECRET}",
    f"Driver={{ODBC}};Server=tcp:sql.lan,1433;Uid=u;Pwd={{ab}}}}{SECRET}}};",
    f"host=db password=ab;{SECRET} dbname=app",
    f"host=db password='a host={SECRET}' port=5432",
    f"Data Source=srv;Password={SECRET}",
    f"DefaultEndpointsProtocol=https;AccountName=a;AccountKey={SECRET}",
    f"https://u:p@h.example.com/x?token=ab@{SECRET}",
    f"ping://https://:{SECRET}",
    f"//user:{SECRET}@127.0.0.1:1/",
    f"http:/user:{SECRET}@127.0.0.1:1",
    f"http:///user:{SECRET}@127.0.0.1:1",
    f"http:user:{SECRET}@h",
    f"tcp:u:{SECRET}@host",
    f"https://{SECRET}@github.com/org/repo.git",
    f"https://ghp_{SECRET}:x-oauth-basic@github.com/x",
    f"Authorization: Bearer {SECRET}",
    f"https://a.io/x#{SECRET}",
    f"https://a.io:{SECRET}/x",
    f"[::1]:{SECRET}",
    f"{SECRET}=x",
    # Found by the first attack on this parser: a userinfo split by two of the
    # authority's end characters, a token as the user name, a host key's text in an
    # unquoted ODBC or escaped libpq value, a query read as pairs, an IPv6 zone.
    f"https://{SECRET}:1234//x@db.example.com/",
    f"https://{SECRET}:1234/a/b@db",
    f"https://{SECRET}:1234?a&b@db",
    f"https://{SECRET}:1234#/x@db",
    f"https://{SECRET}:1234;;@db",
    f"https://{SECRET}:1234 &@db",
    f"postgresql://{SECRET}:5432/x/y@db/app",
    f"https://{SECRET}#/@github.com/x",
    f"https://{SECRET}/?x@github.com/x",
    f"Driver={{ODBC}};Server=db;Uid=u;Pwd=hunter2 host={SECRET};",
    f"Server=db;Pwd=a server={SECRET}",
    f"host=db password=abc\\ host={SECRET}",
    f"host=db password=ab;host={SECRET}",
    f"api.example.com/cb?token=abc;server={SECRET}",
    f"api.example.com/cb?a=1 host={SECRET}",
    f"http://[::1%password={SECRET}]:80/",
    f"http://[fe80::1%{SECRET}]/",
]


@pytest.mark.parametrize("target", HOSTILE)
def test_no_form_of_credential_survives(target: str) -> None:
    assert SECRET.casefold() not in (target_origin(target) or "").casefold()


@pytest.mark.parametrize(
    ("target", "shown"),
    [
        (
            "https://user:pw@api.example.com:8443/v1?token=x",
            "https://api.example.com:8443",
        ),
        ("HTTPS://API.Example.com/", "https://api.example.com"),
        # An `@` after the host cannot be told from a userinfo split early, so the
        # host is hidden rather than guessed.
        (
            "https://grafana.internal:3000/login?email=ops@example.com",
            "https://***",
        ),
        ("Server=db;Port=1433;Pwd=x", "host=db port=1433"),
        ("host=db port=5432 password='p w'", "host=db port=5432"),
        ("api.example.com/cb?token=abc", "api.example.com"),
        (
            "postgresql://monitor:pw@db.internal:5432/app",
            "postgresql://db.internal:5432",
        ),
        ("jdbc:postgresql://db:5432/app?password=x", "jdbc:postgresql://db:5432"),
        ("jdbc:sqlserver://db:1433;password=x", "jdbc:sqlserver://db:1433"),
        ("host=db.internal port=5432 password=x", "host=db.internal port=5432"),
        ("Server=tcp:sql.lan,1433;Pwd=x", "host=sql.lan,1433"),
        ("8.8.8.8", "8.8.8.8"),
        ("example.com", "example.com"),
        ("db:5432", "db:5432"),
        ("monitor:pw@db:5432", "db:5432"),
        ("[2001:db8::1]:443", "[2001:db8::1]:443"),
        ("::1", "[::1]"),
        ("https://[::1]:8080/x", "https://[::1]:8080"),
        ("tcp://db.internal:80 ops@example.com", "tcp://***"),
        ("https://medium.com/@user", "https://***"),
        ("https://h:99999/", "https://***"),
        ("", ""),
        (None, None),
    ],
)
def test_what_a_viewer_is_shown(target: str | None, shown: str | None) -> None:
    assert target_origin(target) == shown


def test_an_editor_reads_the_path_masked_and_a_viewer_the_origin() -> None:
    target = "https://svc:pw@api.example.com/v1/health?token=t"
    for role in (UserRole.ADMIN, UserRole.EDITOR):
        shown = shown_target(target, role)
        assert "/v1/health" in shown
        assert "pw" not in shown
    for maybe_role in (UserRole.VIEWER, None):
        assert shown_target(target, maybe_role) == "https://api.example.com"


@pytest.mark.parametrize(
    "url",
    [
        "https://monitor:4321#zz@db.example.com/",
        "https://monitor:4321/zz@db.example.com/",
        "https://monitor:4321?zz@db.example.com/",
        f"http://[::1%{SECRET}]:8080/",
    ],
)
def test_a_bulk_name_takes_nothing_the_origin_hides(url: str) -> None:
    name = CheckCoreService.bulk_display_name(urlparse(url))

    assert "4321" not in name
    assert SECRET.casefold() not in name.casefold()


@pytest.mark.parametrize(
    "hostile",
    [
        "a" * (4 << 20),
        "https://" + ":" * (4 << 20),
        "https://" + "@" * (4 << 20),
        "host=" + "'" * (4 << 20),
        "host={" + "}" * (4 << 20),
        "x=" * (2 << 20),
        "https://h/" + "/@" * (2 << 20),
    ],
)
def test_reading_a_long_value_takes_no_time(hostile: str) -> None:
    started = time.perf_counter()
    target_origin(hostile)
    assert time.perf_counter() - started < 2.0


# --- every surface a viewer, or no one signed in, reads a target on ---------------

# The secret under a query key no list of credential forms names: an editor, who
# holds it, reads it; a viewer reads neither it nor the path.
PATH_SECRET = f"https://svc:pw@api.example.com/v1/health?sesame={SECRET}"


def _client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    )


@pytest_asyncio.fixture
async def stack(db: AsyncSession, monkeypatch) -> AsyncIterator[dict]:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    limiter.reset()
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(
        agent_id=agent.id,
        display_name="the-check",
        check_type="http",
        target=PATH_SECRET,
    )
    db.add(check)
    await db.flush()
    browsers: dict[str, AsyncClient] = {}
    tokens: dict[str, dict[str, str]] = {}
    for role in ("viewer", "editor"):
        user = make_user(role=role)
        db.add(user)
        await db.flush()
        _, session = await AuthCoreService().create_session(db, user.id)
        _, token = await ApiTokenCoreService.create_token(
            db, user.id, ApiTokenCreate(name=role, scopes=["*"])
        )
        browsers[role] = _client()
        browsers[role].cookies.set(settings.security.session_cookie_name, session)
        tokens[role] = {"Authorization": f"Bearer {token}"}
    api = _client()
    yield {"agent": agent, "check": check, "web": browsers, "token": tokens, "api": api}
    for client in (*browsers.values(), api):
        await client.aclose()
    limiter.reset()


async def test_the_admin_api_shows_a_viewer_the_origin_and_an_editor_the_path(
    stack: dict,
) -> None:
    url = f"/api/v1/admin/checks/{stack['check'].id}"
    viewer = await stack["api"].get(url, headers=stack["token"]["viewer"])
    editor = await stack["api"].get(url, headers=stack["token"]["editor"])

    assert viewer.status_code == editor.status_code == 200
    assert viewer.json()["target"] == "https://api.example.com"
    assert SECRET not in viewer.text
    assert "/v1/health" in editor.json()["target"]


@pytest.mark.parametrize("page", ["/dashboard", "/check/{id}"])
async def test_the_web_shows_a_viewer_the_origin(stack: dict, page: str) -> None:
    url = page.format(id=stack["check"].id)
    viewer = await stack["web"]["viewer"].get(url)
    editor = await stack["web"]["editor"].get(url)

    assert viewer.status_code == editor.status_code == 200
    assert "the-check" in viewer.text
    assert SECRET not in viewer.text
    assert "/v1/health" not in viewer.text
    assert "api.example.com/v1/health" in editor.text


async def test_a_viewers_search_finds_nothing_by_what_is_hidden(stack: dict) -> None:
    for part in ("health", SECRET[:5], "sesame"):
        listed = await stack["api"].get(
            "/api/v1/admin/checks",
            params={"search": part},
            headers=stack["token"]["viewer"],
        )
        assert listed.status_code == 200
        assert listed.json()["items"] == [], part
        page = await stack["web"]["viewer"].get("/dashboard", params={"search": part})
        assert "the-check" not in page.text, part

    by_host = await stack["api"].get(
        "/api/v1/admin/checks",
        params={"search": "api.example"},
        headers=stack["token"]["viewer"],
    )
    assert [c["display_name"] for c in by_host.json()["items"]] == ["the-check"]


async def test_an_editors_admin_api_search_still_finds_the_path(stack: dict) -> None:
    listed = await stack["api"].get(
        "/api/v1/admin/checks",
        params={"search": "health"},
        headers=stack["token"]["editor"],
    )
    assert [c["display_name"] for c in listed.json()["items"]] == ["the-check"]


async def test_the_metrics_label_is_the_origin(stack: dict) -> None:
    agent, check = stack["agent"], stack["check"]
    result = make_check_result(check_id=check.id, agent_id=agent.id)
    result.agent = agent
    MetricsCollectorCoreService.update_check_result(result, check, agent)

    scraped = await stack["api"].get("/metrics")

    assert scraped.status_code == 200
    assert SECRET not in scraped.text
    assert 'target="https://api.example.com"' in scraped.text
