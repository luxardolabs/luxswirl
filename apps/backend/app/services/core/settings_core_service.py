"""
Settings service - business logic for configurable system defaults.
"""

import secrets
from typing import Any, ClassVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.default_settings import DEFAULT_SETTINGS
from app.core.exceptions import NotFoundException
from app.crud.setting_crud import SettingCRUD
from app.models.setting_model import Setting

logger = get_logger("luxswirl.services.settings")


class SettingsCoreService:
    """Service for settings operations."""

    _cache: ClassVar[dict[str, Any]] = {}

    @staticmethod
    async def get_setting(
        db: AsyncSession,
        key: str,
        default: Any = None,
    ) -> Any:
        """
        Get a setting value by key.

        Falls back to default if not found.

        Args:
            db: Database session
            key: Setting key (e.g., "check.default_interval")
            default: Default value to return if setting not found

        Returns:
            The typed value of the setting, or default if not found
        """
        setting = await SettingCRUD.get_by_key(db, key)

        if not setting:
            logger.debug(
                "Setting not found, using default",
                extra={"setting_key": key, "default": default},
            )
            return default

        # Return typed value
        return setting.typed_value

    @staticmethod
    async def get_setting_object(
        db: AsyncSession,
        key: str,
    ) -> Setting | None:
        """
        Get a setting object by key (not just the value).

        Args:
            db: Database session
            key: Setting key (e.g., "check.default_interval")

        Returns:
            The Setting object, or None if not found
        """
        return await SettingCRUD.get_by_key(db, key)

    @staticmethod
    async def get_settings_by_category(
        db: AsyncSession,
        category: str,
    ) -> list[Setting]:
        """
        Get all settings for a specific category.

        Args:
            db: Database session
            category: Category name (check, alert, system, job)

        Returns:
            List of Setting objects
        """
        return list(await SettingCRUD.list_by_category(db, category))

    @staticmethod
    async def get_all_settings(db: AsyncSession) -> list[Setting]:
        """
        Get all settings.

        Args:
            db: Database session

        Returns:
            List of all Setting objects
        """
        return list(await SettingCRUD.list_all(db))

    @staticmethod
    async def update_setting(
        db: AsyncSession,
        key: str,
        new_value: Any,
    ) -> Setting:
        """
        Update a setting value.

        Args:
            db: Database session
            key: Setting key
            new_value: New value to set

        Returns:
            Updated Setting object

        Raises:
            NotFoundException: If setting not found
        """
        setting = await SettingCRUD.get_by_key(db, key)

        if not setting:
            raise NotFoundException(f"Setting with key '{key}' not found")

        # Validate if validation rules exist
        if setting.validation:
            SettingsCoreService._validate_value(new_value, setting.validation)

        # Update value
        setting.set_typed_value(new_value)
        await db.flush()
        await db.refresh(setting)

        logger.info(
            "Updated setting",
            extra={"setting_key": key, "new_value": new_value},
        )
        return setting

    @staticmethod
    async def reset_setting(
        db: AsyncSession,
        key: str,
    ) -> Setting:
        """
        Reset a setting to its default value.

        Args:
            db: Database session
            key: Setting key

        Returns:
            Updated Setting object

        Raises:
            NotFoundException: If setting not found
        """
        setting = await SettingCRUD.get_by_key(db, key)

        if not setting:
            raise NotFoundException(f"Setting with key '{key}' not found")

        # Reset to default
        if isinstance(setting.default_value, dict):
            default_value = setting.default_value.get("value")
        else:
            default_value = setting.default_value
        setting.set_typed_value(default_value)
        await db.flush()
        await db.refresh(setting)

        logger.info(
            "Reset setting to default",
            extra={"setting_key": key, "default_value": default_value},
        )
        return setting

    @staticmethod
    async def create_setting(
        db: AsyncSession,
        key: str,
        category: str,
        value: Any,
        display_name: str,
        description: str | None = None,
        validation: dict[str, Any] | None = None,
        subcategory: str | None = None,
    ) -> Setting:
        """
        Create a new setting.

        Args:
            db: Database session
            key: Unique setting key
            category: Category (check, alert, system, job)
            value: Initial value
            display_name: Human-readable name
            description: Optional description
            validation: Optional validation rules
            subcategory: Optional subcategory for grouping

        Returns:
            Created Setting object
        """
        # Determine type
        value_type = type(value).__name__
        if value_type == "int":
            value_type = "int"
        elif value_type == "float":
            value_type = "float"
        elif value_type == "bool":
            value_type = "bool"
        elif value_type == "str":
            value_type = "string"
        elif value_type == "list":
            value_type = "list"
        else:
            value_type = "string"

        value_jsonb = {"value": value, "type": value_type}

        setting = Setting(
            key=key,
            category=category,
            subcategory=subcategory,
            value=value_jsonb,
            default_value=value_jsonb,  # Initial value is also default
            display_name=display_name,
            description=description,
            validation=validation,
        )

        db.add(setting)
        await db.flush()
        await db.refresh(setting)

        logger.info(
            "Created setting",
            extra={"setting_key": key, "value": value},
        )
        return setting

    @staticmethod
    def clear_cache() -> None:
        """
        Clear the settings cache.

        Useful after bulk updates or during testing.
        """
        SettingsCoreService._cache.clear()
        logger.debug("Settings cache cleared")

    @staticmethod
    def _validate_value(value: Any, validation: dict[str, Any]) -> None:
        """
        Validate a value against validation rules.

        Args:
            value: Value to validate
            validation: Validation rules dict

        Raises:
            ValueError: If validation fails
        """
        if not isinstance(validation, dict):
            return  # No validation to perform

        # Min/max validation
        if "min" in validation and value < validation["min"]:
            raise ValueError(f"Value {value} is less than minimum {validation['min']}")

        if "max" in validation and value > validation["max"]:
            raise ValueError(f"Value {value} is greater than maximum {validation['max']}")

        # Enum validation
        if "enum" in validation and value not in validation["enum"]:
            raise ValueError(f"Value {value} not in allowed values: {validation['enum']}")

        # Timezone validation
        if validation.get("type") == "timezone":
            try:
                ZoneInfo(value)
            except ZoneInfoNotFoundError:
                raise ValueError(
                    f"Invalid timezone: '{value}'. Must be a valid IANA timezone (e.g., 'America/Chicago', 'UTC', 'Europe/London')"
                ) from None
            except Exception as e:
                raise ValueError(f"Invalid timezone format: {str(e)}") from e

    @staticmethod
    async def get_check_defaults(db: AsyncSession) -> dict[str, Any]:
        """
        Get all check defaults as a convenient dict.

        Returns:
            Dict with check default settings
        """
        return {
            "interval_seconds": await SettingsCoreService.get_setting(
                db, "check.default_interval", 60
            ),
            "timeout_seconds": await SettingsCoreService.get_setting(
                db, "check.default_timeout", 10
            ),
            "retry_attempts": await SettingsCoreService.get_setting(
                db, "check.default_retry_attempts", 2
            ),
            "retry_interval_seconds": await SettingsCoreService.get_setting(
                db, "check.default_retry_interval", 30
            ),
            "expected_status": await SettingsCoreService.get_setting(
                db, "check.default_expected_status", 200
            ),
            "verify_ssl": await SettingsCoreService.get_setting(
                db, "check.default_verify_ssl", False
            ),
            "http_method": await SettingsCoreService.get_setting(
                db, "check.default_http_method", "GET"
            ),
        }

    @staticmethod
    async def get_alert_defaults(db: AsyncSession) -> dict[str, Any]:
        """
        Get all alert defaults as a convenient dict.

        Returns:
            Dict with alert default settings
        """
        return {
            "consecutive_failures": await SettingsCoreService.get_setting(
                db, "alert.default_consecutive_failures", 1
            ),
            "notify_on_recovery": await SettingsCoreService.get_setting(
                db, "alert.default_notify_on_recovery", True
            ),
            "latency_threshold_ms": await SettingsCoreService.get_setting(
                db, "alert.default_latency_threshold", 1000
            ),
            "ssl_cert_warning_days": await SettingsCoreService.get_setting(
                db, "alert.ssl_cert_warning_days", 30
            ),
            "ssl_cert_critical_days": await SettingsCoreService.get_setting(
                db, "alert.ssl_cert_critical_days", 14
            ),
        }

    @staticmethod
    async def ensure_default_settings(db: AsyncSession) -> None:
        """
        Seed all default settings from the single source of truth
        (app/core/default_settings.py). Idempotent: inserts any key that does
        not yet exist and never overwrites an operator-changed value. Called
        once at startup; replaces _init_default_settings + the ensure_*_defaults.
        """
        for entry in DEFAULT_SETTINGS:
            if await SettingCRUD.get_by_key(db, entry["key"]) is not None:
                continue
            await SettingsCoreService.create_setting(
                db=db,
                key=entry["key"],
                category=entry["category"],
                value=entry["value"],
                display_name=entry["display_name"],
                description=entry.get("description"),
                validation=entry.get("validation"),
                subcategory=entry.get("subcategory"),
            )
        logger.info("Default settings ensured", extra={"count": len(DEFAULT_SETTINGS)})

    @staticmethod
    async def get_security_settings(db: AsyncSession) -> dict[str, Any]:
        """
        Get all security settings as a convenient dict.

        Returns:
            Dict with security settings
        """
        return {
            "session_lifetime_days": await SettingsCoreService.get_setting(
                db, "security.session_lifetime_days", 7
            ),
            "max_failed_attempts": await SettingsCoreService.get_setting(
                db, "security.max_failed_attempts", 5
            ),
            "account_lock_duration_minutes": await SettingsCoreService.get_setting(
                db, "security.account_lock_duration_minutes", 30
            ),
            "rate_limit_enabled": await SettingsCoreService.get_setting(
                db, "security.rate_limit_enabled", True
            ),
            "login_rate_limit": await SettingsCoreService.get_setting(
                db, "security.login_rate_limit", "10/15minutes"
            ),
            "api_rate_limit": await SettingsCoreService.get_setting(
                db, "security.api_rate_limit", "100/minute"
            ),
            "registration_rate_limit": await SettingsCoreService.get_setting(
                db, "security.registration_rate_limit", "5/hour"
            ),
        }

    @staticmethod
    async def update_security_settings(
        db: AsyncSession,
        session_lifetime_days: int | None = None,
        max_failed_attempts: int | None = None,
        account_lock_duration_minutes: int | None = None,
        rate_limit_enabled: bool | None = None,
        login_rate_limit: str | None = None,
        api_rate_limit: str | None = None,
        registration_rate_limit: str | None = None,
    ) -> dict[str, Any]:
        """
        Update security settings.

        Args:
            db: Database session
            session_lifetime_days: Optional new session lifetime
            max_failed_attempts: Optional new failed attempts limit
            account_lock_duration_minutes: Optional new lock duration
            rate_limit_enabled: Optional rate limiting toggle
            login_rate_limit: Optional login rate limit string
            api_rate_limit: Optional API rate limit string
            registration_rate_limit: Optional registration rate limit string

        Returns:
            Updated security settings dict
        """
        if session_lifetime_days is not None:
            await SettingsCoreService.update_setting(
                db, "security.session_lifetime_days", session_lifetime_days
            )

        if max_failed_attempts is not None:
            await SettingsCoreService.update_setting(
                db, "security.max_failed_attempts", max_failed_attempts
            )

        if account_lock_duration_minutes is not None:
            await SettingsCoreService.update_setting(
                db,
                "security.account_lock_duration_minutes",
                account_lock_duration_minutes,
            )

        if rate_limit_enabled is not None:
            await SettingsCoreService.update_setting(
                db, "security.rate_limit_enabled", rate_limit_enabled
            )

        if login_rate_limit is not None:
            await SettingsCoreService.update_setting(
                db, "security.login_rate_limit", login_rate_limit
            )

        if api_rate_limit is not None:
            await SettingsCoreService.update_setting(db, "security.api_rate_limit", api_rate_limit)

        if registration_rate_limit is not None:
            await SettingsCoreService.update_setting(
                db, "security.registration_rate_limit", registration_rate_limit
            )

        return await SettingsCoreService.get_security_settings(db)

    @staticmethod
    async def reset_security_settings(db: AsyncSession) -> dict[str, Any]:
        """
        Reset all security settings to defaults.

        Args:
            db: Database session

        Returns:
            Reset security settings dict
        """
        await SettingsCoreService.reset_setting(db, "security.session_lifetime_days")
        await SettingsCoreService.reset_setting(db, "security.max_failed_attempts")
        await SettingsCoreService.reset_setting(db, "security.account_lock_duration_minutes")
        await SettingsCoreService.reset_setting(db, "security.rate_limit_enabled")
        await SettingsCoreService.reset_setting(db, "security.login_rate_limit")
        await SettingsCoreService.reset_setting(db, "security.api_rate_limit")
        await SettingsCoreService.reset_setting(db, "security.registration_rate_limit")

        logger.info("Reset all security settings to defaults")
        return await SettingsCoreService.get_security_settings(db)

    @staticmethod
    async def get_general_settings(db: AsyncSession) -> dict[str, Any]:
        """
        Get all general settings as a convenient dict.

        Returns:
            Dict with general settings
        """
        return {
            "timezone": await SettingsCoreService.get_setting(db, "general.timezone", "UTC"),
            "date_format": await SettingsCoreService.get_setting(db, "general.date_format", "long"),
            "time_format": await SettingsCoreService.get_setting(db, "general.time_format", "24h"),
            "default_page_size": await SettingsCoreService.get_setting(
                db, "general.default_page_size", 50
            ),
            "dashboard_refresh_interval": await SettingsCoreService.get_setting(
                db, "general.dashboard_refresh_interval", 10
            ),
            "default_chart_time_range": await SettingsCoreService.get_setting(
                db, "general.default_chart_time_range", "4h"
            ),
            "agent_stale_threshold_seconds": await SettingsCoreService.get_setting(
                db, "general.agent_stale_threshold_seconds", 300
            ),
        }

    @staticmethod
    async def get_metrics_settings(db: AsyncSession) -> dict[str, Any]:
        """
        Get all metrics settings as a convenient dict.

        Returns:
            Dict with metrics settings
        """
        return {
            "enabled": await SettingsCoreService.get_setting(db, "metrics.enabled", True),
            "auth_required": await SettingsCoreService.get_setting(
                db, "metrics.auth_required", False
            ),
            "bearer_token": await SettingsCoreService.get_setting(db, "metrics.bearer_token", ""),
            "agent_timeout_seconds": await SettingsCoreService.get_setting(
                db, "metrics.agent_timeout_seconds", 300
            ),
        }

    @staticmethod
    async def generate_metrics_token(db: AsyncSession) -> str:
        """
        Generate a new bearer token for Prometheus metrics.

        Returns:
            New bearer token (plaintext, shown once)
        """

        # Generate token with luxswirl_metrics_ prefix
        new_token = f"luxswirl_metrics_{secrets.token_hex(24)}"

        # Update the setting
        await SettingsCoreService.update_setting(db, "metrics.bearer_token", new_token)

        logger.info("Generated new Prometheus metrics bearer token")
        return new_token
