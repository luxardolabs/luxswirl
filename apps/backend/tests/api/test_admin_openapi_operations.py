"""The admin API's OpenAPI operations are named after their handlers, not their routes.

A client generated from `/api/v1/admin/openapi.json` takes its method names from
the operationIds, and a reader sees the summaries. Route names are
`api.admin.<resource>.<action>`; neither may leak into the document.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.core.admin_openapi import admin_openapi_schema
from app.main import app

pytestmark = pytest.mark.unit


def _operations() -> list[tuple[str, str, dict[str, Any]]]:
    doc = admin_openapi_schema(app)
    return [
        (method, path, op)
        for path, item in doc["paths"].items()
        for method, op in item.items()
    ]


def test_no_summary_or_operation_id_carries_a_route_name() -> None:
    leaked = [
        (method, path, op.get("summary"), op.get("operationId"))
        for method, path, op in _operations()
        if "." in (op.get("summary") or "") or "api_admin_" in op["operationId"]
    ]
    assert leaked == []


def test_operation_ids_are_the_handlers() -> None:
    expected = {
        ("get", "/api/v1/admin/agents"): "admin_list_agents_api_v1_admin_agents_get",
        ("get", "/api/v1/admin/me"): "admin_me_api_v1_admin_me_get",
        (
            "post",
            "/api/v1/admin/agents/{agent_id}/approve",
        ): "admin_approve_agent_api_v1_admin_agents__agent_id__approve_post",
        (
            "put",
            "/api/v1/admin/status-pages/{status_page_id}/items",
        ): "admin_set_status_page_items_api_v1_admin_status_pages__status_page_id__items_put",
    }
    got = {(method, path): op["operationId"] for method, path, op in _operations()}

    assert {key: got.get(key) for key in expected} == expected


def test_a_route_without_a_summary_reads_as_its_handler() -> None:
    got = {(method, path): op.get("summary") for method, path, op in _operations()}

    assert got[("get", "/api/v1/admin/agents")] == "Admin List Agents"
