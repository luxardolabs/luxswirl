"""Authorization layer: the fleet's scope vocabulary, ported from LuxPM.

- `scopes.py` -- granular OAuth-style `family:action` scopes with wildcards.
  An API token carries a scope set; it narrows the token's user's role, on
  the admin API (`app/api/deps.py`) and the MCP server alike.

See docs/user-guide/admin-api.md for the public scope vocabulary.
"""

from app.core.authorization.scopes import (
    ACTIONS,
    DEFAULT_SCOPES,
    FAMILIES,
    expand_wildcards,
    parse_scope_string,
    scope_grants,
    validate_scope,
)

__all__ = [
    "ACTIONS",
    "DEFAULT_SCOPES",
    "FAMILIES",
    "expand_wildcards",
    "parse_scope_string",
    "scope_grants",
    "validate_scope",
]
