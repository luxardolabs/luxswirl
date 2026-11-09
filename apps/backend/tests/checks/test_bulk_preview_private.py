"""The bulk preview never probes the server's own network, and one editor cannot stack
previews without limit.

The preview runs on the server, which is not where the check runs: a row whose
address is private (RFC 1918, loopback, CGNAT, link-local with metadata blocking
off) opens no connection, whatever the settings allow, and says the agent will test
it. A name with a public address and a private one is probed on the public one
only. The route is rate-limited per signed-in session, and a form over the import's
10,000-line cap is refused before any row is named.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpcore
import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.exceptions import ValidationException
from app.core.rate_limit import BULK_PREVIEW_RATE_LIMIT, limiter
from app.main import app
from app.services.core import check_preview_core_service
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.check_preview_core_service import (
    NOT_PROBED_PRIVATE,
    PREVIEW_MAX_URLS,
    CheckPreviewCoreService,
)
from app.services.core.settings_core_service import SettingsCoreService
from app.web.templates import templates
from fixtures.factories import make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

OK = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


@pytest.fixture
def answers(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    table: dict[str, list[str]] = {}
    real = socket.getaddrinfo

    def fake(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host not in table:
            return real(host, *args, **kwargs)
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))
            for address in table[host]
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return table


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    async def spy(self: Any, host: str, *args: Any, **kwargs: Any) -> Any:
        seen.append(host)
        raise httpcore.ConnectError("no network in this test")

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", spy)
    return seen


async def _row(db: AsyncSession, url: str) -> dict[str, Any]:
    [row] = await CheckPreviewCoreService.bulk_preview_checks(db, [url])
    return row


async def test_a_loopback_listener_gets_no_connection(db: AsyncSession) -> None:
    requests: list[bytes] = []

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        requests.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(OK)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        row = await _row(db, f"http://127.0.0.1:{port}/")

    assert requests == []
    assert row["validation_status"] == "unknown"
    assert row["validation_message"] == NOT_PROBED_PRIVATE
    assert row["status_code"] is None


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.5/",
        "http://172.16.0.1:9000/",
        "http://192.168.1.1/",
        "http://[::1]:9000/",
        "http://[fd00::1]/",
        "http://100.64.0.1/",
        "http://0.0.0.0:9000/",
        "http://[::ffff:10.0.0.5]/",
        "http://localhost:9000/",
        "https://db.internal/",
        # Not global, though in neither setting's list: benchmarking, reserved,
        # IETF protocol assignments, IPv6 discard.
        "http://198.18.0.1/",
        "http://240.0.0.1/",
        "http://192.0.0.8/",
        "http://[100::1]/",
        # An IPv6 form that is global itself but carries a non-global IPv4 address,
        # and IPv6 site-local.
        "http://[64:ff9b::c612:1]/",  # NAT64 of 198.18.0.1
        "http://[64:ff9b::f000:1]/",  # NAT64 of 240.0.0.1
        "http://[::ffff:0:c612:1]/",  # SIIT of 198.18.0.1
        "http://[::c612:1]/",  # IPv4-compatible 198.18.0.1
        "http://[fec0::1]/",
    ],
)
async def test_a_private_address_gets_no_connection(
    db: AsyncSession,
    answers: dict[str, list[str]],
    opened: list[str],
    url: str,
) -> None:
    answers["db.internal"] = ["10.1.2.3"]

    row = await _row(db, url)

    assert opened == []
    assert row["validation_status"] == "unknown"
    assert row["validation_message"] == NOT_PROBED_PRIVATE


async def test_link_local_gets_no_connection_with_metadata_blocking_off(
    db: AsyncSession, opened: list[str]
) -> None:
    await SettingsCoreService.ensure_default_settings(db)
    await SettingsCoreService.update_setting(db, "security.block_cloud_metadata", False)

    row = await _row(db, "http://169.254.169.254/latest/meta-data/")

    assert opened == []
    assert row["validation_message"] == NOT_PROBED_PRIVATE


async def test_a_name_with_a_public_and_a_private_address_is_probed_on_the_public(
    db: AsyncSession, answers: dict[str, list[str]], opened: list[str]
) -> None:
    answers["dual.test"] = ["10.0.0.5", "1.2.3.4"]

    await _row(db, "http://dual.test/")

    assert opened == ["1.2.3.4"]


async def test_private_networks_blocked_still_says_blocked(
    db: AsyncSession, opened: list[str]
) -> None:
    await SettingsCoreService.ensure_default_settings(db)
    await SettingsCoreService.update_setting(
        db, "security.block_private_networks", True
    )

    row = await _row(db, "http://10.0.0.5/")

    assert opened == []
    assert row["validation_status"] == "error"
    assert "blocked private network" in row["validation_message"]


async def test_a_form_over_the_cap_is_refused_before_any_row(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    named: list[str] = []
    real = check_preview_core_service.CheckCoreService.classify_bulk_url

    def spy(url: str) -> Any:
        named.append(url)
        return real(url)

    monkeypatch.setattr(
        check_preview_core_service.CheckCoreService, "classify_bulk_url", spy
    )

    with pytest.raises(ValidationException, match="at most 10,000"):
        await CheckPreviewCoreService.bulk_preview_checks(
            db, ["a"] * (PREVIEW_MAX_URLS + 1)
        )

    assert named == []


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    limiter.reset()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    ) as http:
        yield http
    limiter.reset()


async def _session(db: AsyncSession) -> str:
    user = make_user(role="editor")
    db.add(user)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, user.id)
    return token


async def test_one_session_cannot_stack_previews_without_limit(
    client: AsyncClient, db: AsyncSession, opened: list[str]
) -> None:
    budget = int(BULK_PREVIEW_RATE_LIMIT.split("/")[0])
    mine, theirs = await _session(db), await _session(db)
    cookie = settings.security.session_cookie_name

    async def post(token: str) -> int:
        client.cookies.set(cookie, token)
        resp = await client.post(
            "/checks/bulk-preview", data={"bulk_urls": "tcp://192.0.2.1:80"}
        )
        return resp.status_code

    answered = [await post(mine) for _ in range(budget)]
    over = await post(mine)
    other = await post(theirs)

    assert answered == [200] * budget
    assert over == 429
    assert other == 200


async def test_the_route_refuses_a_form_over_the_cap(
    client: AsyncClient, db: AsyncSession, opened: list[str]
) -> None:
    token = await _session(db)

    client.cookies.set(settings.security.session_cookie_name, token)
    resp = await client.post(
        "/checks/bulk-preview",
        data={"bulk_urls": "\n".join(["a"] * (PREVIEW_MAX_URLS + 1))},
    )

    assert resp.status_code == 422
    assert opened == []


def test_the_form_drops_the_preview_still_in_flight() -> None:
    """Typing sends a preview per pause: each replaces the one still in flight."""
    html = templates.get_template(
        "partials/panels/checks/fields/bulk_fields.html"
    ).render(
        check=None, create_urls=SimpleNamespace(bulk_preview="/checks/bulk-preview")
    )

    textarea = html[html.index("<textarea") : html.index("</textarea>")]
    assert 'hx-sync="this:replace"' in textarea
    assert 'hx-post="/checks/bulk-preview"' in textarea
