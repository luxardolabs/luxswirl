"""Authorization errors for the MCP surface — port of LuxPM's `core/authorization/errors.py`.

Every denial carries a machine-readable code so the agent can tell "I lack permission"
from "that thing doesn't exist" — without the message ever revealing which.
"""

from app.models.enum_model import MCPDenialCode


class AuthorizationError(Exception):
    """Raised by the gate. Never carries data the caller isn't already entitled to see."""

    def __init__(
        self,
        code: MCPDenialCode,
        message: str,
        required_scope: str | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.required_scope = required_scope
        super().__init__(message)
