"""
Settings service - web UI for settings management.

Provides web-specific functionality like value parsing and template cache updates.
"""

from collections import defaultdict
from typing import Any

from fastapi import Request
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user_model import User
from app.schemas.pagination_schema import build_pagination
from app.services.core.alert_core_service import AlertCoreService
from app.services.core.notification_core_service import NotificationCoreService
from app.services.core.registration_key_core_service import RegistrationKeyCoreService
from app.services.core.settings_core_service import SettingsCoreService
from app.services.core.timescale_core_service import TimescaleCoreService  # noqa: F401
from app.services.core.user_core_service import UserCoreService
from app.web.template_filters import update_settings_cache

logger = get_logger("luxswirl.web.services.settings")


class SettingsViewService:
    """Service for settings web UI with web-specific logic."""

    @staticmethod
    async def update_setting_with_parsing(
        db: AsyncSession,
        key: str,
        value: str,
    ) -> tuple[Any, str | None]:
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
            updated_setting = await SettingsCoreService.update_setting(db, key, parsed_value)

            # Update template filter cache if needed
            SettingsViewService._update_template_cache_if_needed(key, parsed_value)

            # Update TimescaleDB policies if database setting was changed
            await SettingsViewService._update_timescale_policies_if_needed(db, key, parsed_value)

            return updated_setting, None

        except ValueError as e:
            return None, f"Invalid value format: {str(e)}"
        except Exception as e:
            logger.error(
                "Error updating setting",
                extra={"setting_key": key},
                exc_info=True,
            )
            return None, str(e)

    @staticmethod
    def _parse_value(value: str, value_type: str | None):
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
    def _update_template_cache_if_needed(key: str, parsed_value):
        """
        Update template filter cache if display setting was changed.

        Args:
            key: Setting key
            parsed_value: New value
        """
        if key in ("general.timezone", "general.date_format", "general.time_format"):
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
        parsed_value,
    ):
        """
        Update TimescaleDB policies if database setting was changed.

        Args:
            db: Database session
            key: Setting key
            parsed_value: New value (days)
        """
        if key.startswith("database."):
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
    ) -> dict[str, list]:
        """
        Get security settings grouped by subcategory.

        Returns:
            Dict mapping subcategory names to lists of setting objects,
            sorted by subcategory name.
        """
        all_security_settings = await SettingsCoreService.get_settings_by_category(db, "security")

        security_groups = defaultdict(list)
        for setting in all_security_settings:
            subcategory = setting.subcategory or "Other"
            security_groups[subcategory].append(setting)

        return dict(sorted(security_groups.items()))

    @staticmethod
    async def reset_setting_with_cache_update(
        db: AsyncSession,
        key: str,
    ) -> Any:
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

        return updated_setting

    @staticmethod
    async def update_metrics_timeout(
        db: AsyncSession,
        timeout: str,
    ) -> tuple[Any, str | None]:
        """
        Validate and update the metrics agent timeout setting.

        Args:
            db: Database session
            timeout: Timeout value as string from form

        Returns:
            Tuple of (metrics_settings, error_message)
        """
        if not timeout:
            return None, "Timeout value required"

        try:
            timeout_int = int(timeout)
        except ValueError:
            return None, "Invalid timeout value"

        if timeout_int < 30 or timeout_int > 3600:
            return None, "Timeout must be between 30 and 3600 seconds"

        await SettingsCoreService.update_setting(db, "metrics.agent_timeout_seconds", timeout_int)

        metrics_settings = await SettingsCoreService.get_metrics_settings(db)
        return metrics_settings, None

    # ------------------------------------------------------------------
    # Page-context builders.
    # ------------------------------------------------------------------

    @staticmethod
    async def build_landing_context(
        db: AsyncSession, request: Request, current_user: User
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
        db: AsyncSession, request: Request, current_user: User
    ) -> dict[str, Any]:
        """/settings/notifications page context."""
        providers, total = await NotificationCoreService.list_providers(db, limit=1000)
        return {
            "request": request,
            "current_user": current_user,
            "providers": providers,
            "total_providers": total,
            "available_types": NotificationCoreService.get_available_provider_types(),
            "page_title": "Settings - Notifications",
        }

    @staticmethod
    async def build_alerts_context(
        db: AsyncSession,
        request: Request,
        current_user: User,
        is_enabled: bool | None,
        is_global: bool | None,
        page: int,
        per_page: int | None,
    ) -> dict[str, Any]:
        """/settings/alerts page context (paginated, filtered)."""
        if per_page is None:
            per_page = await SettingsCoreService.get_setting(db, "general.default_page_size", 50)
        offset = (page - 1) * per_page
        alerts, total = await AlertCoreService.list_alerts(
            db=db,
            skip=offset,
            limit=per_page,
            is_enabled=is_enabled,
            is_global=is_global,
        )
        filters = {"is_enabled": is_enabled, "is_global": is_global}
        pagination = build_pagination(page=page, per_page=per_page, total=total, filters=filters)
        return {
            "request": request,
            "current_user": current_user,
            "alerts": alerts,
            "filters": filters,
            "pagination": pagination,
            "page_title": "Settings - Alerts",
        }

    @staticmethod
    async def build_registration_keys_context(
        db: AsyncSession,
        request: Request,
        current_user: User,
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
            "registration_keys": keys,
            "total_keys": total,
            "metrics_settings": await SettingsCoreService.get_metrics_settings(db),
            "page_title": "Settings - API Keys",
        }

    @staticmethod
    def build_components_context(request: Request, current_user: User) -> dict[str, Any]:
        """/settings/components — purely presentational, no DB."""
        return {
            "request": request,
            "current_user": current_user,
            "page_title": "Settings - Components",
        }

    @staticmethod
    async def build_defaults_context(
        db: AsyncSession, request: Request, current_user: User
    ) -> dict[str, Any]:
        """/settings/defaults — all configurable defaults grouped by category."""
        return {
            "request": request,
            "current_user": current_user,
            "check_settings": await SettingsCoreService.get_settings_by_category(db, "check"),
            "alert_settings": await SettingsCoreService.get_settings_by_category(db, "alert"),
            "system_settings": await SettingsCoreService.get_settings_by_category(db, "system"),
            "job_settings": await SettingsCoreService.get_settings_by_category(db, "job"),
            "security_groups": await SettingsViewService.get_grouped_security_settings(db),
            "general_settings": await SettingsCoreService.get_settings_by_category(db, "general"),
            "database_settings": await SettingsCoreService.get_settings_by_category(db, "database"),
            "page_title": "Settings - Defaults",
        }

    # ------------------------------------------------------------------
    # Setting card / metrics card builders.
    # ------------------------------------------------------------------

    @staticmethod
    def build_setting_card_context(
        request: Request, current_user: User, setting: Any
    ) -> dict[str, Any]:
        """Setting-card partial context (used for update/reset responses)."""
        return {
            "request": request,
            "current_user": current_user,
            "setting": setting,
        }

    @staticmethod
    def build_metrics_card_context(
        request: Request, current_user: User, metrics_settings: Any
    ) -> dict[str, Any]:
        """Metrics-card partial context."""
        return {
            "request": request,
            "current_user": current_user,
            "metrics_settings": metrics_settings,
        }

    @staticmethod
    def build_metrics_token_context(
        request: Request, current_user: User, token: str
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
    async def generate_metrics_token(db: AsyncSession) -> str:
        """Generate new Prometheus metrics bearer token."""
        return await SettingsCoreService.generate_metrics_token(db)

    @staticmethod
    async def toggle_metrics_enabled(db: AsyncSession):
        """Toggle metrics endpoint on/off; return updated metrics_settings."""
        current_value = await SettingsCoreService.get_setting(db, "metrics.enabled", True)
        await SettingsCoreService.update_setting(db, "metrics.enabled", not current_value)
        return await SettingsCoreService.get_metrics_settings(db)

    @staticmethod
    async def toggle_metrics_auth(db: AsyncSession):
        """Toggle metrics auth on/off; return updated metrics_settings."""
        current_value = await SettingsCoreService.get_setting(db, "metrics.auth_required", False)
        await SettingsCoreService.update_setting(db, "metrics.auth_required", not current_value)
        return await SettingsCoreService.get_metrics_settings(db)
