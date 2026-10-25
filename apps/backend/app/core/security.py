"""
Security utilities - authentication and authorization.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import urlencode
from uuid import UUID

import bcrypt
import jwt
from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.safe_redirect import safe_redirect_target
from app.crud import admin_bootstrap_crud
from app.db import get_db
from app.models.enum_model import AgentApprovalStatus
from app.schemas.auth_schema import CurrentUserDTO
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.registration_key_core_service import (
    RegistrationKeyCoreService,
)
from app.shared.logger import get_logger

if TYPE_CHECKING:
    from app.models.agent_model import Agent

logger = get_logger("luxswirl.security")

# Why an agent that is not active is refused, as it is told.
_REFUSED_STATUS_DETAIL = {
    AgentApprovalStatus.PENDING: "Agent registration pending approval",
    AgentApprovalStatus.REJECTED: "Agent registration was rejected",
    AgentApprovalStatus.DISABLED: "Agent access has been disabled",
    AgentApprovalStatus.PAUSED: "Agent is paused",
    AgentApprovalStatus.ARCHIVED: "Agent is archived",
}


# ========================================================================
# SESSION-BASED AUTHENTICATION (for Web UI)
# ========================================================================


async def get_current_user(
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    session_token: Annotated[
        str | None, Cookie(alias=settings.security.session_cookie_name)
    ] = None,
) -> CurrentUserDTO:
    """
    Get current authenticated user from session cookie.

    Args:
        session_token: Session token from HTTP-only cookie
        db: Database session

    Returns:
        The authenticated principal as a CurrentUserDTO — never the ORM model. The
        conversion happens here, inside the request's session scope, so nothing
        downstream can lazy-load off a detached row.

    Raises:
        HTTPException: If not authenticated or session invalid

    Usage:
        @router.get("/api/v1/me")
        async def get_me(user: CurrentUser):
            return user
    """
    if not session_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated - no session cookie",
        )

    auth_service = AuthCoreService()
    session = await auth_service.verify_session(db, session_token)

    if not session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session",
        )

    # Session is valid — convert to the DTO here, in session scope.
    return CurrentUserDTO.model_validate(session.user)


async def get_current_active_user(
    current_user: Annotated[CurrentUserDTO, Depends(get_current_user)],
) -> CurrentUserDTO:
    """
    Get current active user (must not be inactive or locked).

    Args:
        current_user: Current user from get_current_user

    Returns:
        Active CurrentUserDTO

    Raises:
        HTTPException: If user is inactive or locked

    Usage:
        @router.get("/api/v1/agents")
        async def list_agents(
            user: Annotated[CurrentUserDTO, Depends(get_current_active_user)],
        ):
            return agents
    """
    if not current_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is inactive",
        )

    if current_user.is_locked:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Account is locked until {current_user.locked_until}",
        )

    return current_user


def require_role(
    *allowed_roles: str,
) -> Callable[[CurrentUserDTO], Awaitable[CurrentUserDTO]]:
    """
    Dependency factory for role-based access control.

    Args:
        *allowed_roles: Roles that are allowed (e.g., "admin", "editor")

    Returns:
        Dependency function

    Usage:
        @router.post("/api/v1/users", dependencies=[Depends(require_role("admin"))])
        async def create_user(...):
            ...

        # Or to get the user:
        @router.get("/api/v1/settings")
        async def get_settings(
            user: Annotated[
                CurrentUserDTO, Depends(require_role("admin", "editor"))
            ],
        ):
            ...
    """

    async def role_checker(
        user: Annotated[CurrentUserDTO, Depends(get_current_active_user)],
    ) -> CurrentUserDTO:
        """Check if user has required role."""
        if user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient permissions. Required role: {' or '.join(allowed_roles)}",
            )
        return user

    return role_checker


# Convenience dependencies for common role requirements
async def require_editor(
    user: Annotated[CurrentUserDTO, Depends(get_current_active_user)],
) -> CurrentUserDTO:
    """Require editor role or higher (admin, editor)."""
    if user.role not in ("admin", "editor"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Editor or Admin access required",
        )
    return user


async def get_optional_user(
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    session_token: Annotated[
        str | None, Cookie(alias=settings.security.session_cookie_name)
    ] = None,
) -> CurrentUserDTO | None:
    """
    Get current user if authenticated, None otherwise.

    Useful for public pages that change behavior when authenticated.

    Args:
        session_token: Session token from cookie
        db: Database session

    Returns:
        CurrentUserDTO or None
    """
    if not session_token:
        return None

    try:
        # By keyword: these signatures are dependency signatures, so their
        # parameter ORDER is not part of their contract (FastAPI resolves by
        # name). A positional call here broke four public pages the moment the
        # order changed.
        return await get_current_user(db=db, session_token=session_token)
    except HTTPException:
        return None


# ========================================================================
# WEB UI SPECIFIC - REDIRECTS TO LOGIN ON AUTH FAILURE
# ========================================================================


async def _unauthenticated_web_redirect(
    request: Request, db: AsyncSession
) -> HTTPException:
    """Build the redirect for an unauthenticated web request.

    First run (no user at all, FLEET-ADMIN-BOOTSTRAP-STANDARD) -> /setup, since no
    credentials exist yet. Otherwise -> /login, preserving the originally
    requested path. Both targets are resolved by route name.
    """
    if await admin_bootstrap_crud.count_users(db) == 0:
        return HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": request.app.url_path_for("admin.setup")},
        )
    # The requested path rides as an encoded query value; the whole target still
    # goes through the one safe-redirect helper, since part of it is the request's.
    login = request.app.url_path_for("login_page")
    target = f"{login}?{urlencode({'redirect': request.url.path})}"
    return HTTPException(
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
        headers={"Location": safe_redirect_target(target, default=login)},
    )


async def get_current_user_web(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    session_token: Annotated[
        str | None, Cookie(alias=settings.security.session_cookie_name)
    ] = None,
) -> CurrentUserDTO:
    """
    Get current authenticated user for web routes.

    Redirects to /login if not authenticated (instead of returning JSON error).

    Args:
        request: FastAPI request object
        session_token: Session token from HTTP-only cookie
        db: Database session

    Returns:
        The authenticated principal as a CurrentUserDTO (converted in session scope).

    Raises:
        HTTPException with 307 redirect to login (or /setup on first run)
    """
    if not session_token:
        # No session: first-run -> /setup wizard, otherwise /login.
        raise await _unauthenticated_web_redirect(request, db)

    auth_service = AuthCoreService()
    session = await auth_service.verify_session(db, session_token)

    if not session:
        raise await _unauthenticated_web_redirect(request, db)

    # A seeded or reset password must be changed before any other page
    # (FLEET-ADMIN-BOOTSTRAP-STANDARD §5): login sends the user to the form, and
    # every other web route sends them back to it until they change it.
    change_password = request.app.url_path_for("change_password_page")
    if session.user.must_change_password and request.url.path not in {
        change_password,
        request.app.url_path_for("web_logout"),
    }:
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": f"{change_password}?required=true"},
        )

    # Session is valid — convert to the DTO here, in session scope.
    return CurrentUserDTO.model_validate(session.user)


async def require_admin_web(
    request: Request,
    current_user: Annotated[CurrentUserDTO, Depends(get_current_user_web)],
) -> CurrentUserDTO:
    """Require admin role for web routes (redirects non-admins to home)."""
    if current_user.role != "admin":
        # Redirect to home page with error message
        home = request.app.url_path_for("root_redirect")
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": f"{home}?error=admin_required"},
        )
    return current_user


async def require_editor_web(
    request: Request,
    current_user: Annotated[CurrentUserDTO, Depends(get_current_user_web)],
) -> CurrentUserDTO:
    """Require editor role or higher for web routes (redirects viewers to home)."""
    if current_user.role not in ("admin", "editor"):
        # Redirect to home page with error message
        home = request.app.url_path_for("root_redirect")
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": f"{home}?error=editor_required"},
        )
    return current_user


# Typed dependency aliases live with their SURFACE, not here: `app/api/deps`
# (refuses with 401/403) and `app/web/deps` (refuses with a redirect). The two
# differ in how they refuse, and that difference is the surface's contract.


# ========================================================================
# JWT AND AGENT TOKEN AUTHENTICATION (for API and Agents)
# ========================================================================


def create_access_token(
    data: dict[str, Any], expires_delta: timedelta | None = None
) -> str:
    """
    Create a JWT access token.

    Args:
        data: Dictionary of claims to encode in the token
        expires_delta: Optional custom expiration time

    Returns:
        Encoded JWT token
    """
    to_encode = data.copy()

    if expires_delta:
        expire = utc_now() + expires_delta
    else:
        expire = utc_now() + timedelta(
            minutes=settings.security.access_token_expire_minutes
        )

    to_encode.update({"exp": expire})
    encoded_jwt: str = jwt.encode(
        to_encode,
        settings.security.secret_key,
        algorithm=settings.security.algorithm,
    )

    return encoded_jwt


def verify_token(token: str) -> dict[str, Any]:
    """
    Verify and decode a JWT token.

    Args:
        token: JWT token to verify

    Returns:
        Decoded token payload

    Raises:
        HTTPException: If token is invalid or expired
    """
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            settings.security.secret_key,
            algorithms=[settings.security.algorithm],
        )
        return payload
    except jwt.PyJWTError as e:
        logger.warning("Token verification failed", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


async def authenticate_heartbeat(
    agent: Agent,
    authorization: str | None,
    db: AsyncSession,
) -> bool:
    """Authenticate a heartbeat; True when the agent's own key was used.

    An agent with its own key must present it; only a 401 from that check
    falls back to the registration key (an approved agent that has not stored
    its key yet). Any other refusal (a paused or disabled agent) stands. The
    registration key is shared by every install, so a False here never lets
    the heartbeat write onto an agent that has its own key: core only reads
    that agent's status back (`AgentCoreService.process_heartbeat`).
    """
    if agent.api_key_hash:
        try:
            await verify_agent_token(agent, authorization)
            return True
        except HTTPException as e:
            if e.status_code != 401:
                raise
    await verify_registration_token(authorization, db)
    return False


async def verify_registration_token(
    authorization: Annotated[str | None, Header()] = None,
    db: AsyncSession | None = None,
) -> str:
    """
    Verify registration token (shared token for agent registration and recovery).

    Args:
        authorization: Authorization header value
        db: Database session for token validation

    Returns:
        Valid token

    Raises:
        HTTPException: If token is invalid

    Example:
        Used for initial agent registration and key recovery
    """
    if not settings.security.auth_enabled:
        return "disabled"

    if authorization is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Extract token from "Bearer <token>"
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization header format. Expected: Bearer <token>",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = parts[1]

    # Validate against registration keys in database
    if db is not None:
        reg_key = await RegistrationKeyCoreService.verify_key_and_update_usage(
            db, token
        )
        if reg_key:
            logger.info(
                "Valid registration token used",
                extra={
                    "reg_key_name": reg_key.name,
                    "reg_key_id": str(reg_key.id),
                },
            )
            return token

    # Token not found or invalid
    logger.warning("Invalid registration token attempt")
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid registration token",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def verify_recovery_registration_key(
    agent_id: UUID, authorization: str | None, db: AsyncSession
) -> None:
    """The registration-key half of an agent's key recovery, audited.

    Called in the recover-key route BODY, not as a dependency: the route's rate
    limit wraps the body, so a refusal raised from a dependency would never be
    counted and the key could be guessed without limit. The other half, the
    agent's own recovery secret, is checked in core. A refusal here is a failed
    recovery, so it is a SECURITY AUDIT line like the ones core writes.
    """
    try:
        await verify_registration_token(authorization, db)
    except HTTPException as refused:
        logger.warning(
            "SECURITY AUDIT: Agent key recovery refused",
            extra={
                "event": "agent_key_recovery_refused",
                "agent_id": str(agent_id),
                "reason": "registration_key",
            },
        )
        # verify_registration_token refuses only with 401; say so here.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=refused.detail,
            headers=refused.headers,
        ) from None


async def verify_agent_token(
    agent: Agent,
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """
    Verify agent-specific API token and check approval status.

    Authentication flow:
    1. Extract token from Authorization header
    2. If agent has agent-specific key (api_key_hash):
       - Validate token against agent.api_key_hash using bcrypt
       - Update api_key_last_used on success
    4. If agent has no key (newly approved):
       - This shouldn't happen - keys are auto-generated on approval
       - Return 401 and instruct to call /get-api-key endpoint
    5. Check agent approval_status: only an active agent is let in

    Args:
        agent: The already-resolved agent to authenticate
        authorization: Authorization header value

    Returns:
        Valid token

    Raises:
        HTTPException: If token is invalid or agent status prevents access
    """
    if not settings.security.auth_enabled:
        return "disabled"

    if authorization is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Extract token from "Bearer <token>"
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization header format. Expected: Bearer <token>",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = parts[1]

    # Check if agent has an agent-specific key
    if agent.api_key_hash:
        # Validate token against agent's specific key
        try:
            is_valid = bcrypt.checkpw(
                token.encode("utf-8"), agent.api_key_hash.encode("utf-8")
            )
        except Exception:
            logger.exception(
                "Error verifying agent key",
                extra={"agent_id": str(agent.id)},
            )
            is_valid = False

        if not is_valid:
            logger.warning(
                "Invalid agent-specific key attempt",
                extra={"agent_id": str(agent.id)},
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid agent API key",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Update last used timestamp — get_db() will commit on request return.
        agent.api_key_last_used = utc_now()

        logger.debug(
            "Agent authenticated with agent-specific key",
            extra={"agent_id": str(agent.id)},
        )

    else:
        # Agent has no agent-specific key yet
        # This shouldn't happen if approval flow is working correctly
        logger.error(
            "Agent has no API key hash - approval flow issue",
            extra={"agent_id": str(agent.id)},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Agent has no API key. Contact administrator to regenerate key.",
        )

    # Only an active agent is let in; any other status, one added later
    # included, is refused.
    if agent.approval_status != AgentApprovalStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=_REFUSED_STATUS_DETAIL.get(
                agent.approval_status, "Agent is not active"
            ),
        )

    return token
