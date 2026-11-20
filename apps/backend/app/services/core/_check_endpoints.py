"""A check's endpoints vetted against the network protection settings before a
create or an update writes them (SSRF)."""

import asyncio
from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.check_target_validator import validate_check_target
from app.models.check_model import Check
from app.models.enum_model import DATABASE_CHECK_TYPES
from app.services.core.settings_core_service import SettingsCoreService


async def vet_check_endpoints(
    db: AsyncSession,
    check_type: str,
    target: str | None,
    nameserver: str | None,
    connection_string: str | None,
    *,
    kept_connection_string: str | None = None,
) -> None:
    """Validate every address a check connects to against the SSRF settings.

    A check reaches its target, and a DNS check also its nameserver and a
    database check its connection string; each is validated. An empty one
    is skipped. A database check's driver reads its connection string, or
    its target when it has none. An update judging only the target names the
    connection string the check keeps in `kept_connection_string`. The
    lookups run in a thread, and an address naming more than
    `shared.ssrf.MAX_TARGET_HOSTS` hosts is refused before any.

    Raises:
        CheckTargetBlockedError: If any of them is blocked by SSRF protection
    """
    protection = await SettingsCoreService.get_network_protection(db)
    driver_check = check_type in DATABASE_CHECK_TYPES
    for endpoint, driver_reads in (
        (
            target,
            driver_check and not (connection_string or kept_connection_string),
        ),
        (nameserver, False),
        (connection_string, driver_check),
    ):
        if endpoint:
            # Its lookups block, one host after another: off the event loop.
            await asyncio.to_thread(
                validate_check_target,
                endpoint,
                block_cloud_metadata=protection.block_cloud_metadata,
                block_private_networks=protection.block_private_networks,
                database=driver_reads,
            )


async def vet_changed_endpoints(
    db: AsyncSession, check: Check, update_data: Mapping[str, Any]
) -> None:
    """Vet every address `update_data` changes on `check`, and any whose reading
    a change of type or a connection string coming or going decides. One left
    as it is stays out of it, so a stored address is no reason to refuse
    another edit.

    Raises:
        CheckTargetBlockedError: If any of them is blocked by SSRF protection
    """
    check_type = update_data.get("check_type", check.check_type)
    target = update_data.get("target", check.target)
    connection_string = update_data.get("connection_string", check.connection_string)
    type_changes = check_type != check.check_type
    await vet_check_endpoints(
        db,
        check_type,
        (
            target
            if type_changes
            or target != check.target
            or bool(connection_string) != bool(check.connection_string)
            else None
        ),
        update_data.get("nameserver"),
        (
            connection_string
            if type_changes or connection_string != check.connection_string
            else None
        ),
        kept_connection_string=connection_string,
    )
