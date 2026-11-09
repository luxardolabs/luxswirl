"""Webhook and Home Assistant notifications are held to the SSRF settings on save and
on every connection, and the connection opens on the address that was vetted.

A name that answers a safe address when the provider is saved and the metadata
address when it sends gets no connection. Private networks follow
`security.block_private_networks`; the cloud-metadata range is always refused.
Every connection here goes to a local server or is refused before it opens.
"""

from __future__ import annotations

import asyncio
import socket
import time
from typing import Any

import httpcore
import pytest
from app.core.exceptions import ValidationException
from app.schemas.notification_provider_schema import (
    NotificationProviderCreate,
    NotificationProviderUpdate,
)
from app.services.core.notification_core_service import NotificationCoreService
from app.services.core.settings_core_service import SettingsCoreService
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

METADATA = "169.254.169.254"
PATHS = {"webhook": "/hook", "homeassistant": "/api/webhook/ha-id"}


@pytest.fixture
def answers(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[list[str]]]:
    """Each name's resolver answers, in turn; the last repeats."""
    table: dict[str, list[list[str]]] = {}
    real = socket.getaddrinfo

    def fake(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host not in table:
            return real(host, *args, **kwargs)
        turns = table[host]
        addresses = turns.pop(0) if len(turns) > 1 else turns[0]
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))
            for address in addresses
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return table


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every address a socket was opened on."""
    seen: list[str] = []
    real = httpcore.AnyIOBackend.connect_tcp

    async def spy(self: Any, host: str, *args: Any, **kwargs: Any) -> Any:
        seen.append(host)
        return await real(self, host, *args, **kwargs)

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", spy)
    return seen


async def _listener() -> tuple[asyncio.Server, int, list[bytes]]:
    requests: list[bytes] = []

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        requests.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1], requests


async def _provider(db: AsyncSession, provider_type: str, url: str) -> Any:
    return await NotificationCoreService.create_provider(
        db,
        NotificationProviderCreate(
            provider_type=provider_type,
            friendly_name=f"ssrf-{provider_type}",
            config={"post_url": url},
            is_enabled=True,
            is_default_enabled=False,
        ),
    )


async def _block_private_networks(db: AsyncSession) -> None:
    await SettingsCoreService.ensure_default_settings(db)
    await SettingsCoreService.update_setting(
        db, "security.block_private_networks", True
    )


@pytest.mark.parametrize("provider_type", ["webhook", "homeassistant"])
async def test_a_name_that_rebinds_after_the_save_gets_no_connection(
    db: AsyncSession,
    answers: dict[str, list[list[str]]],
    opened: list[str],
    provider_type: str,
) -> None:
    answers["rebind.test"] = [["192.0.2.10"], [METADATA]]
    provider = await _provider(
        db, provider_type, f"http://rebind.test{PATHS[provider_type]}"
    )

    sent, error = await NotificationCoreService.test_provider(db, provider.id)

    assert sent is False
    assert opened == []
    assert error is not None
    assert error.startswith(
        "CheckTargetBlockedError: 169.254.169.254 is in the blocked"
    )
    assert "Settings > Security" in error


@pytest.mark.parametrize("provider_type", ["webhook", "homeassistant"])
async def test_a_name_that_rebinds_after_its_check_is_never_dialed_there(
    db: AsyncSession,
    answers: dict[str, list[list[str]]],
    opened: list[str],
    provider_type: str,
) -> None:
    """Safe for the save and for any check before the send, the metadata address
    after: the send connects where its own lookup vetted, never a lookup after."""
    server, port, requests = await _listener()
    answers["late.test"] = [["127.0.0.1"], ["127.0.0.1"], [METADATA]]
    async with server:
        provider = await _provider(
            db, provider_type, f"http://late.test:{port}{PATHS[provider_type]}"
        )
        sent, _ = await NotificationCoreService.test_provider(db, provider.id)

    assert METADATA not in opened
    assert opened == ["127.0.0.1"]
    assert sent is True
    assert len(requests) == 1


@pytest.mark.parametrize("provider_type", ["webhook", "homeassistant"])
async def test_a_name_is_dialed_on_its_vetted_address(
    db: AsyncSession,
    answers: dict[str, list[list[str]]],
    opened: list[str],
    provider_type: str,
) -> None:
    server, port, requests = await _listener()
    answers["hooks.lan"] = [["127.0.0.1"]]
    async with server:
        provider = await _provider(
            db, provider_type, f"http://hooks.lan:{port}{PATHS[provider_type]}"
        )
        sent, error = await NotificationCoreService.test_provider(db, provider.id)

    assert (sent, error) == (True, None)
    assert opened == ["127.0.0.1"]
    assert f"host: hooks.lan:{port}".encode() in requests[0].lower()


@pytest.mark.parametrize("provider_type", ["webhook", "homeassistant"])
async def test_a_private_address_follows_the_setting_on_send(
    db: AsyncSession, opened: list[str], provider_type: str
) -> None:
    server, port, requests = await _listener()
    async with server:
        provider = await _provider(
            db, provider_type, f"http://127.0.0.1:{port}{PATHS[provider_type]}"
        )
        allowed = await NotificationCoreService.test_provider(db, provider.id)
        await _block_private_networks(db)
        refused = await NotificationCoreService.test_provider(db, provider.id)

    assert allowed == (True, None)
    assert refused[0] is False
    assert refused[1] is not None and "private network" in refused[1]
    assert len(requests) == 1
    assert opened == ["127.0.0.1"]


@pytest.mark.parametrize("provider_type", ["webhook", "homeassistant"])
async def test_a_private_address_is_refused_on_save_when_blocked(
    db: AsyncSession, opened: list[str], provider_type: str
) -> None:
    await _block_private_networks(db)

    with pytest.raises(ValidationException, match="private network"):
        await _provider(db, provider_type, f"http://10.0.0.5{PATHS[provider_type]}")

    assert opened == []


async def test_an_update_to_a_private_address_is_refused_when_blocked(
    db: AsyncSession,
) -> None:
    provider = await _provider(db, "webhook", "http://192.0.2.10/hook")
    await _block_private_networks(db)

    with pytest.raises(ValidationException, match="private network"):
        await NotificationCoreService.update_provider(
            db,
            provider.id,
            NotificationProviderUpdate(config={"post_url": "http://127.0.0.1/hook"}),
        )


async def test_the_metadata_range_is_refused_even_with_its_setting_off(
    db: AsyncSession, opened: list[str]
) -> None:
    await SettingsCoreService.ensure_default_settings(db)
    await SettingsCoreService.update_setting(db, "security.block_cloud_metadata", False)

    with pytest.raises(ValidationException, match="cloud metadata"):
        await _provider(db, "webhook", f"http://{METADATA}/latest/meta-data/")

    assert opened == []


async def test_saving_a_provider_leaves_the_event_loop_free(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    def slow_lookup(*_args: Any, **_kwargs: Any) -> list[Any]:
        time.sleep(0.4)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", slow_lookup)
    loop = asyncio.get_running_loop()
    gaps: list[float] = []
    saving = True

    async def ticker() -> None:
        last = loop.time()
        while saving:
            await asyncio.sleep(0.01)
            gaps.append(loop.time() - last)
            last = loop.time()

    ticks = asyncio.create_task(ticker())
    await _provider(db, "webhook", "http://slow.example.com/hook")
    saving = False
    await ticks

    assert sum(gaps) >= 0.4
    assert max(gaps) < 0.2
