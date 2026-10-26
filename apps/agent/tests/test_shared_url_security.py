"""The agent sends its credentials to the server URL, so plain HTTP is allowed
only to an internal destination: loopback, the compose service names, RFC 1918
addresses, .local and .internal. Anything else needs HTTPS, or the explicit
testing override, which warns.

The boundary is the point: a hostname that merely contains an internal name or
address is external.
"""

from __future__ import annotations

import pytest
from app.shared.url_security import validate_server_url

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "url",
    [
        "https://monitor.example.com:9000",
        "https://8.8.8.8",
        "http://localhost:9000",
        "http://127.0.0.1:9000",
        "http://[::1]:9000",
        "http://server:9000",
        "http://luxswirl_server:9000",
        "http://luxswirl-server:9000",
        "http://10.0.0.5:9000",
        "http://172.16.0.1",
        "http://172.31.255.255",
        "http://192.168.1.20:9000",
        "http://box.local:9000",
        "http://host.docker.internal:9000",
    ],
)
def test_https_or_an_internal_http_destination_is_accepted(url: str) -> None:
    validate_server_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://monitor.example.com:9000",
        "http://8.8.8.8:9000",
        # the edges of the private ranges are public
        "http://172.15.255.255",
        "http://172.32.0.1",
        "http://11.0.0.1",
        "http://192.169.0.1",
        # an internal name or address inside a public one is public
        "http://10.0.0.5.attacker.example",
        "http://localhost.attacker.example",
        "http://server.attacker.example",
        "http://local.attacker.example",
        # not an address at all
        "http://10.0.0",
        "http://10.0.0.256",
        "http://[fd00::1]:9000",
    ],
)
def test_plain_http_to_anything_else_is_refused(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LUXSWIRL_ALLOW_INSECURE_HTTP", raising=False)
    with pytest.raises(ValueError, match="HTTPS required"):
        validate_server_url(url)


def test_the_override_admits_external_http_only_when_exactly_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LUXSWIRL_ALLOW_INSECURE_HTTP", "1")
    with pytest.raises(ValueError, match="HTTPS required"):
        validate_server_url("http://monitor.example.com")

    monkeypatch.setenv("LUXSWIRL_ALLOW_INSECURE_HTTP", "true")
    validate_server_url("http://monitor.example.com")


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("//monitor.example.com:9000", "scheme"),
        ("http://", "hostname"),
        ("ftp://monitor.example.com", "Must be http or https"),
        ("file:///etc/passwd", "hostname"),
    ],
)
def test_a_malformed_url_is_refused(url: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_server_url(url)


def test_the_refusal_says_how_to_fix_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LUXSWIRL_ALLOW_INSECURE_HTTP", raising=False)
    with pytest.raises(ValueError) as raised:
        validate_server_url("http://monitor.example.com:9443")
    assert "https://monitor.example.com:9443" in str(raised.value)
