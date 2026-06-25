"""
Auth Service - handles authentication, session management, and security.
"""

import hashlib
import secrets
from datetime import timedelta
from uuid import UUID

import bcrypt
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.common_passwords import is_common_password
from app.core.datetime_utils import utc_now
from app.crud.session_crud import SessionCRUD
from app.crud.user_crud import UserCRUD
from app.models.session_model import Session
from app.models.user_model import User
from app.services.core.settings_core_service import SettingsCoreService

logger = get_logger("luxswirl.services.auth")


class AuthCoreService:
    """Service for authentication and session management."""

    # Security constants
    SESSION_TOKEN_BYTES = 32  # 256 bits of entropy
    MAX_FAILED_ATTEMPTS = 5
    ACCOUNT_LOCK_DURATION_MINUTES = 30
    SESSION_LIFETIME_DAYS = 7
    PASSWORD_BCRYPT_ROUNDS = 12

    @staticmethod
    async def validate_password_complexity(db: AsyncSession, password: str) -> tuple[bool, str]:
        """
        Validate password against complexity rules from database settings.

        Args:
            db: Database session
            password: Plain text password to validate

        Returns:
            Tuple of (is_valid, error_message)
            - is_valid: True if password meets all requirements
            - error_message: Empty string if valid, error description if invalid

        Example:
            >>> valid, error = await AuthCoreService.validate_password_complexity(db, "Pass123!")
            >>> if not valid:
            >>>     raise ValueError(error)
        """
        # Get security settings from database
        security_settings = await SettingsCoreService.get_security_settings(db)

        # Check minimum length
        min_length = security_settings.get("min_password_length", 8)
        if len(password) < min_length:
            return False, f"Password must be at least {min_length} characters long"

        # Check uppercase requirement
        if security_settings.get("require_uppercase", True) and not any(
            c.isupper() for c in password
        ):
            return False, "Password must contain at least one uppercase letter"

        # Check lowercase requirement
        if security_settings.get("require_lowercase", True) and not any(
            c.islower() for c in password
        ):
            return False, "Password must contain at least one lowercase letter"

        # Check number requirement
        if security_settings.get("require_number", True) and not any(c.isdigit() for c in password):
            return False, "Password must contain at least one digit"

        # Check special character requirement
        if security_settings.get("require_special_char", False):
            special_chars = "!@#$%^&*()_+-=[]{}|;:,.<>?"
            if not any(c in special_chars for c in password):
                return (
                    False,
                    f"Password must contain at least one special character ({special_chars})",
                )

        # Check against common passwords list
        if security_settings.get("check_common_passwords", True) and is_common_password(password):
            return False, "Password is too common. Please choose a stronger password"

        return True, ""

    @staticmethod
    def hash_password(password: str) -> str:
        """
        Hash password using bcrypt.

        Args:
            password: Plain text password

        Returns:
            Bcrypt hash as string
        """
        salt = bcrypt.gensalt(rounds=AuthCoreService.PASSWORD_BCRYPT_ROUNDS)
        password_bytes = password.encode("utf-8")
        hashed = bcrypt.hashpw(password_bytes, salt)
        return hashed.decode("utf-8")

    @staticmethod
    def verify_password(password: str, password_hash: str) -> bool:
        """
        Verify password against bcrypt hash.

        Args:
            password: Plain text password
            password_hash: Bcrypt hash to verify against

        Returns:
            True if password matches hash
        """
        password_bytes = password.encode("utf-8")
        hash_bytes = password_hash.encode("utf-8")
        return bcrypt.checkpw(password_bytes, hash_bytes)

    @staticmethod
    def generate_session_token() -> str:
        """
        Generate cryptographically secure session token.

        Returns:
            Hex-encoded random token (64 characters)
        """
        return secrets.token_hex(AuthCoreService.SESSION_TOKEN_BYTES)

    @staticmethod
    def hash_token(token: str) -> str:
        """
        Hash token using SHA-256 for storage.

        Args:
            token: Plain token

        Returns:
            SHA-256 hash as hex string
        """
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    # Fake bcrypt hash for constant-time response (prevents username enumeration via timing)
    # This is a valid bcrypt hash of "dummy_password_that_will_never_match_12345678"
    # Generated with 12 rounds to match PASSWORD_BCRYPT_ROUNDS
    _FAKE_PASSWORD_HASH = "$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY5GyYqNGxMhxyu"

    async def authenticate_user(
        self,
        db: AsyncSession,
        username: str,
        password: str,
        client_ip: str | None = None,
        user_agent: str | None = None,
    ) -> User | None:
        """
        Authenticate user by username and password.

        Handles account locking and failed attempt tracking.

        Security: Uses constant-time response to prevent username enumeration
        via timing attacks. Always performs bcrypt verification even for
        non-existent users using a fake hash.

        Args:
            db: Database session
            username: Username
            password: Plain text password
            client_ip: Real client IP (trusted-proxy-aware) for audit logging
            user_agent: User-Agent header for audit logging

        Returns:
            User object if authentication successful, None otherwise
        """
        # Common audit-log fields for every auth event in this call.
        audit = {
            "username": username,
            "client_ip": client_ip,
            "user_agent": user_agent,
        }

        # Get security settings from database
        security_settings = await SettingsCoreService.get_security_settings(db)

        # Get user by username
        user = await UserCRUD.get_by_username(db, username)

        if not user:
            # Security: Always run bcrypt verification even for non-existent users
            # to maintain constant timing and prevent username enumeration
            self.verify_password(password, self._FAKE_PASSWORD_HASH)
            logger.warning(
                "Login attempt for non-existent user",
                extra={"event": "auth.failure.unknown_user", **audit},
            )
            return None

        # Check if account is locked
        if user.is_locked:
            # Security: Run bcrypt to maintain constant timing
            self.verify_password(password, self._FAKE_PASSWORD_HASH)
            logger.warning(
                "Login attempt for locked account",
                extra={
                    "event": "auth.failure.locked",
                    "locked_until": str(user.locked_until),
                    **audit,
                },
            )
            return None

        # Check if account is active
        if not user.is_active:
            # Security: Run bcrypt to maintain constant timing
            self.verify_password(password, self._FAKE_PASSWORD_HASH)
            logger.warning(
                "Login attempt for inactive account",
                extra={"event": "auth.failure.inactive", **audit},
            )
            return None

        # Verify password
        if not self.verify_password(password, user.password_hash):
            # Increment failed attempts
            user.failed_login_attempts += 1

            # Lock account if too many failures (using DB setting)
            max_attempts = security_settings["max_failed_attempts"]
            if user.failed_login_attempts >= max_attempts:
                lock_duration = security_settings["account_lock_duration_minutes"]
                user.locked_until = utc_now() + timedelta(minutes=lock_duration)
                logger.warning(
                    "Account locked due to too many failed attempts",
                    extra={
                        "event": "auth.account_locked",
                        "locked_until": str(user.locked_until),
                        "lock_duration_minutes": lock_duration,
                        **audit,
                    },
                )

            logger.warning(
                "Failed login attempt",
                extra={
                    "event": "auth.failure.bad_password",
                    "attempt": user.failed_login_attempts,
                    "max_attempts": max_attempts,
                    **audit,
                },
            )
            return None

        # Authentication successful - reset failed attempts
        user.failed_login_attempts = 0
        user.locked_until = None
        user.last_login_at = utc_now()

        logger.info(
            "Successful login",
            extra={"event": "auth.login.success", "user_id": str(user.id), **audit},
        )
        return user

    async def create_session(
        self,
        db: AsyncSession,
        user: User,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[Session, str]:
        """
        Create a new session for user.

        Args:
            db: Database session
            user: User object
            ip_address: IP address of client
            user_agent: User agent string

        Returns:
            Tuple of (Session object, plain session token)
        """
        # Get security settings from database
        security_settings = await SettingsCoreService.get_security_settings(db)
        session_lifetime_days = security_settings["session_lifetime_days"]

        # Generate secure token
        token = self.generate_session_token()
        token_hash = self.hash_token(token)

        # Create session with configured lifetime
        session = Session(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=utc_now() + timedelta(days=session_lifetime_days),
            ip_address=ip_address,
            user_agent=user_agent,
            last_activity_at=utc_now(),
        )

        db.add(session)
        await db.flush()
        await db.refresh(session)

        logger.info(
            "Created session for user",
            extra={
                "username": user.username,
                "user_id": str(user.id),
                "session_id": str(session.id),
                "expires_at": str(session.expires_at),
                "session_lifetime_days": session_lifetime_days,
            },
        )

        return session, token

    async def verify_session(self, db: AsyncSession, token: str) -> Session | None:
        """
        Verify session token and return session if valid.

        Args:
            db: Database session
            token: Plain session token

        Returns:
            Session object if valid, None otherwise
        """
        token_hash = self.hash_token(token)

        # Get session by token hash
        session = await SessionCRUD.get_by_token_hash(db, token_hash)

        if not session:
            return None

        # Check if session is valid
        if not session.is_valid:
            logger.debug(
                "Invalid session",
                extra={"session_id": str(session.id)},
            )
            return None

        # Update last activity
        session.last_activity_at = utc_now()

        return session

    async def logout(self, db: AsyncSession, token: str) -> bool:
        """
        Logout user by deleting session.

        Args:
            db: Database session
            token: Plain session token

        Returns:
            True if logout successful
        """
        token_hash = self.hash_token(token)

        # Get and delete session
        session = await SessionCRUD.get_by_token_hash(db, token_hash)

        if session:
            user_id = session.user_id
            await db.delete(session)
            logger.info(
                "User logged out",
                extra={
                    "event": "auth.logout",
                    "user_id": str(user_id),
                    "session_id": str(session.id),
                },
            )
            return True

        return False

    async def get_user_sessions(self, db: AsyncSession, user_id: UUID) -> list[Session]:
        """
        Get all active sessions for a user.

        Args:
            db: Database session
            user_id: User UUID

        Returns:
            List of Session objects
        """
        return await SessionCRUD.list_active_for_user(db, user_id)

    async def logout_all_sessions(self, db: AsyncSession, user_id: UUID) -> int:
        """
        Logout all sessions for a user.

        Args:
            db: Database session
            user_id: User UUID

        Returns:
            Number of sessions deleted
        """
        sessions = await self.get_user_sessions(db, user_id)
        count = len(sessions)

        for session in sessions:
            await db.delete(session)

        logger.info(
            "Logged out all sessions for user",
            extra={
                "event": "auth.logout_all",
                "user_id": str(user_id),
                "session_count": count,
            },
        )
        return count

    async def logout_session_by_id(self, db: AsyncSession, user_id: UUID, session_id: UUID) -> bool:
        """
        Logout a specific session (for user managing their sessions).

        Args:
            db: Database session
            user_id: User UUID (for authorization check)
            session_id: Session UUID to delete

        Returns:
            True if session was deleted
        """
        session = await SessionCRUD.get_for_user(db, session_id, user_id)

        if session:
            await db.delete(session)
            logger.info(
                "User logged out specific session",
                extra={
                    "event": "auth.logout_session",
                    "user_id": str(user_id),
                    "session_id": str(session_id),
                },
            )
            return True

        return False

    async def cleanup_expired_sessions(self, db: AsyncSession) -> int:
        """
        Delete all expired sessions (background task).

        Args:
            db: Database session

        Returns:
            Number of sessions deleted
        """
        sessions = await SessionCRUD.list_expired(db)

        count = len(sessions)
        for session in sessions:
            await db.delete(session)

        if count > 0:
            logger.info(
                "Cleaned up expired sessions",
                extra={"session_count": count},
            )

        return count

    async def change_password(
        self, db: AsyncSession, user: User, current_password: str, new_password: str
    ) -> bool:
        """
        Change user password.

        Args:
            db: Database session
            user: User object
            current_password: Current password (for verification)
            new_password: New password

        Returns:
            True if password changed successfully

        Raises:
            ValueError: If current password is incorrect or new password doesn't meet complexity requirements
        """
        # Verify current password
        if not self.verify_password(current_password, user.password_hash):
            raise ValueError("Current password is incorrect")

        # Validate new password complexity
        valid, error_message = await self.validate_password_complexity(db, new_password)
        if not valid:
            raise ValueError(error_message)

        # Hash new password
        user.password_hash = self.hash_password(new_password)
        user.password_changed_at = utc_now()
        user.must_change_password = False

        # Security: Invalidate all sessions to prevent session hijacking
        # If attacker had stolen a session, they can no longer use it after password change
        session_count = await self.logout_all_sessions(db, user.id)
        logger.info(
            "Invalidated sessions for user due to password change",
            extra={
                "username": user.username,
                "user_id": str(user.id),
                "session_count": session_count,
            },
        )

        logger.info(
            "Password changed",
            extra={
                "event": "auth.password_changed",
                "username": user.username,
                "user_id": str(user.id),
            },
        )

        return True

    async def reset_password(
        self, db: AsyncSession, user: User, new_password: str, force_change: bool = True
    ) -> None:
        """
        Reset user password (admin function).

        Args:
            db: Database session
            user: User object
            new_password: New password
            force_change: Whether to force user to change password on next login

        Raises:
            ValueError: If new password doesn't meet complexity requirements
        """
        # Validate new password complexity
        valid, error_message = await self.validate_password_complexity(db, new_password)
        if not valid:
            raise ValueError(error_message)

        user.password_hash = self.hash_password(new_password)
        user.password_changed_at = utc_now()
        user.must_change_password = force_change
        user.failed_login_attempts = 0
        user.locked_until = None

        logger.info(
            "Password reset",
            extra={
                "event": "auth.password_reset",
                "username": user.username,
                "user_id": str(user.id),
                "force_change": force_change,
            },
        )
