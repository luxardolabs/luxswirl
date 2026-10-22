"""Bulk-import URL handling: the check type, target and generated name a URL
becomes, shared by the bulk create and the bulk preview.

The preview probe is exercised only for non-http schemes here, which never
touch the network.
"""

from __future__ import annotations

from urllib.parse import urlparse

import pytest
from app.services.core.check_core_service import CheckCoreService


@pytest.mark.parametrize(
    ("url", "check_type", "full_url"),
    [
        ("https://example.com/health", "http", "https://example.com/health"),
        ("http://example.com", "http", "http://example.com"),
        ("example.com/api", "http", "https://example.com/api"),
        ("tcp://db.internal:5432", "tcp", "tcp://db.internal:5432"),
    ],
)
def test_classify_bulk_url(url: str, check_type: str, full_url: str) -> None:
    got_type, got_url, parsed = CheckCoreService._classify_bulk_url(url)
    assert (got_type, got_url) == (check_type, full_url)
    assert parsed == urlparse(full_url)


@pytest.mark.parametrize(
    ("url", "name"),
    [
        ("https://example.com", "example-com"),
        ("https://example.com/", "example-com"),
        ("https://api.example.com/v1/health/", "api-example-com-v1-health"),
        ("https://example.com:8443/x", "example-com-x-8443"),
        ("https://example.com:443/x", "example-com-x"),
        ("http://example.com:80", "example-com"),
    ],
)
def test_bulk_display_name(url: str, name: str) -> None:
    assert CheckCoreService._bulk_display_name(urlparse(url)) == name


def test_bulk_display_name_is_capped_at_255() -> None:
    long_path = "a/" * 300
    name = CheckCoreService._bulk_display_name(urlparse(f"https://h.io/{long_path}"))
    assert len(name) == 255
    assert name.startswith("h-io-a-a")


async def test_preview_of_a_non_http_url_is_not_probed() -> None:
    [row] = await CheckCoreService.bulk_preview_checks(["tcp://db.internal:5432"])
    assert row == {
        "url": "tcp://db.internal:5432",
        "generated_name": "db-internal-5432",
        "check_type": "tcp",
        "validation_status": "unknown",
        "validation_message": "",
        "status_code": None,
    }


async def test_preview_of_an_unparseable_url_is_an_error_row() -> None:
    # An out-of-range port raises when the name is built.
    [row] = await CheckCoreService.bulk_preview_checks(["tcp://host:99999"])
    assert row["validation_status"] == "error"
    assert row["validation_message"].startswith("Parse error:")
    assert row["generated_name"] == "tcp://host:99999"
