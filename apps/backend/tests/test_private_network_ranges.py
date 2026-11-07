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
from app.shared.ssrf import (
    SsrfBlockedError,
    assert_ip_allowed,
    assert_target_allowed,
    extract_hosts,
)
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


@pytest.mark.unit
@pytest.mark.parametrize(
    "target",
    [
        "mysql://root:pw@/db",
        "mysql://root:pw@:3306/db",
        "mariadb://root:pw@/db",
        "postgresql://u:p@/db",
        "postgresql+asyncpg://u:p@:5432/db",
    ],
)
def test_a_database_url_naming_no_host_is_its_drivers_localhost(target: str) -> None:
    """The driver connects a hostless URL to localhost, so the guard checks that."""
    assert extract_hosts(target) == ["localhost"]
    with pytest.raises(SsrfBlockedError):
        assert_target_allowed(target, block_private_networks=True)
    assert_target_allowed(target)


@pytest.mark.unit
@pytest.mark.parametrize(
    "target",
    [
        "postgres:?host=169.254.169.254&port=80",
        " postgres:?host=169.254.169.254",
        "post\tgres://db/d",
        "postgres:/db?host=169.254.169.254",
        "postgresql:?hostaddr=169.254.169.254",
        "POSTGRES:?host=169.254.169.254",
        "postgres:?host=[::ffff:169.254.169.254%2525]&port=80",
        "postgresql://u:p@/d?host=[::ffff:169.254.169.254%2525]",
        "postgresql://u:p@/d?host=[fe80::1%25eth0]",
        "postgresql://u:p@/d?hostaddr=fe80::1%25eth0",
        "postgresql://u:p@/d?hostaddr=db.example",
        "postgresql://u:p@/d?host=169.254.169.254:x:y&port=80",
        "postgresql://a@169.254.169.254,b@192.0.2.10/db",
        "postgresql://u:s3cr@tpiece@a:5432,b:5433/db",
        "postgres://u@169.254.169.254,&port=@%2C",
        "postgresql://u:p@db%3A1/d",
        "postgresql://u:p@/d?host=::1",
        "postgresql://u:p w@db/d",
        "postgresql://u@/d?host=[1.2]",
        "postgresql://u:p@db/d?service=s",
        "mysql://u:p\x01@db/d",
        "mysql:d?x=://",
        "postgres:/d#://",
        "mysql:d",
        "mysql:",
        "mariadb:d",
        "postgres:",
        "postgres:5432",
    ],
)
def test_a_database_url_a_driver_could_read_two_ways_is_refused(target: str) -> None:
    with pytest.raises(SsrfBlockedError, match="cannot be read plainly") as refusal:
        assert_target_allowed(target, database=True)
    assert "s3cr" not in str(refusal.value)
    assert "169.254" not in str(refusal.value)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("target", "hosts"),
    [
        ("postgresql://u:p@db:5432/d", ["db"]),
        ("postgresql://u:p@a:1,b:2/d", ["a", "b"]),
        ("postgresql://u:p@[2606:4700::1111]:5432/d", ["2606:4700::1111"]),
        ("postgresql://u:p%40ss@db/d", ["db"]),
        ("postgresql://u:p:w,x@db/d", ["db"]),
        ("postgresql://u:p@/d", ["localhost"]),
        ("postgresql+asyncpg://u:p@:5432/d", ["localhost"]),
        ("postgresql://u:p@/d?host=db.example&port=1", ["db.example"]),
        ("postgresql://u:p@/d?host=[fe80::1]:5432", ["fe80::1"]),
        ("postgresql://u:p@/d?host=/var/run/postgresql", []),
        ("postgresql://%2Fvar%2Frun/d", []),
        ("postgresql://u:p@db/d?hostaddr=2606:4700::1111", ["db", "2606:4700::1111"]),
        ("postgresql://u:p@/d?hostaddr=192.0.2.10", ["localhost", "192.0.2.10"]),
        ("mysql://u:p@db:3306/d", ["db"]),
        ("mysql://u:p@ss@db:3306/d", ["db"]),
        ("mysql://u:a b@db:3306/d", ["db"]),
        ("mariadb://u:p@/d", ["localhost"]),
        ("192.0.2.10", ["192.0.2.10"]),
        ("db.example:5432", ["db.example"]),
    ],
)
def test_a_plain_database_target_names_its_hosts(target: str, hosts: list[str]) -> None:
    assert extract_hosts(target, database=True) == hosts


@pytest.mark.unit
def test_a_database_url_read_two_ways_is_refused_with_the_settings_off() -> None:
    with pytest.raises(SsrfBlockedError, match="cannot be read plainly"):
        assert_target_allowed(
            "postgres:?host=db",
            block_cloud_metadata=False,
            block_private_networks=False,
            database=True,
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "target",
    [
        "postgresql://u:p@169.254.169.254/d",
        "postgresql://u:p@192.0.2.10,169.254.169.254/d",
        "postgresql://u:p@/d?host=169.254.169.254",
        "postgresql://u:p@db/d?hostaddr=169.254.169.254",
        "postgresql://u:p@[::ffff:169.254.169.254]/d",
    ],
)
def test_a_metadata_host_in_a_plain_url_is_refused(target: str) -> None:
    with pytest.raises(SsrfBlockedError, match="blocked cloud metadata"):
        assert_target_allowed(target, database=True)


@pytest.mark.unit
@pytest.mark.parametrize(
    "target", ["db:5432", "pg.example:5432", "plainhost", "postgres:5432", "mysql:3306"]
)
def test_a_host_and_port_reads_only_as_that(target: str) -> None:
    assert extract_hosts(target) == [target.split(":")[0]]


@pytest.mark.unit
def test_a_postgres_url_on_a_unix_socket_names_no_network_host() -> None:
    assert extract_hosts("postgresql://u:p@/db?host=/var/run/postgresql") == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("target", "host"),
    [("[::1]", "::1"), ("[::1]:22", "::1"), ("::1", "::1"), ("db:5432", "db")],
)
def test_a_bare_target_names_its_host_without_brackets_or_port(
    target: str, host: str
) -> None:
    assert extract_hosts(target) == [host]


@pytest.mark.unit
@pytest.mark.parametrize("target", ["[::1]", "[::1]:22"])
def test_a_bracketed_loopback_target_is_refused(target: str) -> None:
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
