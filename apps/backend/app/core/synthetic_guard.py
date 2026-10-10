"""The synthetic-check gate: who may author one, and what their script may do.

PUBLIC, and in `app/core/` beside `synthetic_security`, which it pairs with:
that module owns the AST analysis, this one owns who may author a script and the
audit record either verdict leaves. Separate, so the analysis carries no access
control.

`create_check` and `update_check`
each carried their own copy of this — the authz check, the AST validation, and
two near-identical SECURITY AUDIT log blocks — differing only in the action
name and which identifiers they logged. Two copies of an access-control check
is one copy too many: the whole point of enforcing it here rather than in the
web layer is that there is exactly one gate the JSON API cannot walk around.

A synthetic check runs operator-supplied Python on the agent host, so:
  - only an admin actor may create one, turn an existing check into one, or
    change one's script (OWASP API5, function-level authz); and
  - the script is AST-validated, which is HARDENING, not the access control.
See SECURITY.md for the threat model and why this is trusted-environments-only.
"""

from __future__ import annotations

from uuid import UUID

from shared.logger import get_logger

from app.core.exceptions import AuthorizationException
from app.core.synthetic_security import SyntheticSecurityError, validate_and_raise

logger = get_logger("luxswirl.core.synthetic_guard")

SYNTHETIC = "synthetic"


def require_admin_actor(*, actor_is_admin: bool) -> None:
    """Refuse a non-admin actor. API Bearer tokens are admin-equivalent.

    Raises:
        AuthorizationException: if the actor is not an admin.
    """
    if not actor_is_admin:
        raise AuthorizationException("Synthetic checks require admin privileges")


def validate_script(
    script_code: str,
    *,
    action: str,
    check_id: UUID | None = None,
    check_name: str | None = None,
    agent_id: UUID | None = None,
) -> None:
    """AST-validate a synthetic script, logging the verdict either way.

    Both outcomes are logged under the SECURITY AUDIT prefix, because "a script
    was accepted" is as much a part of the audit trail as "a script was
    blocked" — the accepted one is the code that then runs on a host.

    Args:
        script_code: The operator-supplied script.
        action: CREATE or UPDATE, recorded in the audit line.
        check_id: The check being updated (None on create).
        check_name: The check's display name.
        agent_id: The agent the check runs on (create only).

    Raises:
        SyntheticSecurityError: if the script does not pass validation.
    """
    try:
        validate_and_raise(script_code)
    except SyntheticSecurityError:
        logger.exception(
            "SECURITY AUDIT: Synthetic check script validation FAILED",
            extra={
                "event": "synthetic_check_blocked",
                "action": action,
                "script_length": len(script_code),
                "check_id": str(check_id) if check_id else None,
                "check_name": check_name,
                "agent_id": str(agent_id) if agent_id else None,
            },
        )
        raise
    logger.warning(
        "SECURITY AUDIT: Synthetic check script validation passed",
        extra={
            "event": f"synthetic_check_{action.lower()}",
            "action": action,
            "script_length": len(script_code),
            "check_id": str(check_id) if check_id else None,
            "check_name": check_name,
            "agent_id": str(agent_id) if agent_id else None,
        },
    )
