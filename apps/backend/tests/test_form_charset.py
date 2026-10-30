"""A multipart form body is UTF-8; one whose Content-Type names another charset is refused.

Starlette decodes a multipart field with the charset its client names. A codec
such as unicode_escape turns the bytes `\\ud800` into a lone surrogate, text
UTF-8 cannot encode, which no page can echo back and no log line can write. A
browser names no charset there, so a body that names one other than UTF-8 is
refused with 415 before any route runs.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

OWN = settings.allowed_hosts[0]
BOUNDARY = "charsetboundary"


@pytest_asyncio.fixture
async def client(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    limiter.reset()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url=f"https://{OWN}"
    ) as c:
        yield c
    limiter.reset()


async def _login(client: AsyncClient, content_type: str, username: bytes) -> int:
    body = (
        (
            f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="username"\r\n\r\n'
        ).encode()
        + username
        + (
            f"\r\n--{BOUNDARY}\r\n"
            'Content-Disposition: form-data; name="password"\r\n\r\n'
            f"nope\r\n--{BOUNDARY}--\r\n"
        ).encode()
    )
    resp = await client.post(
        "/login",
        content=body,
        headers={"Content-Type": content_type, "Origin": f"https://{OWN}"},
    )
    return resp.status_code


@pytest.mark.parametrize(
    "charset", ["unicode_escape", "raw_unicode_escape", "utf-7", "latin-1", "bogus"]
)
async def test_a_multipart_body_in_another_charset_is_refused(
    client: AsyncClient, caplog: pytest.LogCaptureFixture, charset: str
) -> None:
    with caplog.at_level(logging.DEBUG):
        status = await _login(
            client,
            f"multipart/form-data; boundary={BOUNDARY}; charset={charset}",
            b"a\\ud800",
        )

    assert status == 415
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


@pytest.mark.parametrize(
    "content_type",
    [
        f"multipart/form-data; boundary={BOUNDARY}",
        f"multipart/form-data; boundary={BOUNDARY}; charset=utf-8",
        f'multipart/form-data; boundary={BOUNDARY}; charset="UTF8"',
    ],
)
async def test_a_utf8_multipart_body_reaches_the_route(
    client: AsyncClient, content_type: str
) -> None:
    assert await _login(client, content_type, "nobody-é".encode()) == 401


async def test_a_urlencoded_body_naming_a_charset_reaches_the_route(
    client: AsyncClient,
) -> None:
    resp = await client.post(
        "/login",
        content=b"username=nobody&password=nope",
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=unicode_escape",
            "Origin": f"https://{OWN}",
        },
    )

    assert resp.status_code == 401


async def test_the_refusal_is_the_apps_error_response(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/agent/register",
        content=f"--{BOUNDARY}--\r\n".encode(),
        headers={
            "Content-Type": f"multipart/form-data; boundary={BOUNDARY}; charset=utf-7",
            "Accept": "application/json",
        },
    )

    assert resp.status_code == 415
    assert resp.json()["error"] == "UNSUPPORTED_MEDIA_TYPE"
    assert "Content-Security-Policy" in resp.headers
