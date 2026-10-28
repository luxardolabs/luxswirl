"""
Settings service - business logic for configurable system defaults.
"""

import math
import secrets
from typing import Any, TypeVar, cast, overload
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, SessionTransaction

from app.core.config import settings
from app.core.default_settings import DEFAULT_SETTINGS
from app.core.display_settings import update_display_settings
from app.core.exceptions import NotFoundException, ValidationException
from app.core.rate_limit import update_rate_limits
from app.core.rate_limit_format import check_rate_limit
from app.crud.setting_crud import SettingCRUD
from app.models.enum_model import SettingValueType
from app.models.setting_model import Setting, value_type_of
from app.schemas.setting_schema import (
    AlertDefaults,
    CheckDefaults,
    NetworkProtection,
    SettingResponse,
    SettingScalar,
)
from app.services.core.timescale_core_service import TimescaleCoreService
from app.shared.logger import get_logger

logger = get_logger("luxswirl.services.settings")

_T = TypeVar("_T")

# Settings whose value is generated, never set to a chosen one. Each is a
# secret, so every read outside the setting's own use shows it masked.
GENERATED_KEYS = frozenset({"metrics.bearer_token"})

# The display preference each display setting sets.
_DISPLAY_KEYS = {
    "general.timezone": "timezone",
    "general.date_format": "date_format",
    "general.time_format": "time_format",
}

# The route limit each rate-limit setting sets, by its name in `RateLimits`.
_RATE_LIMIT_KEYS = {
    "security.login_rate_limit": "login",
    "security.api_rate_limit": "api",
    "security.registration_rate_limit": "registration",
}

# The TimescaleDB policy each database setting sets: (relation, policy).
_POLICIES = {
    "database.retention_days": ("check_results", "retention"),
    "database.compress_after_days": ("check_results", "compression"),
    "database.hourly_aggregate_retention_days": ("check_results_hourly", "retention"),
    "database.daily_aggregate_retention_days": ("check_results_daily", "retention"),
}


# The limit in effect lives in process memory, outside the transaction, so a
# change waits on the session until its row lands: a commit applies it, and the
# rollback of the transaction (or savepoint) it was made in drops it.
_PENDING_LIMITS = "pending_rate_limits"


def _defer_rate_limit(session: Session, limit: str, value: str) -> None:
    """Apply `limit` = `value` when `session` commits the change, never before."""
    pending = session.info.get(_PENDING_LIMITS)
    if pending is None:
        pending = session.info[_PENDING_LIMITS] = []
        event.listen(session, "after_commit", _apply_committed_rate_limits)
        event.listen(session, "after_soft_rollback", _drop_rolled_back_rate_limits)
        event.listen(session, "after_transaction_end", _forget_unlanded_rate_limits)
    made_in = session.get_nested_transaction() or session.get_transaction()
    pending.append((made_in, {limit: value}))


def _apply_committed_rate_limits(session: Session) -> None:
    """A transaction committed. A savepoint's changes now belong to the
    transaction around it; the root's have landed, and apply."""
    pending = session.info.get(_PENDING_LIMITS)
    if not pending:
        return
    # SQLAlchemy reports a savepoint's commit here too, while it is still current.
    committed = session.get_nested_transaction()
    if committed is not None:
        pending[:] = [
            (committed.parent if made_in is committed else made_in, changes)
            for made_in, changes in pending
        ]
        return
    for _, changes in pending:
        update_rate_limits(**changes)
    pending.clear()


def _drop_rolled_back_rate_limits(
    session: Session, previous_transaction: SessionTransaction
) -> None:
    """A transaction rolled back: forget the changes made in it or inside it.

    A failed flush reports only its own subtransaction, but rolls back to the
    savepoint (or root) around it, so the changes go from there down.
    """
    pending = session.info.get(_PENDING_LIMITS)
    if not pending:
        return
    ended = previous_transaction
    while not ended.nested and ended.parent is not None:
        ended = ended.parent
    pending[:] = [
        (made_in, changes)
        for made_in, changes in pending
        if not _within(made_in, ended)
    ]


def _forget_unlanded_rate_limits(
    session: Session, transaction: SessionTransaction
) -> None:
    """The root transaction ended. A commit has applied its changes by now, so
    whatever is left did not land (a close, say) and must not wait for the next."""
    if transaction.parent is None:
        pending = session.info.get(_PENDING_LIMITS)
        if pending:
            pending.clear()


