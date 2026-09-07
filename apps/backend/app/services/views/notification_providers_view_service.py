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
from fastapi.responses import HTMLResponse
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_deps import AuthedUser
from app.models.notification_provider_model import NotificationProvider
from app.schemas.notification_provider_schema import (
    NotificationProviderCreate,
    NotificationProviderUpdate,
)
from app.services.core.notification_core_service import NotificationCoreService
from app.web.hx_responses import hx_empty_with_toast

logger = get_logger("luxswirl.web.services.notification_providers")


def _parse_config_json(raw: str | None) -> dict[str, Any]:
    """Parse the form's config_json field; raise ValueError with a friendly message."""
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in configuration: {e}") from e
    if not isinstance(parsed, dict):
        raise ValueError("Configuration must be a JSON object, not a list or scalar")
    return parsed


def build_provider_config_fields(
    available_types: list[dict[str, Any]],
    selected_type: str | None,
    provider: Any | None,
) -> list[dict[str, Any]]:
    """The selected provider type's config fields, ready to render.

    The form used to find the type's schema with a selectattr filter and then
    resolve each field's current value with a dynamic `provider.config[name]`
    lookup, once per field type. Both belong here: the template should branch on
    `field.type` and render `field.value`.

    Args:
        available_types: The registry's provider metadata.
        selected_type: The type whose fields the form is showing, if any.
        provider: The provider being edited, or None when creating.

    Returns:
        One descriptor per field, with its resolved value / checked state /
        options. Empty when no type is selected or the type is unknown.
    """
    if not selected_type:
        return []

    info = next(
        (t for t in available_types if t.get("type") == selected_type),
        None,
    )
    if not info:
        return []

    config = (provider.config or {}) if provider else {}

    fields = []
    for name, schema in info.get("schema", {}).items():
        stored = config.get(name)
        has_stored = name in config
        default = schema.get("default", "")
        field_type = schema.get("type")

        if field_type == "json":
            value: Any = json.dumps(stored) if has_stored else ""
        elif field_type == "password":
            # A saved secret is re-rendered; a new one starts blank rather than
            # showing the schema default.
            value = stored if has_stored else ""
        else:
            value = stored if has_stored else (default if default is not None else "")

        fields.append(
            {
                "name": name,
                "input_name": f"config_{name}",
                "type": field_type,
                "label": schema.get("label", name),
                "required": bool(schema.get("required")),
                "placeholder": schema.get("placeholder") or "",
                "help_text": schema.get("help_text"),
                "value": value,
                "checked": bool(stored) if has_stored else bool(default),
                "options": [
                    {
                        "value": option["value"],
                        "label": option["label"],
                        "selected": (stored == option["value"])
                        if has_stored
                        else (default == option["value"]),
                    }
                    for option in schema.get("options", [])
                ],
            }
        )
    return fields


class NotificationProvidersViewService:
    """View-layer wrapper for /notification-providers endpoints."""

    @staticmethod
    def build_create_form_context(
        request: Request, current_user: AuthedUser, provider_type: str | None
    ) -> dict[str, Any]:
        """Form context for the 'new provider' modal (or type-selector)."""
        available_types = NotificationCoreService.get_available_provider_types()
        return {
            "request": request,
            "current_user": current_user,
            "provider": None,
            "selected_type": provider_type,
            "available_types": available_types,
            "config_fields": build_provider_config_fields(
                available_types, provider_type, None
            ),
            # .path, not str(): url_for returns an absolute url from the
            # request's scheme/host, which behind TLS-terminating nginx can emit
            # http:// into an https:// page.
            "form_action": request.url_for("web_create_provider").path,
        }

    @staticmethod
    async def build_edit_form_context(
        db: AsyncSession, request: Request, current_user: AuthedUser, provider_id: UUID
    ) -> dict[str, Any]:
        """Form context for editing an existing provider."""
        provider = await NotificationCoreService.get_provider_by_id(db, provider_id)
        available_types = NotificationCoreService.get_available_provider_types()
        return {
            "request": request,
            "current_user": current_user,
            "provider": provider,
            "selected_type": provider.provider_type,
            "available_types": available_types,
            "config_fields": build_provider_config_fields(
                available_types, provider.provider_type, provider
            ),
            "form_action": request.url_for(
                "web_update_provider", provider_id=provider_id
            ).path,
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
        provider = await NotificationCoreService.update_provider(
            db, provider_id, update_data
        )
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
        await NotificationCoreService.delete_provider(
            db, provider_id, hard_delete=False
        )
        logger.info(
            "Deleted notification provider",
            extra={"provider_id": str(provider_id)},
        )

    @staticmethod
    async def toggle_provider(
        db: AsyncSession, provider_id: UUID
    ) -> NotificationProvider:
        """Read current state, flip is_enabled, persist."""
        provider = await NotificationCoreService.get_provider_by_id(db, provider_id)
        update_data = NotificationProviderUpdate(is_enabled=not provider.is_enabled)
        provider = await NotificationCoreService.update_provider(
            db, provider_id, update_data
        )
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
    async def test_provider(db: AsyncSession, provider_id: UUID) -> HTMLResponse:
        """Send a test notification and report the outcome as a toast."""
        success, error_msg = await NotificationCoreService.test_provider(
            db, provider_id
        )
        if success:
            return hx_empty_with_toast("Test notification sent successfully!")
        return hx_empty_with_toast(f"Test failed: {error_msg}", kind="error")

    @staticmethod
    def build_error_context(
        request: Request, current_user: AuthedUser, error: str
    ) -> dict[str, Any]:
        """Common error-partial context."""
        return {
            "request": request,
            "current_user": current_user,
            "error": error,
        }
