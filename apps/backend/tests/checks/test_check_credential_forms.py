"""Every form a credential takes in a check's target reaches no reader unmasked.

`mask_credentials` is what a viewer's API, MCP and web reads, and `/metrics` labels,
all go through, so the forms are held to the function and to one route per surface.
"""

import subprocess
import sys
import time
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.credential_mask import mask_credentials, mask_mapping, mask_url_paths
from app.core.rate_limit import limiter
from app.main import app
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.api_token_core_service import ApiTokenCoreService
from app.services.core.metrics_collector_core_service import (
    MetricsCollectorCoreService,
)
from fixtures.factories import make_agent, make_check, make_check_result, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

SECRET = "S3cretZZ9"

FORMS = {
    "client_secret": f"https://login.example.com/oauth/token?client_secret={SECRET}",
    "auth_token": f"https://api.example.com/x?auth_token={SECRET}",
    "api-key": f"https://api.example.com/x?api-key={SECRET}",
    "x-api-key": f"https://api.example.com/x?x-api-key={SECRET}",
    "key": f"https://maps.example.com/geocode/json?key={SECRET}",
    "encoded-key": f"https://api.example.com/x?api%2Dkey={SECRET}",
    "sig": f"https://a.blob.example.net/c?sv=2022&sig={SECRET}",
    "amz-signature": f"https://s3.example.com/o?X-Amz-Signature={SECRET}",
    "account-key": (
        "DefaultEndpointsProtocol=https;AccountName=a;"
        f"AccountKey={SECRET};EndpointSuffix=core.windows.net"
    ),
    "joined-key": f"https://api.example.com/x?accesskey={SECRET}",
    "slack-path": f"https://hooks.slack.com/services/T000/B000/{SECRET}",
    "webhook-path": f"https://chat.example.com/api/webhooks/1234/{SECRET}",
    "webhook-host": f"https://x.webhook.office.com/webhookb2/{SECRET}",
    "telegram-bot": f"https://api.telegram.org/bot123:{SECRET}/getMe",
    "odbc-escaped-brace": f"Server=db;Uid=sa;Pwd={{ab}}}}{SECRET}}};Database=app",
    "ado-double-quoted": f'Server=db;User Id=sa;Password="ab;{SECRET}";Database=app',
    "ado-single-quoted": f"Server=db;User Id=sa;Password='ab;{SECRET}';Database=app",
    "appid": f"https://api.openweathermap.org/data/2.5/weather?q=L&appid={SECRET}",
    "functions-code": f"https://fn.azurewebsites.net/api/f?code={SECRET}",
    "appkey": f"https://a.example.com/x?appkey={SECRET}",
    "licensekey": f"https://a.example.com/x?licensekey={SECRET}",
    "jsessionid": f"https://a.example.com/x;jsessionid={SECRET}",
    "phpsessid": f"https://a.example.com/x?PHPSESSID={SECRET}",
    "client-assertion": f"https://login.example.com/t?client_assertion={SECRET}",
    "bracketed-key": f"https://a.example.com/x?user[password]={SECRET}",
    "bracketed-token": f"https://a.example.com/x?auth[token]={SECRET}",
    "odbc-connect": (
        "mssql+pyodbc:///?odbc_connect=DRIVER%3D%7BODBC%7D%3BSERVER%3Ddb%3BUID%3Dsa"
        f"%3BPWD%3D{SECRET}"
    ),
    "slack-no-scheme": f"hooks.slack.com/services/T000/B000/{SECRET}",
    "named-webhook-segment": f"https://a.example.com/slack-webhook/{SECRET}",
    "incoming-webhooks": f"https://a.example.com/incoming-webhooks/{SECRET}",
    "webhook-label": f"https://my-webhook.example.com/{SECRET}",
    "http-library-error": (
        "HTTPSConnectionPool(host='hooks.slack.com', port=443): Max retries exceeded "
        f"with url: /services/T000/B000/{SECRET} (Caused by X)"
    ),
    "pw": f"https://printer.lan/status?user=admin&pw={SECRET}",
    "pswd": f"https://printer.lan/status?user=admin&pswd={SECRET}",
    "sid": f"https://a.example.com/x?sid={SECRET}",
    "clientkey": f"https://a.example.com/x?clientkey={SECRET}",
    "npgsql": f"Host=db;Username=m;Password='ab;{SECRET}';Database=app",
    "passhash": f"https://h/x?passhash={SECRET}",
    "dbpass": f"https://h/x?dbpass={SECRET}",
    "pass1": f"https://h/x?pass1={SECRET}",
    "key1": f"https://h/x?key1={SECRET}",
    "api-keys": f"https://h/x?api_keys={SECRET}",
    "bindpw": f"https://h/x?bindpw={SECRET}",
}


