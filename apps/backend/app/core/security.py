"""
Security utilities - authentication and authorization.
"""

from __future__ import annotations

import hmac
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import bcrypt
from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from jose import JWTError, jwt  # type: ignore[import-untyped]
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.db import get_db

if TYPE_CHECKING:
    from app.models.agent_model import Agent
    from app.models.user_model import User

logger = get_logger("luxswirl.security")


# ========================================================================
# SESSION-BASED AUTHENTICATION (for Web UI)
# ========================================================================


async def get_current_user(
    session_token: str | None = Cookie(None, alias=settings.security.session_cookie_name),
    db: AsyncSession = Depends(get_db),
):
    """
    Get current authenticated user from session cookie.

    Args:
        session_token: Session token from HTTP-only cookie
        db: Database session

    Returns:
        User object

    Raises:
        HTTPException: If not authenticated or session invalid

    Usage:
        @router.get("/api/v1/me")
        async def get_me(user: User = Depends(get_current_user)):
            return user
    """
    if not session_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated - no session cookie",
        )

    # Import here to avoid circular dependency
    from app.services.core.auth_core_service import AuthCoreService

    auth_service = AuthCoreService()
    session = await auth_service.verify_session(db, session_token)

    if not session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session",
        )

    # Session is valid, return user
    return session.user


async def get_current_active_user(
    current_user: User = Depends(get_current_user),
):
    """
    Get current active user (must not be inactive or locked).

    Args:
        current_user: Current user from get_current_user

    Returns:
        Active User object

    Raises:
        HTTPException: If user is inactive or locked

    Usage:
        @router.get("/api/v1/agents")
        async def list_agents(user: User = Depends(get_current_active_user)):
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


def require_role(*allowed_roles: str):
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
        async def get_settings(user: User = Depends(require_role("admin", "editor"))):
            ...
    """

    async def role_checker(user: User = Depends(get_current_active_user)):
        """Check if user has required role."""
        if user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient permissions. Required role: {' or '.join(allowed_roles)}",
            )
        return user

    return role_checker


# Convenience dependencies for common role requirements
async def require_admin(user: User = Depends(get_current_active_user)):
    """Require admin role."""
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return user


async def require_editor(user: User = Depends(get_current_active_user)):
    """Require editor role or higher (admin, editor)."""
    if user.role not in ("admin", "editor"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Editor or Admin access required",
        )
    return user


async def get_optional_user(
    session_token: str | None = Cookie(None, alias=settings.security.session_cookie_name),
    db: AsyncSession = Depends(get_db),
):
    """
    Get current user if authenticated, None otherwise.

    Useful for public pages that change behavior when authenticated.

    Args:
        session_token: Session token from cookie
        db: Database session

    Returns:
        User object or None
    """
    if not session_token:
        return None

    try:
        return await get_current_user(session_token, db)
    except HTTPException:
        return None


# ========================================================================
# WEB UI SPECIFIC - REDIRECTS TO LOGIN ON AUTH FAILURE
# ========================================================================


async def _unauthenticated_web_redirect(request: Request, db: AsyncSession) -> HTTPException:
    """Build the redirect for an unauthenticated web request.

    First run (no admin seeded from env and none in the DB) -> the /setup wizard,
    since no credentials exist yet. Otherwise -> /login, preserving the originally
    requested path. Mirrors UserCoreService.needs_setup.
    """
    # Lazy import: app.core.__init__ eagerly imports this module while
    # app.models.base is still initializing, so a top-level user_crud (-> models)
    # import here would create a circular import.
    from app.crud.user_crud import (
        UserCRUD,  # noqa: inline-import (breaks core<->models import cycle)
    )

    if not settings.security.initial_admin_password and await UserCRUD.get_first_admin(db) is None:
        return HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/setup"},
        )
    return HTTPException(
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
        headers={"Location": f"/login?redirect={request.url.path}"},
    )


