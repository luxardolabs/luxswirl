"""
Notification providers view service — context building for the
notification-provider admin UI.

Encapsulates: form context assembly, JSON config parsing, toggle
fetch-then-update, and the common error-context helper used by all
endpoints.
"""

import json
from typing import Any
from uuid import UUID

from fastapi import Request
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification_provider_model import NotificationProvider
from app.models.user_model import User
from app.schemas.notification_provider_schema import (
    NotificationProviderCreate,
    NotificationProviderUpdate,
)
from app.services.core.notification_core_service import NotificationCoreService

logger = get_logger("luxswirl.web.services.notification_providers")


def _parse_config_json(raw: Any) -> dict[str, Any]:
    """Parse the form's config_json field; raise ValueError with a friendly message."""
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in configuration: {e}") from e
    if not isinstance(parsed, dict):
        raise ValueError("Configuration must be a JSON object, not a list or scalar")
    return parsed


class NotificationProvidersViewService:
    """View-layer wrapper for /notification-providers endpoints."""

    @staticmethod
    def build_create_form_context(
        request: Request, current_user: User, provider_type: str | None
    ) -> dict[str, Any]:
        """Form context for the 'new provider' modal (or type-selector)."""
        return {
            "request": request,
            "current_user": current_user,
            "provider": None,
            "selected_type": provider_type,
            "available_types": NotificationCoreService.get_available_provider_types(),
        }

    @staticmethod
    async def build_edit_form_context(
        db: AsyncSession, request: Request, current_user: User, provider_id: UUID
    ) -> dict[str, Any]:
        """Form context for editing an existing provider."""
        provider = await NotificationCoreService.get_provider_by_id(db, provider_id)
        return {
            "request": request,
            "current_user": current_user,
            "provider": provider,
            "selected_type": provider.provider_type,
            "available_types": NotificationCoreService.get_available_provider_types(),
        }

    @staticmethod
    async def create_provider(db: AsyncSession, form: dict) -> NotificationProvider:
        """Parse a create form, build the schema, delegate to core."""
        provider_data = NotificationProviderCreate(
            provider_type=form.get("provider_type", ""),
            friendly_name=form.get("friendly_name", ""),
            config=_parse_config_json(form.get("config_json", "")),
            is_enabled=form.get("is_enabled") == "true",
            is_default_enabled=form.get("is_default_enabled") == "true",
        )
        provider = await NotificationCoreService.create_provider(db, provider_data)
        logger.info(
            "Created notification provider",
            extra={
                "provider_friendly_name": provider.friendly_name,
                "provider_id": str(provider.id),
            },
        )
        return provider

    @staticmethod
    async def update_provider(
        db: AsyncSession, provider_id: UUID, form: dict
    ) -> NotificationProvider:
        """Parse an update form, build the schema, delegate to core."""
        update_data = NotificationProviderUpdate(
            friendly_name=form.get("friendly_name", ""),
            config=_parse_config_json(form.get("config_json", "")),
            is_enabled=form.get("is_enabled") == "true",
            is_default_enabled=form.get("is_default_enabled") == "true",
        )
        provider = await NotificationCoreService.update_provider(db, provider_id, update_data)
        logger.info(
            "Updated notification provider",
            extra={
                "provider_friendly_name": provider.friendly_name,
                "provider_id": str(provider.id),
            },
        )
        return provider

    @staticmethod
    async def delete_provider(db: AsyncSession, provider_id: UUID) -> None:
        """Soft-delete a provider — pure delegation."""
        await NotificationCoreService.delete_provider(db, provider_id, hard_delete=False)
        logger.info(
            "Deleted notification provider",
            extra={"provider_id": str(provider_id)},
        )

    @staticmethod
    async def toggle_provider(db: AsyncSession, provider_id: UUID) -> NotificationProvider:
        """Read current state, flip is_enabled, persist."""
        provider = await NotificationCoreService.get_provider_by_id(db, provider_id)
        update_data = NotificationProviderUpdate(is_enabled=not provider.is_enabled)
        provider = await NotificationCoreService.update_provider(db, provider_id, update_data)
        logger.info(
            "Toggled provider",
            extra={
                "provider_friendly_name": provider.friendly_name,
                "provider_id": str(provider.id),
                "is_enabled": provider.is_enabled,
            },
        )
        return provider

    @staticmethod
    async def test_provider(db: AsyncSession, provider_id: UUID) -> tuple[bool, str | None]:
        """Send a test notification — pure delegation; (success, error_msg)."""
        return await NotificationCoreService.test_provider(db, provider_id)

    @staticmethod
    def build_error_context(request: Request, current_user: User, error: str) -> dict[str, Any]:
        """Common error-partial context."""
        return {
            "request": request,
            "current_user": current_user,
            "error": error,
        }
