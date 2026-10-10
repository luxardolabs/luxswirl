"""
Settings service - web UI for settings management.

Provides web-specific functionality like value parsing and template cache updates.
"""

from collections import defaultdict
from typing import Any

from fastapi import Request, Response
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationException
from app.schemas.alert_schema import AlertRow, AlertUrls
from app.schemas.auth_schema import AuthedUser
from app.schemas.notification_provider_schema import (
    NotificationProviderRow,
    ProviderUrls,
)
from app.schemas.pagination_schema import build_pagination
from app.schemas.registration_key_schema import (
    RegistrationKeyResponse,
    RegistrationKeyRow,
    RegistrationKeyUrls,
)
from app.schemas.setting_schema import (
    SettingResponse,
    SettingRow,
    SettingScalar,
    SettingUrls,
)
from app.services.core.alert_core_service import AlertCoreService
from app.services.core.notification_core_service import NotificationCoreService
from app.services.core.registration_key_core_service import RegistrationKeyCoreService
from app.services.core.settings_core_service import SettingsCoreService
from app.services.core.timescale_core_service import TimescaleCoreService
from app.services.core.user_core_service import UserCoreService
from app.web.template_filters import update_settings_cache
from app.web.templates import templates

logger = get_logger("luxswirl.web.services.settings")