async def get_current_user_web(
    request: Request,
    session_token: str | None = Cookie(None, alias=settings.security.session_cookie_name),
    db: AsyncSession = Depends(get_db),
):
    """
    Get current authenticated user for web routes.

    Redirects to /login if not authenticated (instead of returning JSON error).

    Args:
        request: FastAPI request object
        session_token: Session token from HTTP-only cookie
        db: Database session

    Returns:
        User object

    Raises:
        HTTPException with 307 redirect to login (or /setup on first run)
    """
    if not session_token:
        # No session: first-run -> /setup wizard, otherwise /login.
        raise await _unauthenticated_web_redirect(request, db)

    # Import here to avoid circular dependency
    from app.services.core.auth_core_service import AuthCoreService

    auth_service = AuthCoreService()
    session = await auth_service.verify_session(db, session_token)

    if not session:
        raise await _unauthenticated_web_redirect(request, db)

    # Session is valid, return user
    return session.user


async def require_admin_web(
    request: Request,
    current_user: User = Depends(get_current_user_web),
):
    """Require admin role for web routes (redirects non-admins to home)."""
    if current_user.role != "admin":
        # Redirect to home page with error message
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/?error=admin_required"},
        )
    return current_user


async def require_editor_web(
    request: Request,
    current_user: User = Depends(get_current_user_web),
):
    """Require editor role or higher for web routes (redirects viewers to home)."""
    if current_user.role not in ("admin", "editor"):
        # Redirect to home page with error message
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/?error=editor_required"},
        )
    return current_user


# Typed dependency aliases (CurrentUser, AdminUserWeb, …) live in
# app/core/auth_deps.py — NOT here — to avoid a models-init circular import
# (see that module's docstring).


# ========================================================================
# JWT AND AGENT TOKEN AUTHENTICATION (for API and Agents)
# ========================================================================


def create_access_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
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
        expire = utc_now() + timedelta(minutes=settings.security.access_token_expire_minutes)

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
    except JWTError as e:
        logger.warning("Token verification failed", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


async def verify_api_token(authorization: str | None = Header(None)) -> str:
    """
    Verify API token from Authorization header.

    Args:
        authorization: Authorization header value

    Returns:
        Valid token

    Raises:
        HTTPException: If token is missing or invalid

    Example:
        @router.get("/agents")
        async def list_agents(token: str = Depends(verify_api_token)):
            ...
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

    # Constant-time comparison against each configured token (avoids a timing oracle).
    if not any(hmac.compare_digest(token, t) for t in settings.security.auth_tokens):
        logger.warning(
            "Invalid API token attempt",
            extra={"token_prefix": token[:10]},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return token


async def get_optional_token(authorization: str | None = Header(None)) -> str | None:
    """
    Get API token if provided, but don't require it.

    Args:
        authorization: Authorization header value

    Returns:
        Token if valid, None if not provided

    Example:
        For public endpoints that have different behavior when authenticated
    """
    if authorization is None:
        return None

    try:
        return await verify_api_token(authorization)
    except HTTPException:
        return None


async def verify_registration_token(
    authorization: str | None = Header(None),
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

    # Import here to avoid circular dependency
    from app.services.core.registration_key_core_service import RegistrationKeyCoreService

    # Validate against registration keys in database
    if db is not None:
        reg_key = await RegistrationKeyCoreService.verify_key_and_update_usage(db, token)
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


async def verify_agent_token(
    agent: Agent,
    authorization: str | None = Header(None),
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
    5. Check agent approval_status (pending, rejected, disabled, paused)

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
            is_valid = bcrypt.checkpw(token.encode("utf-8"), agent.api_key_hash.encode("utf-8"))
        except Exception:
            logger.error(
                "Error verifying agent key",
                extra={"agent_id": str(agent.id)},
                exc_info=True,
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

    # Check approval status
    if agent.approval_status == "pending":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Agent registration pending approval",
        )
    elif agent.approval_status == "rejected":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Agent registration was rejected",
        )
    elif agent.approval_status == "disabled":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Agent access has been disabled",
        )
    elif agent.approval_status == "paused":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Agent is paused",
        )

    return token
