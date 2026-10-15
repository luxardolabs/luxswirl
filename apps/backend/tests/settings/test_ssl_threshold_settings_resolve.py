"""The SSL threshold settings the operator edits must be the ones the code reads.

`alert.ssl_cert_warning_days` / `alert.ssl_cert_critical_days` are registered in
default_settings.py under the `alert.` prefix, but the ingest path looked them
up without it — so `get_setting` never found them and every certificate was
judged against the built-in 30/14 no matter what the operator configured.
"""

from __future__ import annotations

import pytest
from app.core.default_settings import DEFAULT_SETTINGS
from app.services.core.settings_core_service import SettingsCoreService
from sqlalchemy.ext.asyncio import AsyncSession

SSL_KEYS = ("alert.ssl_cert_warning_days", "alert.ssl_cert_critical_days")


@pytest.mark.parametrize("key", SSL_KEYS)
def test_key_is_registered(key: str):
    """The reader's key must exist in the registry the settings UI writes."""
    assert any(s["key"] == key for s in DEFAULT_SETTINGS)


@pytest.mark.parametrize(
    ("key", "field"), list(zip(SSL_KEYS, ("warning", "critical"), strict=True))
)
async def test_operator_value_reaches_the_reader(
    db: AsyncSession, key: str, field: str
):
    """Write the setting, then read it back the way the code does."""
    await SettingsCoreService.create_setting(
        db, key=key, category="alert", value=3, display_name="SSL threshold"
    )
    assert await SettingsCoreService.get_setting(db, key, default=999) == 3

    defaults = await SettingsCoreService.get_alert_defaults(db)
    assert getattr(defaults, f"ssl_cert_{field}_days") == 3
