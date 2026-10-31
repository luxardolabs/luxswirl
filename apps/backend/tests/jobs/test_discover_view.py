"""A network_discover result's scan links ride on the things they scan.

The template looked each link up as `scan_form_urls[iface.suggested_scan]`;
the view now returns the inferred network's link and each interface with its
own `scan_url` (None when it suggests no scan).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from app.services.views.jobs_view_service import build_discover_view

pytestmark = pytest.mark.unit


class _Request:
    def url_for(self, name: str, **_: Any) -> SimpleNamespace:
        assert name == "jobs.new"
        return SimpleNamespace(path="/jobs/create-form")


def _subnet(url: str | None) -> str | None:
    if url is None:
        return None
    query = parse_qs(urlparse(url).query)
    assert query["job_type"] == ["network_scan"]
    return json.loads(query["prefill_params"][0])["subnet"]


def test_links_for_the_network_and_each_scannable_interface() -> None:
    job = SimpleNamespace(
        runner_token="agent-1",
        result={
            "inferred_network": "10.0.0.0/24",
            "interfaces": [
                {"name": "eth0", "suggested_scan": "10.0.0.0/24"},
                {"name": "docker0"},
                {"name": "eth1", "suggested_scan": "192.168.5.0/24"},
            ],
        },
    )

    view = build_discover_view(_Request(), job)

    assert _subnet(view["inferred_scan_url"]) == "10.0.0.0/24"
    assert [
        (i["name"], _subnet(i["scan_url"])) for i in view["discover_interfaces"]
    ] == [
        ("eth0", "10.0.0.0/24"),
        ("docker0", None),
        ("eth1", "192.168.5.0/24"),
    ]


def test_an_empty_result_offers_nothing() -> None:
    view = build_discover_view(
        _Request(), SimpleNamespace(runner_token="", result=None)
    )

    assert view == {"inferred_scan_url": None, "discover_interfaces": []}
