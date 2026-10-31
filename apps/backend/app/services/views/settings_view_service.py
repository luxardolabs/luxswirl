"""
Settings service - web UI for settings management.

Provides web-specific functionality like value parsing and template cache updates.
"""

from collections import defaultdict
from typing import Any

from fastapi import Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationException, value_error_message
from app.schemas.alert_schema import AlertRow, AlertUrls
from app.schemas.auth_schema import AuthedUser
from app.schemas.notification_provider_schema import (
    NotificationProviderRow,
    ProviderUrls,
)
from app.schemas.pagination_schema import build_pagination
from app.schemas.registration_key_schema import (
    RegistrationKeyRow,
    RegistrationKeyUrls,
)
from app.schemas.setting_schema import (
    MetricsCardUrls,
    SettingResponse,
    SettingRow,
    SettingScalar,
    SettingUrls,
)
from app.services.core.alert_core_service import AlertCoreService
from app.services.core.notification_core_service import NotificationCoreService
from app.services.core.registration_key_core_service import RegistrationKeyCoreService
from app.services.core.settings_core_service import (
    SettingsCoreService,
    stored_value_type,
)
from app.services.core.user_core_service import UserCoreService
from app.web.templates import templates


def build_setting_urls(request: Request, key: str) -> SettingUrls:
    """Resolve one setting's update/reset hrefs by route name."""
    return SettingUrls(
        update=str(request.url_for("settings.update", key=key).path),
        reset=str(request.url_for("settings.reset", key=key).path),
    )


def build_metrics_card_urls(request: Request) -> MetricsCardUrls:
    """Resolve the metrics card's control hrefs by route name."""

    def u(name: str) -> str:
        return str(request.app.url_path_for(name))

    return MetricsCardUrls(
        toggle_enabled=u("metrics_settings.toggle_enabled"),
        toggle_auth=u("metrics_settings.toggle_auth"),
        update_timeout=u("metrics_settings.update_timeout"),
        generate_token=u("metrics_settings.generate_token"),
    )


def setting_row(request: Request, setting: SettingResponse) -> SettingRow:
    """`setting` with its card's update/reset links."""
    return SettingRow(**dict(setting), urls=build_setting_urls(request, setting.key))


