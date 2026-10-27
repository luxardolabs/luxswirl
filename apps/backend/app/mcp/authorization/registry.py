"""Authorization registry: every MCP tool mapped to the scopes and role it needs.

Port of LuxPM's (and LuxWX's) `registry.py`. This is the single source of truth for
the MCP security model. **A tool with no registry entry is DENIED, not allowed**, and
a tool exposed without an entry fails `tests/mcp_server/test_mcp_authorization.py`.

Each entry mirrors the admin-API route the tool stands for:
- `scopes`: every concrete `family:action` the call needs (app/core/authorization).
  A tool that reaches a second family names both, as the route does.
- `role`: the lowest user role the route takes (ApiUser / ApiEditor / ApiAdmin).

LuxPM's and LuxWX's resource resolvers (project, station) have no counterpart:
LuxSwirl has one organisation, so the token's user and role are the whole boundary.
"""

from dataclasses import dataclass

from app.models.enum_model import UserRole


@dataclass(frozen=True)
class ToolGate:
    """Authorization requirements for a single MCP tool."""

    scopes: tuple[str, ...]
    role: UserRole = UserRole.VIEWER


_EDITOR = UserRole.EDITOR

# ----------------------------------------------------------------------
# The registry. Grouped by family; alphabetical within each group.
# ----------------------------------------------------------------------

REGISTRY: dict[str, ToolGate] = {
    # --- Agents -------------------------------------------------------------
    "get_agent": ToolGate(("agents:read",)),
    "list_agents": ToolGate(("agents:read",)),
    # --- Alerts -------------------------------------------------------------
    "get_alert": ToolGate(("alerts:read",)),
    "list_alerts": ToolGate(("alerts:read",)),
    # Snoozing is any user's, as on the web and the admin API.
    "snooze_alert_check": ToolGate(("alerts:write",)),
    "unsnooze_alert_check": ToolGate(("alerts:write",)),
    # --- Checks -------------------------------------------------------------
    "clone_check": ToolGate(("checks:write",), _EDITOR),
    "create_check": ToolGate(("checks:write",), _EDITOR),
    "disable_check": ToolGate(("checks:write",), _EDITOR),
    "enable_check": ToolGate(("checks:write",), _EDITOR),
    "get_check": ToolGate(("checks:read",)),
    "get_check_history": ToolGate(("checks:read",)),
    "get_check_results": ToolGate(("checks:read",)),
    "get_check_summary": ToolGate(("checks:read",)),
    "list_checks": ToolGate(("checks:read",)),
    "update_check": ToolGate(("checks:write",), _EDITOR),
    # --- Jobs ---------------------------------------------------------------
    "create_checks_from_job": ToolGate(("jobs:write", "checks:write"), _EDITOR),
    "create_job": ToolGate(("jobs:write",), _EDITOR),
    "get_job": ToolGate(("jobs:read",)),
    "get_maintenance_job": ToolGate(("jobs:read",)),
    "list_job_types": ToolGate(("jobs:read",)),
    "list_jobs": ToolGate(("jobs:read",)),
}


def get_gate(tool_name: str) -> ToolGate | None:
    """Return the gate for a tool, or None if unregistered (which the gate treats as DENY)."""
    return REGISTRY.get(tool_name)


# Tools that bypass scope + role checks: introspection about the TOKEN ITSELF, not
# about any monitoring data. Available to any authenticated token.
META_TOOLS: frozenset[str] = frozenset({"whoami"})


def is_meta_tool(tool_name: str) -> bool:
    return tool_name in META_TOOLS


def registered_tools() -> frozenset[str]:
    """Every tool name known to the registry."""
    return frozenset(REGISTRY.keys())