@pytest.mark.parametrize("target", FORMS.values(), ids=FORMS.keys())
def test_the_mask_hides_the_form(target: str) -> None:
    masked = mask_credentials(target)

    assert SECRET not in masked, masked
    assert "987654321" not in masked, masked
    assert "***" in masked
    assert mask_credentials(masked) == masked, "masking twice changes nothing"


@pytest.mark.parametrize(
    "value",
    [
        "https://api.example.com/x?monkey=1&keyword=2&region=eu",
        "https://api.example.com/x?bypass=1&design=2&author=3",
        "https://api.example.com/ping?compass=north",
        "https://example.com/health",
        "git@github.com:org/repo.git",
        "connect to db.internal:5432 refused",
        "10.0.0.5:8080",
        "[::1]:5432",
        "Server=db;Initial Catalog=app;Uid=sa",
        "mongodb://db/app?authSource=admin&replicaSet=rs0",
        "mongodb://db/app?authMechanism=SCRAM-SHA-256",
        "https://a.example.com/x?session_timeout=30",
        "https://a.example.com/x?token_type=bearer",
        "mysql://db/app?auth_plugin=mysql_native_password",
        "registry.example.com:5000/app@sha256:abcdef",
        "mailto:ops@example.com",
        "auth-service:9000",
        "token-service:8080/health",
        "http://a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6-1234567890.us-east-1.elb.example.com/h",
        "tcp://internal-payments-api-prod-1234567890.us-east-1.elb.example.com:5432",
        "https://my-function-app-prod-2024-westeurope-01.azurewebsites.net/api/health",
        "token-service:8080?x=1",
        "session-db:5432?sslmode=require",
        "secret-store:8200#x",
        "auth-service:8080)",
        "registry.npmjs.org/@scope/pkg",
        "unpkg.com/react@18/umd/x.js",
        "example.com/@user",
        "10.0.0.5/foo@bar",
        "https://grafana.internal:3000/login?email=ops@example.com",
        "https://intranet.example.org:8443/redir/a@elsewhere.example.net/x",
        "http://localhost:3000/login?email=ops@example.com",
        "https://a.example.com/x?code=200",
        "https://a.example.com/x?p=2",
        "auth-gateway: Connection refused",
        "session-cache: Timeout waiting",
    ],
)
def test_what_is_no_credential_is_shown_whole(value: str) -> None:
    assert mask_credentials(value) == value


@pytest.mark.parametrize(
    ("value", "kept"),
    [
        ("https://api.example.com/v2?token=abc&region=eu-west", "region=eu-west"),
        ("https://hooks.slack.com/services/T/B/x", "hooks.slack.com"),
        ("https://chat.example.com/api/webhooks/1/x", "chat.example.com/api/webhooks"),
        ("https://h.example.com/api/v1/items", "/api/v1/items"),
        (f"Server=db;Pwd={{{SECRET}}};Initial Catalog=app", "Initial Catalog=app"),
        (f"Server=db;Pwd={SECRET};Initial Catalog=app", "Initial Catalog=app"),
        (f"Pwd={SECRET};Data Source=db", "Data Source=db"),
    ],
)
def test_the_text_around_a_credential_stays(value: str, kept: str) -> None:
    assert kept in mask_credentials(value)


