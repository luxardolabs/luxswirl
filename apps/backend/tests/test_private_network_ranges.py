"""Block Private Network Targets covers every private and internal address.

With the setting on, a check may not reach RFC 1918, IPv6 unique-local,
loopback, the unspecified address (0.0.0.0 reaches the host on Linux) or CGNAT,
in any spelling a socket accepts. With it off, all of them are allowed. Revision
5d1f0c2a9e47 rewrites the setting's seeded description to name them.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from app.core.check_target_validator import CheckTargetBlockedError
from app.models.setting_model import Setting
from app.schemas.check_schema import CheckCreate
from app.services.core.check_core_service import CheckCoreService
from app.services.core.settings_core_service import SettingsCoreService
from app.shared.ssrf import SsrfBlockedError, assert_ip_allowed, assert_target_allowed
from fixtures.factories import make_agent
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

INTERNAL = [
    "10.0.0.5",
    "172.16.0.1",
    "192.168.1.1",
    "fd00::1",
    "127.0.0.1",
    "127.1.2.3",
    "::1",
    "0.0.0.0",
    "::",
    "100.64.0.1",
    "100.127.255.254",
    "::ffff:127.0.0.1",
]
EXTERNAL = ["192.0.2.10", "8.8.8.8", "100.128.0.1", "2001:4860:4860::8888"]


@pytest.mark.unit
@pytest.mark.parametrize("ip", INTERNAL)
def test_an_internal_address_is_refused_when_private_networks_are_blocked(
    ip: str,
) -> None:
    with pytest.raises(SsrfBlockedError, match="private network"):
        assert_ip_allowed(ip, block_private_networks=True)


@pytest.mark.unit
@pytest.mark.parametrize("ip", INTERNAL)
def test_an_internal_address_is_allowed_by_default(ip: str) -> None:
    assert_ip_allowed(ip)


@pytest.mark.unit
@pytest.mark.parametrize("ip", EXTERNAL)
def test_an_external_address_is_allowed_with_private_networks_blocked(
    ip: str,
) -> None:
    assert_ip_allowed(ip, block_private_networks=True)


@pytest.mark.unit
@pytest.mark.parametrize(
    "target", ["http://127.0.0.1:8080/admin", "http://[::1]/", "http://0.0.0.0:9000/"]
)
def test_a_url_on_the_host_itself_is_refused(target: str) -> None:
    with pytest.raises(SsrfBlockedError):
        assert_target_allowed(target, block_private_networks=True)


@pytest.mark.integration
async def test_saving_a_loopback_check_follows_the_setting(db: AsyncSession) -> None:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    data = CheckCreate(
        display_name="local-admin",
        check_type="http",
        target="http://127.0.0.1:8080/",
        interval_seconds=60,
        timeout_seconds=5,
    )
    await SettingsCoreService.ensure_default_settings(db)

    await SettingsCoreService.update_setting(
        db, "security.block_private_networks", False
    )
    assert (await CheckCoreService.create_check(db, agent.id, data)).id is not None

    await SettingsCoreService.update_setting(
        db, "security.block_private_networks", True
    )
    with pytest.raises(CheckTargetBlockedError):
        await CheckCoreService.create_check(db, agent.id, data)


# ---------------------------------------------------------------------------
# The setting's description.
# ---------------------------------------------------------------------------

_REVISION = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "005_private_networks_description.py"
)


def _revision() -> ModuleType:
    spec = importlib.util.spec_from_file_location("private_networks_desc", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _description(db: AsyncSession) -> str | None:
    db.expire_all()
    return (
        await db.execute(
            select(Setting.description).where(
                Setting.key == "security.block_private_networks"
            )
        )
    ).scalar_one()


async def _set_description(db: AsyncSession, text: str) -> None:
    setting = (
        await db.execute(
            select(Setting).where(Setting.key == "security.block_private_networks")
        )
    ).scalar_one()
    setting.description = text
    await db.flush()


@pytest.mark.integration
async def test_a_newly_seeded_setting_names_every_range(db: AsyncSession) -> None:
    await SettingsCoreService.ensure_default_settings(db)

    description = await _description(db)

    assert description is not None
    assert all(term in description for term in ("RFC 1918", "loopback", "CGNAT"))


@pytest.mark.integration
async def test_the_migration_rewrites_the_old_seeded_description(
    db: AsyncSession,
) -> None:
    revision = _revision()
    await SettingsCoreService.ensure_default_settings(db)
    await _set_description(db, revision.OLD)

    await db.run_sync(
        lambda s: revision.rewrite(s.connection(), revision.OLD, revision.NEW)
    )
    assert await _description(db) == revision.NEW

    await db.run_sync(
        lambda s: revision.rewrite(s.connection(), revision.NEW, revision.OLD)
    )
    assert await _description(db) == revision.OLD


@pytest.mark.integration
async def test_the_migration_leaves_an_edited_description_alone(
    db: AsyncSession,
) -> None:
    revision = _revision()
    await SettingsCoreService.ensure_default_settings(db)
    await _set_description(db, "Our own words")

    await db.run_sync(
        lambda s: revision.rewrite(s.connection(), revision.OLD, revision.NEW)
    )

    assert await _description(db) == "Our own words"
