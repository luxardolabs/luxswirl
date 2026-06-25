"""Unit tests for the shared SSRF guard (apps/shared/ssrf.py) and the server's
``validate_check_target`` wrapper.

Cloud-metadata / link-local literals resolve to themselves, so these tests never
touch the network. They cover the logic both the server (create time) and the
agent (fetch time) rely on.
"""

from __future__ import annotations

import pytest
from shared.ssrf import (
    SsrfBlockedError,
    assert_ip_allowed,
    assert_target_allowed,
    extract_host,
)

from app.core.check_target_validator import CheckTargetBlockedError, validate_check_target

METADATA_IP = "169.254.169.254"  # AWS/GCP/Azure IMDS


# --- assert_ip_allowed -------------------------------------------------------


def test_assert_ip_allowed_blocks_cloud_metadata():
    with pytest.raises(SsrfBlockedError):
        assert_ip_allowed(METADATA_IP)


def test_assert_ip_allowed_blocks_ipv6_link_local():
    with pytest.raises(SsrfBlockedError):
        assert_ip_allowed("fe80::1")


def test_assert_ip_allowed_allows_public_ip():
    assert_ip_allowed("8.8.8.8")  # no raise


def test_assert_ip_allowed_private_allowed_by_default():
    assert_ip_allowed("10.0.0.5")  # block_private_networks defaults False


def test_assert_ip_allowed_private_blocked_when_requested():
    with pytest.raises(SsrfBlockedError):
        assert_ip_allowed("10.0.0.5", block_private_networks=True)


def test_assert_ip_allowed_ignores_non_ip():
    assert_ip_allowed("not-an-ip")  # no raise — caller's connection will fail


# --- assert_target_allowed ---------------------------------------------------


def test_assert_target_allowed_blocks_metadata_url():
    with pytest.raises(SsrfBlockedError):
        assert_target_allowed(f"http://{METADATA_IP}/latest/meta-data/")


def test_assert_target_allowed_blocks_metadata_host_port():
    with pytest.raises(SsrfBlockedError):
        assert_target_allowed(f"{METADATA_IP}:80")


def test_assert_target_allowed_blocks_metadata_connection_string():
    with pytest.raises(SsrfBlockedError):
        assert_target_allowed(f"postgresql://u:p@{METADATA_IP}:5432/db")


def test_assert_target_allowed_allows_public():
    assert_target_allowed("https://8.8.8.8/")  # no raise


def test_assert_target_allowed_noop_when_nothing_blocked():
    # Both toggles off — must not raise even for the metadata IP.
    assert_target_allowed(
        f"http://{METADATA_IP}/",
        block_cloud_metadata=False,
        block_private_networks=False,
    )


def test_assert_target_allowed_unresolvable_host_does_not_raise():
    # Resolution failure → []; nothing to forge a request to, so allow.
    assert_target_allowed("http://nonexistent.invalid./")


# --- extract_host ------------------------------------------------------------


@pytest.mark.parametrize(
    "target,expected",
    [
        ("http://example.com/path", "example.com"),
        ("https://example.com:8443/x", "example.com"),
        ("example.com:9000", "example.com"),
        ("mysql://user:pass@db.internal:3306/app", "db.internal"),
        ("plainhost", "plainhost"),
        (f"http://{METADATA_IP}/", METADATA_IP),
    ],
)
def test_extract_host(target, expected):
    assert extract_host(target) == expected


# --- validate_check_target (server wrapper) ----------------------------------


def test_validate_check_target_blocks_metadata_and_hints_settings():
    with pytest.raises(CheckTargetBlockedError) as exc:
        validate_check_target(f"http://{METADATA_IP}/")
    assert "Settings" in str(exc.value)


def test_validate_check_target_allows_public():
    validate_check_target("https://8.8.8.8/")  # no raise


def test_validate_check_target_private_blocked_when_requested():
    with pytest.raises(CheckTargetBlockedError):
        validate_check_target("http://192.168.1.10/", block_private_networks=True)