def test_a_url_is_cut_with_its_path_secret_gone() -> None:
    cut = mask_url_paths(f"sent to https://hooks.slack.com/services/T/B/{SECRET} ok")

    assert cut == "sent to https://hooks.slack.com/... ok"


def test_a_string_under_a_secret_key_is_hidden_in_a_mapping() -> None:
    masked = mask_mapping({"password": SECRET, "nested": {"api_key": SECRET}, "n": 1})

    assert masked == {"password": "***", "nested": {"api_key": "***"}, "n": 1}


@pytest.mark.parametrize(
    "mapping",
    [
        {"password": [SECRET]},
        {"api_key": [{"v": SECRET}]},
        {"auth": {"value": SECRET}},
        {"API Key": SECRET},
        {"tokens": [SECRET]},
    ],
    ids=str,
)
def test_what_sits_under_a_secret_key_is_hidden_whatever_its_shape(
    mapping: dict[str, object],
) -> None:
    masked = str(mask_mapping(mapping))

    assert SECRET not in masked
    assert "987654321" not in masked


def test_hiding_keeps_the_shape_a_page_reads() -> None:
    masked = mask_mapping(
        {"track_timings": {"auth": {"start": 1.5, "name": "x"}}, "get_token": 1.5}
    )

    assert masked == {
        "track_timings": {"auth": {"start": 1.5, "name": "***"}},
        "get_token": 1.5,
    }


@pytest.mark.parametrize(
    "mapping",
    [
        {"token_count": 5},
        {"key_size": 2048},
        {"session_count": 3},
        {"token": 5},
        {"auth": True},
        {"token": None},
    ],
    ids=str,
)
def test_a_figure_under_a_secret_word_stays(mapping: dict[str, object]) -> None:
    assert mask_mapping(mapping) == mapping


@pytest.mark.parametrize(
    "value",
    [
        f"Authorization: Bearer {SECRET} refused",
        f"PW='{SECRET}';u",
        f'token="{SECRET}"&flag',
        f"https://u:p@h/x?token={SECRET}",
        f"Server=db;Pwd={SECRET};Database=app",
    ],
)
def test_masking_twice_changes_nothing(value: str) -> None:
    once = mask_credentials(value)

    assert mask_credentials(once) == once


def test_keys_that_mask_alike_are_numbered_in_linear_time() -> None:
    keys = {f"password={number}": 1 for number in range(20_000)}

    started = time.perf_counter()
    masked = mask_mapping(dict(keys))
    elapsed = time.perf_counter() - started

    assert elapsed < 2.0, elapsed
    assert len(masked) == 20_000


def test_a_document_of_short_strings_costs_little_to_mask() -> None:
    document = {"items": ["a"] * (2 * MIB)}

    started = time.perf_counter()
    mask_mapping(document)
    elapsed = time.perf_counter() - started

    assert elapsed < 2.0, elapsed


MIB = 1024 * 1024
QUARTER = MIB // 4


