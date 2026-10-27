"""Request context for MCP tool handlers via contextvars.

Port of LuxWX's `app/mcp/context.py`. MCP tool handlers are invoked by the MCP
framework, not by FastAPI, so they cannot use FastAPI's dependency injection. The
auth middleware binds the authenticated principal and the request's origin; each
tool call binds the session it runs in. Handlers read them back.

LuxWX's tenant and unit-system variables have no counterpart: LuxSwirl serves one
organisation, and the principal (the token's user, role and scopes) is the whole
identity. Nor do its deferred key counters: an API token keeps only `last_used_at`,
which authentication stamps in a transaction of its own.
"""

from contextvars import ContextVar

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.api_token_schema import ApiPrincipal

_db_var: ContextVar[AsyncSession] = ContextVar("mcp_db")
_principal_var: ContextVar[ApiPrincipal | None] = ContextVar(
    "mcp_principal", default=None
)
# Where the call came from. Security telemetry — the credential runs on someone else's
# hardware, so origin is how a leaked token becomes visible.
_origin_var: ContextVar[tuple[str | None, str | None]] = ContextVar(
    "mcp_origin", default=(None, None)
)


def set_principal(principal: ApiPrincipal) -> None:
    """Bind who the current MCP request acts as. Set by the auth middleware."""
    _principal_var.set(principal)


def set_db(db: AsyncSession) -> None:
    """Bind the session the current tool call runs in. Set by `call_tool`."""
    _db_var.set(db)


def set_origin(ip_address: str | None, user_agent: str | None) -> None:
    """Bind the caller's origin for this request. Set by the auth middleware."""
    _origin_var.set((ip_address, user_agent))


def get_origin() -> tuple[str | None, str | None]:
    """(ip_address, user_agent) for the current request, or (None, None)."""
    return _origin_var.get()


def get_db() -> AsyncSession:
    return _db_var.get()


def get_principal() -> ApiPrincipal | None:
    return _principal_var.get()
