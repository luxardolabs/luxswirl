"""Settings, session and artifact writes survive their owner's commit.

Durability locks for moving SettingsCoreService, AuthCoreService and
ArtifactCoreService session writes into crud (`fw.no_session_mutation_outside_crud`).
Each test owns the session, calls the real service method, commits, and reads
back on a SEPARATE connection.
"""

from __future__ import annotations

import base64
from uuid import uuid4

import pytest
from app.core.datetime_utils import utc_now
from app.models.check_artifact_model import CheckArtifact
from app.models.session_model import Session
from app.models.setting_model import Setting
from app.schemas.check_artifact_schema import CheckArtifactCreate
from app.services.core.artifact_core_service import ArtifactCoreService
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.settings_core_service import SettingsCoreService
from fixtures.factories import make_agent, make_check, make_user
from sqlalchemy import delete, select

pytestmark = pytest.mark.integration


@pytest.fixture
async def setting_key(worker_db):
    key = f"test.durable_{uuid4().hex[:8]}"
    yield key
    async with worker_db() as s:
        await s.execute(delete(Setting).where(Setting.key == key))
        await s.commit()


async def _setting_value(maker, key: str):
    async with maker() as other:
        row = await other.scalar(select(Setting).where(Setting.key == key))
        return None if row is None else row.value["value"]


async def test_create_update_and_reset_setting_are_committed(
    worker_db, setting_key
) -> None:
    async with worker_db() as s:
        await SettingsCoreService.create_setting(
            s, key=setting_key, category="general", value=5, display_name="Durable"
        )
        await s.commit()
    assert await _setting_value(worker_db, setting_key) == 5

    async with worker_db() as s:
        await SettingsCoreService.update_setting(s, setting_key, 7)
        await s.commit()
    assert await _setting_value(worker_db, setting_key) == 7

    async with worker_db() as s:
        await SettingsCoreService.reset_setting(s, setting_key)
        await s.commit()
    assert await _setting_value(worker_db, setting_key) == 5


async def test_sessions_are_created_and_ended_durably(worker_db) -> None:
    user = make_user()
    async with worker_db() as s:
        s.add(user)
        await s.commit()
    auth = AuthCoreService()

    async with worker_db() as s:
        first, token = await auth.create_session(s, user.id)
        second, _ = await auth.create_session(s, user.id)
        await s.commit()

    async def _ids():
        async with worker_db() as other:
            return set(
                await other.scalars(
                    select(Session.id).where(Session.user_id == user.id)
                )
            )

    assert await _ids() == {first.id, second.id}

    async with worker_db() as s:
        assert await auth.logout(s, token) is True
        await s.commit()
    assert await _ids() == {second.id}


async def test_create_artifact_is_committed(worker_db) -> None:
    agent = make_agent()
    check = make_check(agent_id=agent.id)
    async with worker_db() as s:
        s.add(agent)
        await s.flush()
        s.add(check)
        await s.commit()

    async with worker_db() as s:
        created = await ArtifactCoreService.create_artifact(
            s,
            CheckArtifactCreate(
                check_id=check.id,
                check_result_id=uuid4(),
                check_result_timestamp=utc_now(),
                artifact_type="screenshot",
                content_type="image/png",
                filename="shot.png",
                data_base64=base64.b64encode(b"png-bytes").decode(),
            ),
        )
        await s.commit()

    async with worker_db() as other:
        stored = await other.scalar(
            select(CheckArtifact).where(CheckArtifact.id == created.id)
        )
    assert stored is not None and stored.data == b"png-bytes"