@pytest.mark.parametrize(
    "hostile",
    [
        "a" * (4 * QUARTER) + "=",
        "token" * QUARTER + "=",
        "key" * QUARTER,
        "xkey" * QUARTER + "=",
        "a=" * (2 * QUARTER),
        "a " * (2 * QUARTER),
        " " * (4 * QUARTER),
        "a" + " " * (4 * QUARTER) + "=",
        "://a:" * QUARTER,
        "x://a:" * (QUARTER // 2),
        "(a:" * QUARTER,
        "a:b " * QUARTER,
        ":@ " * QUARTER,
        "/" + "a" * (4 * QUARTER),
        "/1" * (2 * QUARTER),
        "/webhook" * (QUARTER // 2),
        "://hooks." * (QUARTER // 2),
        "x://" + "hooks." * (QUARTER // 2),
        "x://" + "a.webhook." * (QUARTER // 3),
        "/" + "webhook" * (QUARTER // 2),
        "token: " * (QUARTER // 2),
        '"token"' * (QUARTER // 2),
        "%2D" * QUARTER + "=",
        "authorization: " * (QUARTER // 4),
        "bearer " * (QUARTER // 2),
        "a;b" * QUARTER,
        "pwd=a" + ";b" * (2 * QUARTER),
        "%3A%2F%2F" * (QUARTER // 3),
        "%3A%2F%2Fa" * (QUARTER // 3),
        "?u=&p=" * (QUARTER // 2),
        '"' * (4 * QUARTER),
        '"a":' * QUARTER,
        '"token' * (QUARTER // 2),
        '\\"password\\":' * (QUARTER // 3),
        "cookie:" * (QUARTER // 2),
        "https://a:1/" * (QUARTER // 3),
        "https://a:1/@" * (QUARTER // 3),
        "x-password" * (QUARTER // 3) + ":",
        "passw " * (QUARTER // 2),
        "pwd={}}" * (QUARTER // 2),
        "password=a&" * (QUARTER // 3),
        "password= " * (QUARTER // 3),
        "?password=" * (QUARTER // 3),
        "host=a b=" + "c=" * QUARTER,
    ],
    ids=lambda text: repr(text[:12]),
)
def test_masking_a_mib_is_linear_in_time(hostile: str) -> None:
    """An MCP argument can reach 4 MiB, and this runs on the event loop. A quadratic
    pattern takes minutes at this size; a linear one a fraction of a second, under load."""
    started = time.perf_counter()
    mask_url_paths(hostile)
    elapsed = time.perf_counter() - started

    assert elapsed < 3.0, elapsed


@pytest.mark.parametrize(
    "hostile",
    ["'a' * (4 * MIB) + '='", "'token' * MIB + '='", "'(a:' * MIB", "'://a:' * MIB"],
)
def test_masking_4_mib_costs_little_memory(hostile: str) -> None:
    script = (
        "import resource\n"
        "from app.core.credential_mask import mask_url_paths\n"
        f"MIB = {MIB}\n"
        f"text = {hostile}\n"
        "limit = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024"
        " + 200 * 1024 * 1024\n"
        "resource.setrlimit(resource.RLIMIT_DATA, (limit, limit))\n"
        "mask_url_paths(text)\n"
    )

    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
    )

    assert done.returncode == 0, done.stderr[-300:]


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    limiter.reset()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    ) as http:
        yield http


@pytest.mark.integration
@pytest.mark.parametrize("target", FORMS.values(), ids=FORMS.keys())
async def test_a_viewer_neither_reads_nor_finds_it(
    client: AsyncClient, db: AsyncSession, target: str
) -> None:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    db.add(make_check(agent_id=agent.id, display_name="the-check", target=target))
    viewer = make_user(role="viewer")
    db.add(viewer)
    await db.flush()
    _, token = await ApiTokenCoreService.create_token(
        db, viewer.id, ApiTokenCreate(name="viewer", scopes=["*"])
    )
    headers = {"Authorization": f"Bearer {token}"}

    listed = await client.get("/api/v1/admin/checks", headers=headers)
    found = await client.get(
        "/api/v1/admin/checks", params={"search": SECRET}, headers=headers
    )

    assert listed.status_code == 200, listed.text
    assert "the-check" in listed.text
    assert SECRET not in listed.text
    assert found.json()["items"] == []


@pytest.mark.integration
@pytest.mark.parametrize("target", FORMS.values(), ids=FORMS.keys())
async def test_metrics_never_label_it(
    client: AsyncClient, db: AsyncSession, target: str
) -> None:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(agent_id=agent.id, display_name="the-check", target=target)
    db.add(check)
    await db.flush()
    result = make_check_result(check_id=check.id, agent_id=agent.id)
    MetricsCollectorCoreService.update_check_result(result, check, agent)

    response = await client.get("/metrics")

    assert "the-check" in response.text
    assert SECRET not in response.text
