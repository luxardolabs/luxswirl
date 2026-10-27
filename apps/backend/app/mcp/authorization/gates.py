"""The MCP authorization chokepoint — port of LuxPM's (and LuxWX's) `gates.py`.

`authorize_tool_call()` runs before every dispatch. Two independent axes, in order:

  1. **Role**       — does the token's user hold the role the tool's route needs?
  2. **Capability** — does the token's scope set grant every scope the tool needs?

The resource axis LuxPM and LuxWX add (project / station pin within a tenant) has no
counterpart in LuxSwirl, which serves one organisation. Scopes only narrow: a scope
never lets a user do what their role does not.
"""

from app.core.authorization import scope_grants
from app.mcp.authorization.errors import AuthorizationError
from app.mcp.authorization.registry import get_gate, is_meta_tool
from app.models.enum_model import MCPDenialCode, UserRole
from app.schemas.api_token_schema import ApiPrincipal

_RANK = {UserRole.VIEWER: 0, UserRole.EDITOR: 1, UserRole.ADMIN: 2}


def may_call(principal: ApiPrincipal, tool_name: str) -> bool:
    """Whether `principal` may call `tool_name`: the test `list_tools` filters by."""
    try:
        authorize_tool_call(principal=principal, tool_name=tool_name)
    except AuthorizationError:
        return False
    return True


def authorize_tool_call(*, principal: ApiPrincipal, tool_name: str) -> None:
    """Authorize an MCP tool call. Raises AuthorizationError on deny, returns None on allow."""
    # Meta/introspection tools describe the token itself, so they bypass role + scope.
    if is_meta_tool(tool_name):
        return

    gate = get_gate(tool_name)
    if gate is None:
        # Unregistered == denied. This is what makes "a new tool cannot ship ungated"
        # true by construction rather than by review.
        raise AuthorizationError(
            MCPDenialCode.UNKNOWN_TOOL,
            f"Unknown tool: {tool_name}",
        )

    # 1. Role.
    if _RANK[principal.user.role] < _RANK[gate.role]:
        raise AuthorizationError(
            MCPDenialCode.ROLE_INSUFFICIENT,
            f"Tool {tool_name} requires the {gate.role.value} role",
        )

    # 2. Capability.
    for scope in gate.scopes:
        if not scope_grants(principal.scopes, scope):
            raise AuthorizationError(
                MCPDenialCode.SCOPE_INSUFFICIENT,
                f"Tool {tool_name} requires scope {scope}",
                required_scope=scope,
            )
