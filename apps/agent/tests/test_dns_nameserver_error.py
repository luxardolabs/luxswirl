"""A DNS check's error never quotes its nameserver.

A DNS-over-HTTPS nameserver is a URL whose path can be the provider's access
token, and the server shows a result's error to every signed-in user.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from app.checks import dns as dns_mod
from app.checks.dns import DNSCheck

pytestmark = pytest.mark.unit

TOKEN = "f00dfacecafe1234"


async def _allowed(target: str) -> None:
    return None


@pytest.mark.parametrize(
    "nameserver",
    [
        f"http://doh.example.net/{TOKEN}/dns-query",
        f"ftp://doh.example.net/{TOKEN}",
        f"doh.example.net/{TOKEN}",
    ],
    ids=["http-url", "ftp-url", "name-with-path"],
)
async def test_a_refused_nameserver_is_not_quoted_in_the_error(
    monkeypatch, nameserver
) -> None:
    monkeypatch.setattr(dns_mod, "target_allowed", _allowed)
    check = DNSCheck(
        {
            "check_id": str(uuid4()),
            "name": "doh",
            "check_type": "dns",
            "target": "example.test",
            "nameserver": nameserver,
            "record_type": "A",
            "retries": 1,
        }
    )

    result = await check.run()

    assert result["success"] is False
    assert result["error"].startswith("Invalid nameserver"), result["error"]
    assert TOKEN not in result["error"], result["error"]


async def test_an_error_that_quotes_the_nameserver_leaves_it_out(
    monkeypatch,
) -> None:
    nameserver = f"https://doh.example.net/{TOKEN}/dns-query"

    async def refuse(self, *args, **kwargs):
        raise dns_mod.dns.exception.DNSException(f"{nameserver} answered badly")

    monkeypatch.setattr(dns_mod, "target_allowed", _allowed)
    monkeypatch.setattr(dns_mod.dns.asyncresolver.Resolver, "resolve", refuse)
    check = DNSCheck(
        {
            "check_id": str(uuid4()),
            "name": "doh",
            "check_type": "dns",
            "target": "example.test",
            "nameserver": nameserver,
            "record_type": "A",
            "retries": 1,
        }
    )

    result = await check.run()

    assert result["success"] is False
    assert TOKEN not in result["error"], result["error"]
    assert "answered badly" in result["error"]
