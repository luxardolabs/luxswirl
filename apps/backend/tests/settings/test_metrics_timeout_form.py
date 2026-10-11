"""The metrics card's agent-timeout form: a bad value is a form error, a good
one is stored and the card re-renders from the stored settings."""

from __future__ import annotations

import pytest
from app.services.core.settings_core_service import SettingsCoreService
from app.services.views.settings_view_service import SettingsViewService
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("", "Timeout value required"),
        ("soon", "Invalid timeout value"),
        ("29", "Timeout must be between 30 and 3600 seconds"),
        ("3601", "Timeout must be between 30 and 3600 seconds"),
    ],
)
async def test_a_bad_value_is_the_form_error(
    db: AsyncSession, value: str, message: str
) -> None:
    await SettingsCoreService.ensure_default_settings(db)

    with pytest.raises(ValueError, match=message):
        await SettingsViewService.update_metrics_timeout(db, value)


async def test_a_good_value_is_stored_and_returned(db: AsyncSession) -> None:
    await SettingsCoreService.ensure_default_settings(db)

    metrics = await SettingsViewService.update_metrics_timeout(db, "120")

    assert metrics["agent_timeout_seconds"] == 120
    assert (
        await SettingsCoreService.get_setting(db, "metrics.agent_timeout_seconds", 0)
        == 120
    )