def _within(transaction: SessionTransaction | None, ended: SessionTransaction) -> bool:
    """Whether `transaction` is `ended` or nested inside it."""
    while transaction is not None:
        if transaction is ended:
            return True
        transaction = transaction.parent
    return False


def _stored_scalar(stored: object) -> object:
    """The value a JSON column holds: the app writes {"value": ..., "type": ...},
    but a row written outside the app can hold the bare value."""
    if isinstance(stored, dict):
        return stored.get("value")
    return stored


def stored_value_type(stored: dict[str, Any]) -> SettingValueType:
    """A stored value's type: its tag, or, for a row written without one, the
    type of the value it holds."""
    tag = stored.get("type")
    if tag in set(SettingValueType):
        return SettingValueType(tag)
    return value_type_of(stored.get("value"))


def _fits_type(value: SettingScalar, value_type: SettingValueType) -> bool:
    """Whether `value` is of the setting's stored type (a bool is no number,
    and NaN or an infinity no float)."""
    if isinstance(value, bool):
        return value_type == SettingValueType.BOOL
    if value_type == SettingValueType.INT:
        return isinstance(value, int)
    if value_type == SettingValueType.FLOAT:
        return isinstance(value, int) or (
            isinstance(value, float) and math.isfinite(value)
        )
    if value_type == SettingValueType.STRING:
        return isinstance(value, str)
    if value_type == SettingValueType.LIST:
        return isinstance(value, list)
    if value_type == SettingValueType.DICT:
        return isinstance(value, dict)
    return False