def build_setting_urls(request: Request, key: str) -> SettingUrls:
    """Resolve one setting's update/reset hrefs by route name."""
    return SettingUrls(
        update=str(request.url_for("update_setting", key=key).path),
        reset=str(request.url_for("reset_setting", key=key).path),
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
            setting = await SettingsCoreService.get_setting_object(db, key)
            if not setting:
                return None, f"Setting not found: {key}"

            # Parse value based on type
            value_type = setting.value.get("type")
            parsed_value = SettingsViewService._parse_value(value, value_type)

            # Update setting
            updated_setting = await SettingsCoreService.update_setting(
                db, key, parsed_value
            )

            # Update template filter cache if needed
            SettingsViewService._update_template_cache_if_needed(key, parsed_value)

            # Update TimescaleDB policies if database setting was changed
            await SettingsViewService._update_timescale_policies_if_needed(
                db, key, parsed_value
            )

            # Convert at the view seam: the template gets a schema, not an ORM row.
            return SettingResponse.model_validate(updated_setting), None

        except ValueError as e:
            return None, f"Invalid value format: {str(e)}"

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
    def _update_template_cache_if_needed(key: str, parsed_value: SettingScalar) -> None:
        """
        Update template filter cache if display setting was changed.

        Args:
            key: Setting key
            parsed_value: New value

        Raises:
            ValidationException: A display setting's value is not a string.
        """
        if key in ("general.timezone", "general.date_format", "general.time_format"):
            if not isinstance(parsed_value, str):
                raise ValidationException(
                    f"{key} must be a string", details={"setting_key": key}
                )
            if key == "general.timezone":
                update_settings_cache(timezone=parsed_value)
            elif key == "general.date_format":
                update_settings_cache(date_format=parsed_value)
            elif key == "general.time_format":
                update_settings_cache(time_format=parsed_value)
            logger.info(
                "Updated template filter cache",
                extra={"setting_key": key},
            )

    @staticmethod
    async def _update_timescale_policies_if_needed(
        db: AsyncSession,
        key: str,
        parsed_value: SettingScalar,
    ) -> None:
        """
        Update TimescaleDB policies if database setting was changed.

        Args:
            db: Database session
            key: Setting key
            parsed_value: New value (days)

        Raises:
            ValidationException: A database setting's value is not a whole number.
        """
        if key.startswith("database."):
            if isinstance(parsed_value, bool) or not isinstance(parsed_value, int):
                raise ValidationException(
                    f"{key} must be a whole number of days",
                    details={"setting_key": key},
                )
            if key == "database.retention_days":
                await TimescaleCoreService.update_retention_policy(
                    db, "check_results", parsed_value
                )
                logger.info(
                    "Updated check_results retention policy",
                    extra={"retention_days": parsed_value},
                )
            elif key == "database.compress_after_days":
                await TimescaleCoreService.update_compression_policy(
                    db, "check_results", parsed_value
                )
                logger.info(
                    "Updated check_results compression policy",
                    extra={"compression_days": parsed_value},
                )
            elif key == "database.hourly_aggregate_retention_days":
                await TimescaleCoreService.update_retention_policy(
                    db, "check_results_hourly", parsed_value
                )
                logger.info(
                    "Updated check_results_hourly retention policy",
                    extra={"retention_days": parsed_value},
                )
            elif key == "database.daily_aggregate_retention_days":
                await TimescaleCoreService.update_retention_policy(
                    db, "check_results_daily", parsed_value
                )
                logger.info(
                    "Updated check_results_daily retention policy",
                    extra={"retention_days": parsed_value},
                )

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
    async def reset_setting_with_cache_update(
        db: AsyncSession,
        key: str,
    ) -> SettingResponse:
        """
        Reset a setting to its default value and update template cache if needed.

        Args:
            db: Database session
            key: Setting key

        Returns:
            Updated setting object
        """
        updated_setting = await SettingsCoreService.reset_setting(db, key)

        # Update template filter cache if display setting was reset
        if key in ("general.timezone", "general.date_format", "general.time_format"):
            reset_value = updated_setting.typed_value
            SettingsViewService._update_template_cache_if_needed(key, reset_value)
            logger.info(
                "Reset template filter cache to default",
                extra={"setting_key": key, "reset_value": reset_value},
            )

        return SettingResponse.model_validate(updated_setting)

    @staticmethod
    async def update_metrics_timeout(
        db: AsyncSession,
        timeout: str,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """
        Validate and update the metrics agent timeout setting.

        Args:
            db: Database session
            timeout: Timeout value as string from form

        Returns:
            Tuple of (the metrics settings dict, error message). The settings
            side is None whenever there is an error — a genuine multi-key
            settings mapping, not an entity crossing the seam.
        """
        if not timeout:
            return None, "Timeout value required"

        try:
            timeout_int = int(timeout)
        except ValueError:
            return None, "Invalid timeout value"

        if timeout_int < 30 or timeout_int > 3600:
            return None, "Timeout must be between 30 and 3600 seconds"

        await SettingsCoreService.update_setting(
            db, "metrics.agent_timeout_seconds", timeout_int
        )

        metrics_settings = await SettingsCoreService.get_metrics_settings(db)
        return metrics_settings, None

    # ------------------------------------------------------------------
    # Page-context builders.
    # ------------------------------------------------------------------

    @staticmethod
    async def build_landing_context(
        db: AsyncSession, request: Request, current_user: AuthedUser
    ) -> dict[str, Any]:
        """/settings landing page — totals across providers/alerts/keys/users."""
        _, providers_count = await NotificationCoreService.list_providers(db, limit=1)
        _, alerts_count = await AlertCoreService.list_alerts(db, skip=0, limit=1)
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
                            "toggle_provider", provider_id=p.id
                        ).path,
                        test=request.url_for(
                            "web_test_provider", provider_id=p.id
                        ).path,
                        edit_form=request.url_for(
                            "edit_provider_form", provider_id=p.id
                        ).path,
                        delete=request.url_for(
                            "web_delete_provider", provider_id=p.id
                        ).path,
                    ),
                )
                for p in providers
            ],
            "total_providers": total,
            "available_types": NotificationCoreService.get_available_provider_types(),
            "provider_create_form_url": request.url_for("create_provider_form").path,
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
            "alerts_url": request.url_for("settings_alerts").path,
            "alert_create_form_url": request.url_for("create_alert_form").path,
            "request": request,
            "current_user": current_user,
            "alerts": [
                AlertRow(
                    **a.model_dump(),
                    urls=AlertUrls(
                        edit_form=request.url_for(
                            "edit_alert_form", alert_id=a.id
                        ).path,
                        delete=request.url_for("web_delete_alert", alert_id=a.id).path,
                        toggle=request.url_for("toggle_alert", alert_id=a.id).path,
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
                    **dict(RegistrationKeyResponse.model_validate(k)),
                    urls=RegistrationKeyUrls(
                        revoke=request.url_for("revoke_key", key_id=k.id).path,
                        delete=request.url_for("delete_key", key_id=k.id).path,
                    ),
                )
                for k in keys
            ],
            "total_keys": total,
            "metrics_settings": await SettingsCoreService.get_metrics_settings(db),
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
