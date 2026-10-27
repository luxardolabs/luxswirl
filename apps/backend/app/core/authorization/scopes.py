"""Granular OAuth-style scope vocabulary with wildcards.

Port of LuxPM's `app/core/authorization/scopes.py` (and LuxWX's port of it):
the functions are the fleet's, verbatim; only the families are LuxSwirl's.

Format: `family:action`
- Family: one per admin-API resource (checks, agents, alerts, ...)
- Action: read, write, admin

Wildcards:
- `<family>:*` -- all actions on that family (checks:* -> checks:read + checks:write + checks:admin)
- `*:<action>` -- that action across all families (*:read -> every read)
- `*` -- everything

Resolution: a scope set "grants" a required scope iff the required atom
is present in the expanded form of the scope set. Pure functions only;
no I/O, no DB.
"""

import re

# The complete vocabulary. Adding a new family or action is a one-line change.
FAMILIES: frozenset[str] = frozenset(
    {
        "agents",
        "alerts",
        "checks",
        "jobs",
        "notifications",
        "registration_keys",
        "scheduler",
        "settings",
        "status_pages",
        "users",
    }
)

ACTIONS: frozenset[str] = frozenset({"read", "write", "admin"})

# Default scope set for tokens with empty/null `scopes`. Least privilege:
# read-only across the platform. Write/admin must be granted explicitly.
DEFAULT_SCOPES: tuple[str, ...] = ("*:read",)

# A scope must look like `family:action`, `family:*`, `*:action`, or `*`.
_SCOPE_PATTERN = re.compile(r"^(\*|[a-z_]+):(\*|[a-z_]+)$|^\*$")


def parse_scope_string(scope: str) -> tuple[str, str]:
    """Split a scope into (family, action). `*` is normalized to (`*`, `*`)."""
    if scope == "*":
        return "*", "*"
    if not _SCOPE_PATTERN.match(scope):
        raise ValueError(f"Invalid scope syntax: {scope!r}")
    family, action = scope.split(":", 1)
    return family, action


def validate_scope(scope: str) -> bool:
    """True if the scope is well-formed AND uses known family/action values.

    Wildcards always validate. Future families/actions added to the
    constants above immediately validate without code changes elsewhere.
    """
    try:
        family, action = parse_scope_string(scope)
    except ValueError:
        return False
    if family != "*" and family not in FAMILIES:
        return False
    return not (action != "*" and action not in ACTIONS)


def expand_wildcards(scopes: list[str] | tuple[str, ...]) -> frozenset[str]:
    """Expand a scope set into the full set of concrete `family:action` atoms it grants.

    Invalid scopes are silently dropped (not raised) so a malformed entry
    never bricks an entire token. Validation belongs at write time, not
    read time.
    """
    atoms: set[str] = set()
    for scope in scopes:
        if not validate_scope(scope):
            continue
        family, action = parse_scope_string(scope)
        family_set = FAMILIES if family == "*" else {family}
        action_set = ACTIONS if action == "*" else {action}
        for f in family_set:
            for a in action_set:
                atoms.add(f"{f}:{a}")
    return frozenset(atoms)


def scope_grants(
    granted_scopes: list[str] | tuple[str, ...] | None,
    required: str,
) -> bool:
    """True iff the granted scope set grants the required atomic scope.

    `granted_scopes` may be None or empty -- DEFAULT_SCOPES is used in that
    case (read-only across the platform).

    `required` should be a concrete `family:action` -- not a wildcard.
    Asking "does this token grant `*`?" is meaningless; check for `*:admin`
    or some specific action instead.
    """
    if not granted_scopes:
        granted_scopes = DEFAULT_SCOPES

    if not validate_scope(required):
        return False
    family, action = parse_scope_string(required)
    if family == "*" or action == "*":
        # Required must be concrete; wildcard required is always treated as ungranted.
        return False

    return required in expand_wildcards(granted_scopes)