class SettingsCoreService:
    """Service for settings operations."""

    @overload
    @staticmethod
    async def get_setting(db: AsyncSession, key: str) -> SettingScalar: ...

    @overload
    @staticmethod
    async def get_setting(db: AsyncSession, key: str, default: _T) -> _T: ...

    @staticmethod
    async def get_setting(
        db: AsyncSession,
        key: str,
        default: SettingScalar | _T = None,
    ) -> SettingScalar | _T:
        """
        Get a setting value by key.

        Falls back to default if not found.

        Args:
            db: Database session
            key: Setting key (e.g., "check.default_interval")
            default: Default value to return if setting not found

        Returns:
            The typed value of the setting, or `default` if not found.

            The return type follows the DEFAULT: a setting has one declared value type,
            and every caller passes a default of that type, so `get_setting(db, k, 50)`
            is an `int` to mypy. Typing this `Any` instead blinded every caller — it hid
            `timedelta(days=<a dict>)` and `compare_digest(<a list>)` shaped bugs.
        """
        setting = await SettingCRUD.get_by_key(db, key)

        if not setting:
            logger.debug(
                "Setting not found, using default",
                extra={"setting_key": key, "default": default},
            )
            return default

        # Return typed value (declared per setting; see the docstring)
        return cast("SettingScalar | _T", setting.typed_value)

    @staticmethod
    async def get_setting_response(
        db: AsyncSession,
        key: str,
    ) -> SettingResponse | None:
        """
        Get a whole setting by key (not just the value).

        Args:
            db: Database session
            key: Setting key (e.g., "check.default_interval")

        Returns:
            The setting, or None if not found
        """
        setting = await SettingCRUD.get_by_key(db, key)
        return None if setting is None else SettingResponse.model_validate(setting)

    @staticmethod
    async def list_settings(
        db: AsyncSession, category: str | None = None
    ) -> list[SettingResponse]:
        """Every setting by key, or one category's."""
        rows = (
            await SettingCRUD.list_all(db)
            if category is None
            else await SettingCRUD.list_by_category(db, category)
        )
        return [SettingResponse.model_validate(setting) for setting in rows]

    @staticmethod
    async def get_settings_by_category(
        db: AsyncSession,
        category: str,
    ) -> list[SettingResponse]:
        """
        Get all settings for a specific category.

        Args:
            db: Database session
            category: Category name (check, alert, system, job)

        Returns:
            The category's settings
        """
        return [
            SettingResponse.model_validate(setting)
            for setting in await SettingCRUD.list_by_category(db, category)
        ]

    @staticmethod
    async def update_setting(
        db: AsyncSession,
        key: str,
        new_value: SettingScalar,
    ) -> SettingResponse:
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
        setting = await SettingCRUD.save_setting(db, setting)

        # The key alone: a value can be a secret (the metrics bearer token).
        logger.info("Updated setting", extra={"setting_key": key})
        return SettingResponse.model_validate(setting)

    @staticmethod
    async def set_setting(
        db: AsyncSession, key: str, new_value: SettingScalar
    ) -> SettingResponse:
        """Set a setting to a chosen value, by any path, and apply what it governs.

        The value must be of the setting's stored type and meet its validation
        rules. A generated setting (the metrics bearer token) is not set this way.

        Raises:
            NotFoundException: No setting has the key.
            ValidationException: The value does not fit the setting.
        """
        setting = await SettingCRUD.get_by_key(db, key)
        if not setting:
            raise NotFoundException(f"Setting with key '{key}' not found")
        if key in GENERATED_KEYS:
            raise ValidationException(f"{key} is generated, not set to a value")
        value_type = stored_value_type(SettingResponse.model_validate(setting).value)
        if not _fits_type(new_value, value_type):
            raise ValidationException(f"{key} takes a {value_type.value} value")
        if value_type == SettingValueType.FLOAT and isinstance(new_value, int):
            new_value = float(new_value)
        try:
            SettingsCoreService._validate_value(new_value, setting.validation)
            # What the setting governs moves first: a value its policy refuses
            # leaves the setting as it was.
            await SettingsCoreService._apply(db, key, new_value)
        except ValueError as e:
            raise ValidationException(str(e)) from None
        return await SettingsCoreService.update_setting(db, key, new_value)

    @staticmethod
    async def _apply(db: AsyncSession, key: str, value: SettingScalar) -> None:
        """Make what the setting governs follow its new value."""
        preference = _DISPLAY_KEYS.get(key)
        if preference is not None and isinstance(value, str):
            update_display_settings(**{preference: value})
        limit = _RATE_LIMIT_KEYS.get(key)
        if limit is not None and isinstance(value, str):
            check_rate_limit(value)
            _defer_rate_limit(db.sync_session, limit, value)
        policy = _POLICIES.get(key)
        if policy is None or isinstance(value, bool) or not isinstance(value, int):
            return
        relation, kind = policy
        if kind == "compression":
            await TimescaleCoreService.update_compression_policy(db, relation, value)
        else:
            await TimescaleCoreService.update_retention_policy(db, relation, value)
        logger.info(
            "Updated TimescaleDB policy",
            extra={"setting_key": key, "relation": relation, "days": value},
        )

    @staticmethod
    async def reset_setting(
        db: AsyncSession,
        key: str,
    ) -> SettingResponse:
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

        # Reset to default. The column is JSON, so a row written outside the app
        # could hold a bare value rather than the {"value": ..., "type": ...} the
        # app writes; read it as the JSON it is before taking the value out.
        stored_default: object = setting.default_value
        if isinstance(stored_default, dict):
            default_value = stored_default.get("value")
        else:
            default_value = stored_default
        try:
            await SettingsCoreService._apply(db, key, default_value)
        except ValueError as e:
            raise ValidationException(str(e)) from None
        setting.set_typed_value(default_value)
        setting = await SettingCRUD.save_setting(db, setting)

        logger.info("Reset setting to default", extra={"setting_key": key})
        return SettingResponse.model_validate(setting)

    @staticmethod
    async def create_setting(
        db: AsyncSession,
        key: str,
        category: str,
        value: SettingScalar,
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

        setting = await SettingCRUD.add_setting(db, setting)

        logger.info(
            "Created setting",
            extra={"setting_key": key, "value": value},
        )
        return setting

    @staticmethod
    def _validate_value(
        value: SettingScalar, validation: dict[str, Any] | None
    ) -> None:
        """
        Validate a value against validation rules.

        Args:
            value: Value to validate
            validation: Validation rules dict

        Raises:
            ValueError: If validation fails
        """
        if not isinstance(validation, dict):
            return  # No rules (NULL, or the JSON null literal) - nothing to check

        # Min/max validation, for a number (any other value is no number)
        if "min" in validation or "max" in validation:
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValueError(f"Value {value!r} is not a number")
            if "min" in validation and value < validation["min"]:
                raise ValueError(
                    f"Value {value} is less than minimum {validation['min']}"
                )
            if "max" in validation and value > validation["max"]:
                raise ValueError(
                    f"Value {value} is greater than maximum {validation['max']}"
                )

        # Enum validation
        if "enum" in validation and value not in validation["enum"]:
            raise ValueError(
                f"Value {value} not in allowed values: {validation['enum']}"
            )

        # Timezone validation
        if validation.get("type") == "timezone":
            if not isinstance(value, str):
                raise ValueError(
                    f"Invalid timezone: {value!r}. Must be a string IANA timezone name"
                )
            try:
                ZoneInfo(value)
            except ZoneInfoNotFoundError:
                raise ValueError(
                    f"Invalid timezone: '{value}'. Must be a valid IANA timezone (e.g., 'America/Chicago', 'UTC', 'Europe/London')"
                ) from None
            except Exception as e:
                raise ValueError(f"Invalid timezone format: {str(e)}") from e

    @staticmethod
    async def get_network_protection(db: AsyncSession) -> NetworkProtection:
        """The SSRF settings that decide which addresses a check may reach."""
        return NetworkProtection(
            block_cloud_metadata=await SettingsCoreService.get_setting(
                db, "security.block_cloud_metadata", True
            ),
            block_private_networks=await SettingsCoreService.get_setting(
                db, "security.block_private_networks", False
            ),
        )

    @staticmethod
    async def get_check_defaults(db: AsyncSession) -> CheckDefaults:
        """
        Get all check defaults as their typed bundle.

        Returns:
            The check defaults
        """
        return CheckDefaults.model_validate(
            {
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
        )

    @staticmethod
    async def get_alert_defaults(db: AsyncSession) -> AlertDefaults:
        """
        Get all alert defaults as their typed bundle.

        Returns:
            The alert defaults
        """
        return AlertDefaults.model_validate(
            {
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
        )

    @staticmethod
    async def ensure_default_settings(db: AsyncSession) -> None:
        """
        Seed all default settings from the single source of truth
        (app/core/default_settings.py). Idempotent: inserts any key that does
        not yet exist and never overwrites an operator-changed value. Called
        once at startup; replaces _init_default_settings + the ensure_*_defaults.
        """
        existing = await SettingCRUD.existing_keys(db)
        for entry in DEFAULT_SETTINGS:
            if entry["key"] in existing:
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
    async def load_rate_limits(db: AsyncSession) -> None:
        """Put the stored rate-limit settings in effect, at startup.

        The environment's limit is each setting's default, so a reset returns to
        the environment as it is now. A setting still at its stored default takes
        the environment's value too, so a changed environment applies. A value an
        operator set is kept. A stored value that is not a limit leaves the
        environment's in effect.
        """
        for key, limit in _RATE_LIMIT_KEYS.items():
            setting = await SettingCRUD.get_by_key(db, key)
            if setting is None:
                continue
            configured: str = getattr(settings.security, f"{limit}_rate_limit")
            value = _stored_scalar(setting.value)
            default = _stored_scalar(setting.default_value)
            if default != configured:
                if value == default:
                    setting.set_typed_value(configured)
                    value = configured
                setting.default_value = {
                    "value": configured,
                    "type": value_type_of(configured),
                }
                setting = await SettingCRUD.save_setting(db, setting)
            try:
                if not isinstance(value, str):
                    raise ValueError(f"{value!r} is not a rate limit")
                update_rate_limits(**{limit: value})
            except ValueError:
                logger.warning(
                    "Stored rate limit is not a limit; the environment's applies",
                    extra={"setting_key": key},
                )

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
    async def get_general_settings(db: AsyncSession) -> dict[str, Any]:
        """
        Get all general settings as a convenient dict.

        Returns:
            Dict with general settings
        """
        return {
            "timezone": await SettingsCoreService.get_setting(
                db, "general.timezone", "UTC"
            ),
            "date_format": await SettingsCoreService.get_setting(
                db, "general.date_format", "long"
            ),
            "time_format": await SettingsCoreService.get_setting(
                db, "general.time_format", "24h"
            ),
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
            "enabled": await SettingsCoreService.get_setting(
                db, "metrics.enabled", True
            ),
            "auth_required": await SettingsCoreService.get_setting(
                db, "metrics.auth_required", False
            ),
            "bearer_token": await SettingsCoreService.get_setting(
                db, "metrics.bearer_token", ""
            ),
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
