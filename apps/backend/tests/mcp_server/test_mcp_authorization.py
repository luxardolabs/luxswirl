"""The MCP authorization registry covers every tool, and the gate holds it.

Ported from LuxWX's registry-coverage tests: a tool exposed without a registry entry
fails here, so "a new tool cannot ship ungated" is true by construction. Every
registered scope names a real family and action, and every tool's scopes include its
own family, as the admin route it stands for requires.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from app.core.authorization import validate_scope
from app.mcp.authorization import (
    META_TOOLS,
    REGISTRY,
    AuthorizationError,
    authorize_tool_call,
)
from app.mcp.tools import ALL_TOOLS, HANDLERS
from app.models.enum_model import MCPDenialCode, UserRole
from app.schemas.api_token_schema import ApiPrincipal
from app.schemas.auth_schema import CurrentUserDTO


def _principal(role: UserRole, scopes: tuple[str, ...]) -> ApiPrincipal:
    return ApiPrincipal(
        user=CurrentUserDTO.model_construct(
            id=uuid4(), username="agent", role=role, is_active=True
        ),
        token_id=uuid4(),
        token_name="agent",
        token_expires_at=None,
        scopes=scopes,
    )


def test_every_tool_is_registered_or_meta() -> None:
    names = {tool.name for tool in ALL_TOOLS}
    assert names - set(REGISTRY) == set(META_TOOLS)
    assert set(REGISTRY) <= names


def test_every_tool_has_a_handler() -> None:
    assert {tool.name for tool in ALL_TOOLS} == set(HANDLERS)


def test_every_registered_scope_is_a_real_concrete_scope() -> None:
    bad = [
        (name, scope)
        for name, gate in REGISTRY.items()
        for scope in gate.scopes
        if not validate_scope(scope) or "*" in scope
    ]
    assert bad == []


def test_a_change_needs_an_editor_unless_its_route_takes_any_user() -> None:
    writes = {
        name: gate
        for name, gate in REGISTRY.items()
        if any(s.endswith(":write") for s in gate.scopes)
    }
    any_user = {name for name, gate in writes.items() if gate.role == UserRole.VIEWER}
    assert any_user == {"snooze_alert_check", "unsnooze_alert_check"}


def test_an_unregistered_tool_is_denied() -> None:
    with pytest.raises(AuthorizationError) as raised:
        authorize_tool_call(
            principal=_principal(UserRole.ADMIN, ("*",)), tool_name="delete_check"
        )
    assert raised.value.code == MCPDenialCode.UNKNOWN_TOOL


def test_the_role_is_judged_before_the_scopes() -> None:
    with pytest.raises(AuthorizationError) as raised:
        authorize_tool_call(
            principal=_principal(UserRole.VIEWER, ("*",)), tool_name="create_check"
        )
    assert raised.value.code == MCPDenialCode.ROLE_INSUFFICIENT


def test_every_scope_a_tool_names_is_needed() -> None:
    principal = _principal(UserRole.EDITOR, ("jobs:read", "jobs:write"))
    with pytest.raises(AuthorizationError) as raised:
        authorize_tool_call(principal=principal, tool_name="create_checks_from_job")
    assert raised.value.code == MCPDenialCode.SCOPE_INSUFFICIENT
    assert raised.value.required_scope == "checks:write"


def test_whoami_needs_no_scope() -> None:
    authorize_tool_call(
        principal=_principal(UserRole.VIEWER, ("checks:read",)), tool_name="whoami"
    )
