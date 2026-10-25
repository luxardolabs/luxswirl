"""A network scan job aimed at the cloud-metadata range is refused when created.

The agent refuses it too, before it probes anything; refusing at create tells
the operator at once, and keeps the job out of the queue.
"""

from __future__ import annotations

import pytest
from app.models.enum_model import JobType
from app.schemas.job_schema import JobCreate
from app.services.core.job_core_service import JobCoreService
from fixtures.factories import make_agent
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def _create(db: AsyncSession, subnet: str):
    agent = make_agent()
    db.add(agent)
    await db.flush()
    return await JobCoreService.create_job(
        db,
        JobCreate(
            job_type=JobType.NETWORK_SCAN, agent_id=agent.id, params={"subnet": subnet}
        ),
    )


async def test_a_scan_of_the_metadata_range_is_refused(db: AsyncSession) -> None:
    with pytest.raises(ValidationError, match="cloud metadata"):
        await _create(db, "169.254.169.0/24")


@pytest.mark.parametrize("subnet", ["100.100.100.0/24", "fd00:ec2::/112"])
async def test_a_scan_reaching_a_metadata_address_outside_link_local_is_refused(
    db: AsyncSession, subnet: str
) -> None:
    with pytest.raises(ValidationError, match="cloud metadata"):
        await _create(db, subnet)


async def test_a_scan_of_a_private_lan_is_created(db: AsyncSession) -> None:
    job = await _create(db, "192.168.1.0/24")

    assert job.params["subnet"] == "192.168.1.0/24"
