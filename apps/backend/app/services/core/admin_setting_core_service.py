"""
Admin setting core service - settings as the admin API serves them.

An admin's alone, as in the web UI. Setting and resetting go through
`SettingsCoreService`, which applies what each setting governs (TimescaleDB
policies, the display preferences) whatever the path.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundException
from app.models.enum_model import SettingCategory, SettingValueType
from app.schemas.admin_setting_schema import (
    MASK,
    AdminSetting,
    AdminSettingValue,
    MetricsToken,
)
from app.schemas.setting_schema import SettingResponse, SettingScalar
from app.services.core.settings_core_service import (
    GENERATED_KEYS,
    SettingsCoreService,
)


def _unwrapped(stored: object) -> SettingScalar:
    """A stored value without its type tag (a row written outside the app may
    hold the bare value)."""
    if isinstance(stored, dict) and "value" in stored:
        value: SettingScalar = stored["value"]
        return value
    return None


def _response(setting: SettingResponse) -> AdminSetting:
    generated = setting.key in GENERATED_KEYS
    value = _unwrapped(setting.value)
    default = _unwrapped(setting.default_value)
    if generated:
        value = MASK if value else ""
        default = MASK if default else ""
    stored_type = setting.value.get("type")
    value_type = (
        SettingValueType(stored_type)
        if stored_type in set(SettingValueType)
        else SettingValueType.STRING
    )
    return AdminSetting(
        key=setting.key,
        category=setting.category,
        subcategory=setting.subcategory,
        display_name=setting.display_name,
        description=setting.description,
        type=value_type,
        value=value,
        default_value=default,
        validation=setting.validation,
        generated=generated,
        updated_at=setting.updated_at,
    )


class AdminSettingCoreService:
    """List, read, set and reset settings, and generate the metrics token."""

    @staticmethod
    async def list_settings(
        db: AsyncSession, category: SettingCategory | None
    ) -> list[AdminSetting]:
        """Every setting by key, or one category's."""
        settings = await SettingsCoreService.list_settings(db, category)
        return [_response(s) for s in settings]

    @staticmethod
    async def get_setting(db: AsyncSession, key: str) -> AdminSetting:
        setting = await SettingsCoreService.get_setting_response(db, key)
        if setting is None:
            raise NotFoundException(f"Setting with key '{key}' not found")
        return _response(setting)

    @staticmethod
    async def set_setting(
        db: AsyncSession, key: str, data: AdminSettingValue
    ) -> AdminSetting:
        return _response(await SettingsCoreService.set_setting(db, key, data.value))

    @staticmethod
    async def reset_setting(db: AsyncSession, key: str) -> AdminSetting:
        return _response(await SettingsCoreService.reset_setting(db, key))

    @staticmethod
    async def generate_metrics_token(db: AsyncSession) -> MetricsToken:
        """A new metrics bearer token; the old one stops working now."""
        return MetricsToken(token=await SettingsCoreService.generate_metrics_token(db))