class SettingsViewService:
    """Service for settings web UI with web-specific logic."""

    @staticmethod
    async def update_setting_with_parsing(
        db: AsyncSession,
        key: str,
        value: str,
    ) -> tuple[SettingResponse | None, str | None]:
        """
        Update a setting with automatic value parsing based on type.

        Args:
            db: Database session
            key: Setting key
            value: String value from form

        Returns:
            Tuple of (updated_setting, error_message)
        """
        try:
            # Get setting to determine type
            setting = await SettingsCoreService.get_setting_response(db, key)
            if not setting:
                return None, f"Setting not found: {key}"

            # Parse value based on type
            parsed_value = SettingsViewService._parse_value(
                value, stored_value_type(setting.value)
            )
        except ValueError as e:
            return None, f"Invalid value format: {value_error_message(e)}"
        try:
            # Set it; the core applies what the setting governs.
            updated_setting = await SettingsCoreService.set_setting(
                db, key, parsed_value
            )
        except ValidationException as e:
            return None, e.message
        return updated_setting, None

    @staticmethod
    def _parse_value(value: str, value_type: str | None) -> SettingScalar:
        """
        Parse string value to appropriate type.

        Args:
            value: String value from form
            value_type: Type to parse to (int/float/bool/str)

        Returns:
            Parsed value
        """
        if value_type == "int":
            return int(value)
        elif value_type == "float":
            return float(value)
        elif value_type == "bool":
            return value.lower() in ("true", "1", "yes", "on")
        else:
            return value

    @staticmethod
    async def get_grouped_security_settings(
        db: AsyncSession,
    ) -> dict[str, list[SettingResponse]]:
        """
        Get security settings grouped by subcategory.

        Returns:
            Dict mapping subcategory names to lists of setting objects,
            sorted by subcategory name.
        """
        all_security_settings = await SettingsCoreService.get_settings_by_category(
            db, "security"
        )

        security_groups: defaultdict[str, list[SettingResponse]] = defaultdict(list)
        for setting in all_security_settings:
            subcategory = setting.subcategory or "Other"
            security_groups[subcategory].append(setting)

        return dict(sorted(security_groups.items()))

    @staticmethod
    async def reset_setting_card(
        db: AsyncSession,
        request: Request,
        current_user: AuthedUser,
        key: str,
    ) -> tuple[dict[str, Any], str]:
        """
        Reset a setting to its default (the core applies what it governs).

        Args:
            db: Database session
            request: The request, for the card's links
            current_user: Who is signed in
            key: Setting key

        Returns:
            The refreshed card's context, and the toast to show.
        """
        setting = await SettingsCoreService.reset_setting(db, key)
        return (
            SettingsViewService.build_setting_card_context(
                request, current_user, setting
            ),
            f"Setting '{setting.display_name}' reset to default",
        )

    @staticmethod
    async def update_metrics_timeout(
        db: AsyncSession,
        timeout: str,
    ) -> dict[str, Any]:
        """
        Validate and update the metrics agent timeout setting.

        Args:
            db: Database session
            timeout: Timeout value as string from form

        Returns:
            The metrics settings after the update.

        Raises:
            ValueError: If the value is missing, not a number, or outside
                30-3600 seconds; the message is the form error to show.
        """
        if not timeout:
            raise ValueError("Timeout value required")

        try:
            timeout_int = int(timeout)
        except ValueError:
            raise ValueError("Invalid timeout value") from None

        if timeout_int < 30 or timeout_int > 3600:
            raise ValueError("Timeout must be between 30 and 3600 seconds")

        await SettingsCoreService.update_setting(
            db, "metrics.agent_timeout_seconds", timeout_int
        )
        return await SettingsCoreService.get_metrics_settings(db)

    # ------------------------------------------------------------------
    # Page-context builders.
    # ------------------------------------------------------------------

    @staticmethod
    async def build_landing_context(
        db: AsyncSession, request: Request, current_user: AuthedUser
    ) -> dict[str, Any]:
        """/settings landing page — totals across providers/alerts/keys/users."""
        _, providers_count = await NotificationCoreService.list_providers(db, limit=1)
        alerts_count = await AlertCoreService.count_alerts(db)
        _, keys_count = await RegistrationKeyCoreService.list_keys(db, skip=0, limit=1)
        users_count = await UserCoreService.get_active_user_count(db)
        return {
            "request": request,
            "current_user": current_user,
            "stats": {
                "providers": providers_count,
                "alerts": alerts_count,
                "keys": keys_count,
                "users": users_count,
            },
            "page_title": "Settings",
        }

    @staticmethod
    async def build_notifications_context(
        db: AsyncSession, request: Request, current_user: AuthedUser
    ) -> dict[str, Any]:
        """/settings/notifications page context."""
        providers, total = await NotificationCoreService.list_providers(db, limit=1000)
        return {
            "request": request,
            "current_user": current_user,
            "providers": [
                NotificationProviderRow(
                    **p.model_dump(),
                    urls=ProviderUrls(
                        toggle=request.url_for(
                            "notification_providers.toggle", provider_id=p.id
                        ).path,
                        test=request.url_for(
                            "notification_providers.test", provider_id=p.id
                        ).path,
                        edit_form=request.url_for(
                            "notification_providers.edit", provider_id=p.id
                        ).path,
                        delete=request.url_for(
                            "notification_providers.delete", provider_id=p.id
                        ).path,
                    ),
                )
                for p in providers
            ],
            "total_providers": total,
            "available_types": NotificationCoreService.get_available_provider_types(),
            "provider_create_form_url": request.url_for(
                "notification_providers.new"
            ).path,
            "page_title": "Settings - Notifications",
        }

    @staticmethod
    async def build_alerts_context(
        db: AsyncSession,
        request: Request,
        current_user: AuthedUser,
        is_enabled: bool | None,
        is_global: bool | None,
        page: int,
        per_page: int | None,
    ) -> dict[str, Any]:
        """/settings/alerts page context (paginated, filtered)."""
        if per_page is None:
            per_page = await SettingsCoreService.get_setting(
                db, "general.default_page_size", 50
            )
        alerts, total = await AlertCoreService.list_alert_responses(
            db,
            skip=(page - 1) * per_page,
            limit=per_page,
            is_enabled=is_enabled,
            is_global=is_global,
        )
        filters = {"is_enabled": is_enabled, "is_global": is_global}
        pagination = build_pagination(
            page=page, per_page=per_page, total=total, filters=filters
        )
        return {
            "alerts_url": request.url_for("settings.alerts").path,
            "alert_create_form_url": request.url_for("alerts.new").path,
            "request": request,
            "current_user": current_user,
            "alerts": [
                AlertRow(
                    **a.model_dump(),
                    urls=AlertUrls(
                        edit_form=request.url_for("alerts.edit", alert_id=a.id).path,
                        delete=request.url_for("alerts.delete", alert_id=a.id).path,
                        toggle=request.url_for("alerts.toggle", alert_id=a.id).path,
                    ),
                )
                for a in alerts
            ],
            "total": total,
            "per_page": per_page,
            "filters": filters,
            "pagination": pagination,
            "page_title": "Settings - Alerts",
        }

    @staticmethod
    async def build_registration_keys_context(
        db: AsyncSession,
        request: Request,
        current_user: AuthedUser,
        is_enabled: bool | None,
    ) -> dict[str, Any]:
        """/settings/registration-keys page context (also includes metrics settings)."""
        keys, total = await RegistrationKeyCoreService.list_keys(
            db=db,
            skip=0,
            limit=1000,
            include_revoked=not is_enabled if is_enabled is not None else False,
        )
        return {
            "request": request,
            "current_user": current_user,
            # Each key carries its finished hrefs, resolved by route name
            # (fw.url_assembly_in_view).
            "registration_keys": [
                RegistrationKeyRow(
                    **dict(k),
                    urls=RegistrationKeyUrls(
                        revoke=request.url_for(
                            "registration_keys.revoke", key_id=k.id
                        ).path,
                        delete=request.url_for(
                            "registration_keys.delete", key_id=k.id
                        ).path,
                    ),
                )
                for k in keys
            ],
            "total_keys": total,
            "metrics_settings": await SettingsCoreService.get_metrics_settings(db),
            "metrics_urls": build_metrics_card_urls(request),
            "create_key_form_url": str(
                request.app.url_path_for("registration_keys.new")
            ),
            "page_title": "Settings - API Keys",
        }

    @staticmethod
    def build_components_context(
        request: Request, current_user: AuthedUser
    ) -> dict[str, Any]:
        """/settings/components — purely presentational, no DB.

        The samples feed the data-bound swatches: each has the shape the real
        page hands its macro, so the catalogue renders the component itself.
        """
        return {
            "request": request,
            "current_user": current_user,
            "page_title": "Settings - Components",
            "sample_pagination": build_pagination(page=2, per_page=10, total=42),
            # The sample filter/pagination swatches link back to this page.
            "list_url": str(request.app.url_path_for("settings.components")),
            "sample_status_row": {
                "check_id": "00000000-0000-0000-0000-000000000000",
                "check_name": "api-health",
                "check_type": "http",
                "check_tags": ["prod", "api"],
                "target": "https://api.example.com/health",
                "enabled": True,
                "detail_url": "#",
                "status_class": "success",
                "latency_display": "142ms",
                "uptime_display": "99.9%",
                "minute_bars": [
                    {
                        "success": minute != 6,
                        "minutes_ago": 15 - minute,
                        "count": 1,
                        "avg_latency": 140,
                    }
                    for minute in range(15)
                ],
            },
            "sample_chart_empty": {"has_data": False},
            "sample_sparkline": [12.0, 18.5, 15.2, 22.8, 19.1, 25.4, 21.0],
            "sample_job_param": {
                "type": "integer",
                "description": "TCP port to probe",
                "minimum": 1,
                "maximum": 65535,
                "default": 443,
            },
        }

    @staticmethod
    async def build_defaults_context(
        db: AsyncSession, request: Request, current_user: AuthedUser
    ) -> dict[str, Any]:
        """/settings/defaults — all configurable defaults grouped by category."""
        groups = {
            "check": await SettingsCoreService.get_settings_by_category(db, "check"),
            "alert": await SettingsCoreService.get_settings_by_category(db, "alert"),
            "system": await SettingsCoreService.get_settings_by_category(db, "system"),
            "job": await SettingsCoreService.get_settings_by_category(db, "job"),
            "general": await SettingsCoreService.get_settings_by_category(
                db, "general"
            ),
            "database": await SettingsCoreService.get_settings_by_category(
                db, "database"
            ),
        }
        rows = {
            category: [setting_row(request, s) for s in settings]
            for category, settings in groups.items()
        }
        security_groups = {
            subcategory: [setting_row(request, s) for s in settings]
            for subcategory, settings in (
                await SettingsViewService.get_grouped_security_settings(db)
            ).items()
        }
        return {
            "request": request,
            "current_user": current_user,
            "check_settings": rows["check"],
            "alert_settings": rows["alert"],
            "system_settings": rows["system"],
            "job_settings": rows["job"],
            "security_groups": security_groups,
            # The tab badge shows how many security settings there are across
            # the subcategory groups.
            "security_count": sum(len(rows) for rows in security_groups.values()),
            "general_settings": rows["general"],
            "database_settings": rows["database"],
            "page_title": "Settings - Defaults",
        }

    # ------------------------------------------------------------------
    # Setting card / metrics card builders.
    # ------------------------------------------------------------------

    @staticmethod
    def build_setting_card_context(
        request: Request, current_user: AuthedUser, setting: SettingResponse
    ) -> dict[str, Any]:
        """Setting-card partial context (used for update/reset responses)."""
        return {
            "request": request,
            "current_user": current_user,
            "setting": setting_row(request, setting),
        }

    @staticmethod
    def build_metrics_card_context(
        request: Request, current_user: AuthedUser, metrics_settings: dict[str, Any]
    ) -> dict[str, Any]:
        """Metrics-card partial context."""
        return {
            "request": request,
            "current_user": current_user,
            "metrics_settings": metrics_settings,
            "metrics_urls": build_metrics_card_urls(request),
        }

    @staticmethod
    def build_metrics_token_context(
        request: Request, current_user: AuthedUser, token: str
    ) -> dict[str, Any]:
        """Metrics-token-display partial context."""
        return {
            "request": request,
            "current_user": current_user,
            "token": token,
            "generated": True,
        }

    # ------------------------------------------------------------------
    # Mutation orchestrators (no router-side core calls).
    # ------------------------------------------------------------------

    @staticmethod
    async def render_new_metrics_token(
        db: AsyncSession, request: Request, current_user: AuthedUser
    ) -> Response:
        """Mint a new Prometheus bearer token and render the show-it-once partial."""
        token = await SettingsCoreService.generate_metrics_token(db)
        return templates.TemplateResponse(
            request,
            "partials/settings/metrics_token.html",
            SettingsViewService.build_metrics_token_context(
                request, current_user, token
            ),
        )

    @staticmethod
    async def toggle_metrics_enabled(db: AsyncSession) -> dict[str, Any]:
        """Toggle metrics endpoint on/off; return updated metrics_settings."""
        current_value = await SettingsCoreService.get_setting(
            db, "metrics.enabled", True
        )
        await SettingsCoreService.update_setting(
            db, "metrics.enabled", not current_value
        )
        return await SettingsCoreService.get_metrics_settings(db)

    @staticmethod
    async def toggle_metrics_auth(db: AsyncSession) -> dict[str, Any]:
        """Toggle metrics auth on/off; return updated metrics_settings."""
        current_value = await SettingsCoreService.get_setting(
            db, "metrics.auth_required", False
        )
        await SettingsCoreService.update_setting(
            db, "metrics.auth_required", not current_value
        )
        return await SettingsCoreService.get_metrics_settings(db)
