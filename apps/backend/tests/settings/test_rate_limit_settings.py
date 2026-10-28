"""The rate-limit settings govern the limits the routes count, from the next request.

`security.login_rate_limit`, `security.api_rate_limit` and
`security.registration_rate_limit` start at their environment settings. A value
set in Settings or the admin API replaces the limit at once, a reset puts the
environment's back, and a value that is not a limit is refused with the limit
left as it was. These go through the real app, so a route still reading its
limit from the environment fails here.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from app.core.config import SecuritySettings, settings
from app.core.exceptions import ValidationException
from app.core.rate_limit import current_rate_limits, limiter, update_rate_limits
from app.crud.setting_crud import SettingCRUD
from app.main import app
from app.models.setting_model import Setting
from app.services.core.settings_core_service import SettingsCoreService
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

OWN = settings.allowed_hosts[0]
_WRONG = {"Authorization": "Bearer not-the-right-key"}


@pytest.fixture(autouse=True)
def _limits_restored() -> Iterator[None]:
    """The limits in effect live in process memory: leave them as found."""
    before = current_rate_limits()
    limiter.reset()
    try:
        yield
    finally:
        update_rate_limits(
            login=before.login, api=before.api, registration=before.registration
        )
        limiter.reset()


@pytest_asyncio.fixture
async def seeded(db: AsyncSession) -> AsyncSession:
    await SettingsCoreService.ensure_default_settings(db)
    return db


@pytest_asyncio.fixture
async def client(seeded: AsyncSession, monkeypatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url=f"https://{OWN}"
    ) as c:
        yield c


async def test_a_login_limit_set_in_settings_counts_from_the_next_request(
    client: AsyncClient, seeded: AsyncSession
) -> None:
    await SettingsCoreService.set_setting(
        seeded, "security.login_rate_limit", "2/minute"
    )
    await seeded.commit()

    statuses = [
        (
            await client.post(
                "/login", data={"username": "nobody", "password": "wrong"}
            )
        ).status_code
        for _ in range(3)
    ]

    assert 429 not in statuses[:2], statuses
    assert statuses[2] == 429, statuses


async def test_a_registration_limit_set_in_settings_counts_refused_keys(
    client: AsyncClient, seeded: AsyncSession
) -> None:
    await SettingsCoreService.set_setting(
        seeded, "security.registration_rate_limit", "1/minute"
    )
    await seeded.commit()

    statuses = [
        (
            await client.post(
                "/api/v1/agent/register", json={"hostname": "probe"}, headers=_WRONG
            )
        ).status_code
        for _ in range(2)
    ]

    assert statuses == [401, 429], statuses


async def test_an_api_limit_set_in_settings_limits_the_metrics_scrape(
    client: AsyncClient, seeded: AsyncSession
) -> None:
    await SettingsCoreService.set_setting(seeded, "security.api_rate_limit", "1/minute")
    await seeded.commit()

    statuses = [(await client.get("/metrics")).status_code for _ in range(2)]

    assert statuses == [200, 429], statuses


@pytest.mark.parametrize(
    "value",
    [
        "lots",
        "10 per fortnight",
        "0/minute",
        "10/minute;0/hour",
        "",
        # Parses, but its period overflows the limiter's storage on every request.
        "1/" + "9" * 400 + "years",
        "1/2years",
        ";".join(["1/minute"] * 20),
    ],
)
async def test_a_value_that_is_not_a_limit_is_refused_and_the_limit_kept(
    seeded: AsyncSession, value: str
) -> None:
    before = current_rate_limits()

    with pytest.raises(ValidationException):
        await SettingsCoreService.set_setting(
            seeded, "security.login_rate_limit", value
        )

    assert current_rate_limits() == before
    stored = await SettingCRUD.get_by_key(seeded, "security.login_rate_limit")
    assert stored is not None
    assert stored.typed_value == settings.security.login_rate_limit


async def test_a_reset_puts_the_environment_limit_back_as_it_is_now(
    seeded: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(settings.security, "login_rate_limit", "3/minute")
    await SettingsCoreService.load_rate_limits(seeded)
    await SettingsCoreService.set_setting(
        seeded, "security.login_rate_limit", "20/minute"
    )
    await seeded.commit()
    assert current_rate_limits().login == "20/minute"
    # The environment changes under a value an operator set, and the server restarts.
    monkeypatch.setattr(settings.security, "login_rate_limit", "5/minute")
    await SettingsCoreService.load_rate_limits(seeded)
    assert current_rate_limits().login == "20/minute"

    await SettingsCoreService.reset_setting(seeded, "security.login_rate_limit")
    await seeded.commit()

    stored = await SettingCRUD.get_by_key(seeded, "security.login_rate_limit")
    assert stored is not None
    assert current_rate_limits().login == "5/minute"
    assert stored.typed_value == "5/minute"


async def test_a_set_that_is_rolled_back_leaves_the_limit_in_effect(
    seeded: AsyncSession,
) -> None:
    before = current_rate_limits()

    await SettingsCoreService.set_setting(seeded, "security.login_rate_limit", "3/hour")
    await seeded.rollback()

    assert current_rate_limits() == before


async def test_a_set_rolled_back_is_not_applied_by_a_later_commit(
    seeded: AsyncSession,
) -> None:
    before = current_rate_limits()

    await SettingsCoreService.set_setting(
        seeded, "security.login_rate_limit", "999999/second"
    )
    await seeded.rollback()
    await SettingsCoreService.ensure_default_settings(seeded)
    await SettingsCoreService.set_setting(seeded, "security.session_lifetime_days", 8)
    await seeded.commit()

    assert current_rate_limits() == before


async def test_a_set_in_a_savepoint_rolled_back_is_not_applied_by_the_outer_commit(
    seeded: AsyncSession,
) -> None:
    before = current_rate_limits()

    savepoint = await seeded.begin_nested()
    await SettingsCoreService.set_setting(
        seeded, "security.login_rate_limit", "999999/second"
    )
    await savepoint.rollback()
    await seeded.commit()

    assert current_rate_limits() == before


def _duplicate_login_limit_row() -> Setting:
    """A row whose key is taken, so flushing it fails."""
    taken = {"value": "x", "type": "string"}
    return Setting(
        key="security.login_rate_limit",
        category="security",
        value=taken,
        default_value=taken,
        display_name="duplicate",
    )


async def test_a_set_in_a_savepoint_a_failed_flush_rolls_back_is_not_applied(
    seeded: AsyncSession,
) -> None:
    before = current_rate_limits()

    with pytest.raises(IntegrityError):
        async with seeded.begin_nested():
            await SettingsCoreService.set_setting(
                seeded, "security.login_rate_limit", "4/hour"
            )
            seeded.add(_duplicate_login_limit_row())
            await seeded.flush()
    await seeded.commit()

    assert current_rate_limits() == before


async def test_a_set_outside_a_savepoint_a_failed_flush_rolls_back_still_lands(
    seeded: AsyncSession,
) -> None:
    await SettingsCoreService.set_setting(seeded, "security.login_rate_limit", "4/hour")
    with pytest.raises(IntegrityError):
        async with seeded.begin_nested():
            seeded.add(_duplicate_login_limit_row())
            await seeded.flush()
    await seeded.commit()

    assert current_rate_limits().login == "4/hour"


async def test_a_set_on_a_session_closed_before_commit_is_not_applied_by_its_reuse(
    seeded: AsyncSession,
) -> None:
    before = current_rate_limits()

    await SettingsCoreService.set_setting(seeded, "security.login_rate_limit", "4/hour")
    await seeded.close()
    await SettingsCoreService.ensure_default_settings(seeded)
    await SettingsCoreService.set_setting(seeded, "security.session_lifetime_days", 8)
    await seeded.commit()

    assert current_rate_limits() == before


async def test_a_set_in_a_savepoint_that_lands_is_applied_by_the_outer_commit(
    seeded: AsyncSession,
) -> None:
    savepoint = await seeded.begin_nested()
    await SettingsCoreService.set_setting(seeded, "security.login_rate_limit", "4/hour")
    await savepoint.commit()
    assert current_rate_limits().login == settings.security.login_rate_limit

    await seeded.commit()

    assert current_rate_limits().login == "4/hour"


async def test_a_limit_too_long_for_its_storage_does_not_take_login_down(
    client: AsyncClient, seeded: AsyncSession
) -> None:
    with pytest.raises(ValidationException):
        await SettingsCoreService.set_setting(
            seeded, "security.login_rate_limit", "1/" + "9" * 400 + "years"
        )
    await seeded.commit()

    response = await client.post(
        "/login", data={"username": "nobody", "password": "wrong"}
    )

    assert response.status_code != 500


async def test_a_reset_to_a_default_that_is_not_a_limit_is_refused(
    seeded: AsyncSession,
) -> None:
    setting = await SettingCRUD.get_by_key(seeded, "security.login_rate_limit")
    assert setting is not None
    setting.default_value = {"value": "10/15mins", "type": "string"}
    await SettingCRUD.save_setting(seeded, setting)

    with pytest.raises(ValidationException):
        await SettingsCoreService.reset_setting(seeded, "security.login_rate_limit")


@pytest.mark.parametrize("value", ["10/15mins", "0/minute", "1/" + "9" * 400 + "years"])
def test_an_environment_limit_that_is_not_a_limit_stops_the_server_starting(
    value: str,
) -> None:
    with pytest.raises(ValidationError):
        SecuritySettings(login_rate_limit=value)


async def test_startup_applies_a_limit_an_operator_set(seeded: AsyncSession) -> None:
    await SettingsCoreService.update_setting(
        seeded, "security.registration_rate_limit", "2/hour"
    )

    await SettingsCoreService.load_rate_limits(seeded)

    assert current_rate_limits().registration == "2/hour"


async def test_startup_follows_the_environment_for_a_limit_nobody_set(
    seeded: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(settings.security, "login_rate_limit", "7/minute")

    await SettingsCoreService.load_rate_limits(seeded)

    stored = await SettingCRUD.get_by_key(seeded, "security.login_rate_limit")
    assert stored is not None
    assert current_rate_limits().login == "7/minute"
    assert stored.typed_value == "7/minute"
    assert stored.default_value["value"] == "7/minute"


async def test_startup_keeps_the_environment_limit_over_a_stored_one_that_is_not_a_limit(
    seeded: AsyncSession,
) -> None:
    await SettingsCoreService.update_setting(seeded, "security.api_rate_limit", "lots")

    await SettingsCoreService.load_rate_limits(seeded)

    assert current_rate_limits().api == settings.security.api_rate_limit


@pytest.mark.parametrize("bare", ["5/minute", None])
async def test_startup_reads_a_row_that_holds_a_bare_value(
    seeded: AsyncSession, bare: str | None
) -> None:
    """A row written outside the app can hold the value itself, not the
    {"value": ..., "type": ...} the app writes; startup still comes up."""
    await seeded.execute(
        text("UPDATE settings SET value = CAST(:v AS json) WHERE key = :k"),
        {"v": json.dumps(bare), "k": "security.registration_rate_limit"},
    )
    seeded.expire_all()

    await SettingsCoreService.load_rate_limits(seeded)

    expected = bare or settings.security.registration_rate_limit
    assert current_rate_limits().registration == expected
