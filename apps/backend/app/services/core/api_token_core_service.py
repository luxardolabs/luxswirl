"""
API token core service - users' bearer tokens for the admin API.

A token acts as its user. It authenticates only while it is unrevoked and
unexpired AND its user is active and owes no password change, and it carries
the user's role, so the admin API grants exactly what the web UI grants that
user. A login lockout does not stop it: the lockout guards the password, and
anyone who knows a username can trigger one. A password change or reset
revokes the user's tokens, as it ends their sessions.
"""

import hashlib
import secrets
from datetime import timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.datetime_utils import utc_now
from app.core.exceptions import NotFoundException
from app.crud.api_token_crud import ApiTokenCRUD
from app.crud.user_crud import UserCRUD
from app.models.api_token_model import ApiToken
from app.models.enum_model import UserRole
from app.schemas.api_token_schema import (
    ApiPrincipal,
    ApiTokenCreate,
    ApiTokenResponse,
    ApiWhoAmIResponse,
)
from app.schemas.auth_schema import CurrentUserDTO
from app.shared.logger import get_logger

logger = get_logger("luxswirl.services.api_tokens")

TOKEN_PREFIX = "lsw_"
TOKEN_BYTES = 32
# What the token list shows of each token: the prefix and 8 more characters.
DISPLAY_LENGTH = 12


def _hash(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


class ApiTokenCoreService:
    """Create, list, revoke and authenticate API tokens."""

    @staticmethod
    async def create_token(
        db: AsyncSession, user_id: UUID, data: ApiTokenCreate
    ) -> tuple[ApiTokenResponse, str]:
        """A new token for `user_id`, and its plaintext, which is never stored."""
        plaintext = TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)
        token = ApiToken(
            user_id=user_id,
            name=data.name,
            token_hash=_hash(plaintext),
            token_prefix=plaintext[:DISPLAY_LENGTH],
            expires_at=(
                utc_now() + timedelta(days=data.expires_in_days)
                if data.expires_in_days
                else None
            ),
        )
        token = await ApiTokenCRUD.add_token(db, token)
        logger.info(
            "SECURITY AUDIT: API token created",
            extra={
                "event": "api_token_created",
                "user_id": str(user_id),
                "token_id": str(token.id),
                "token_name": token.name,
                "expires_at": str(token.expires_at),
            },
        )
        return ApiTokenResponse.model_validate(token), plaintext

    @staticmethod
    async def list_tokens(db: AsyncSession, user_id: UUID) -> list[ApiTokenResponse]:
        """`user_id`'s unrevoked tokens, newest first."""
        tokens = await ApiTokenCRUD.list_unrevoked_for_user(db, user_id)
        return [ApiTokenResponse.model_validate(t) for t in tokens]

    @staticmethod
    async def revoke_token(
        db: AsyncSession, token_id: UUID, actor_id: UUID, actor_role: UserRole
    ) -> None:
        """Revoke a token: its owner's, or any token for an admin.

        Raises:
            NotFoundException: no such token, or it is another user's and the
                actor is not an admin (the same answer, so a token's existence
                is not disclosed).
        """
        token = await ApiTokenCRUD.get_by_id(db, token_id)
        if token is None or (
            token.user_id != actor_id and actor_role != UserRole.ADMIN
        ):
            raise NotFoundException(
                f"API token not found: {token_id}", {"token_id": str(token_id)}
            )
        if token.revoked_at is not None:
            return
        await ApiTokenCRUD.mark_revoked(db, token, utc_now())
        logger.info(
            "SECURITY AUDIT: API token revoked",
            extra={
                "event": "api_token_revoked",
                "token_id": str(token.id),
                "user_id": str(token.user_id),
                "revoked_by": str(actor_id),
            },
        )

    @staticmethod
    async def revoke_all_for_user(db: AsyncSession, user_id: UUID, reason: str) -> int:
        """Revoke every token of `user_id` (a password change or reset)."""
        count = await ApiTokenCRUD.revoke_all_for_user(db, user_id, utc_now())
        if count:
            logger.info(
                "SECURITY AUDIT: API tokens revoked",
                extra={
                    "event": "api_tokens_revoked",
                    "user_id": str(user_id),
                    "token_count": count,
                    "reason": reason,
                },
            )
        return count

    @staticmethod
    async def authenticate(db: AsyncSession, plaintext: str) -> ApiPrincipal | None:
        """Who `plaintext` acts as, or None when it authenticates nobody."""
        if not plaintext.startswith(TOKEN_PREFIX):
            return None
        token = await ApiTokenCRUD.get_by_hash(db, _hash(plaintext))
        now = utc_now()
        if (
            token is None
            or token.revoked_at is not None
            or (token.expires_at is not None and token.expires_at <= now)
        ):
            return None
        user = await UserCRUD.get_by_id(db, token.user_id)
        if user is None or not user.is_active or user.must_change_password:
            return None
        await ApiTokenCRUD.mark_used(db, token, now)
        return ApiPrincipal(
            user=CurrentUserDTO.model_validate(user),
            token_id=token.id,
            token_name=token.name,
            token_expires_at=token.expires_at,
        )

    @staticmethod
    def who_am_i(principal: ApiPrincipal) -> ApiWhoAmIResponse:
        """The user and token an admin-API request acts as."""
        return ApiWhoAmIResponse(
            user_id=principal.user.id,
            username=principal.user.username,
            role=principal.user.role,
            token_id=principal.token_id,
            token_name=principal.token_name,
            token_expires_at=principal.token_expires_at,
        )
