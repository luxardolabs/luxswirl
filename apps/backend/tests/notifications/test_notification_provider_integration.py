"""Integration tests for NotificationCoreService provider CRUD operations.

Covers create / update / list / get / delete on NotificationProvider, plus
the config-validation gate enforced by NotificationCoreService.create_provider.
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

_tests_root = str(Path(__file__).resolve().parent.parent)
if _tests_root not in sys.path:
    sys.path.insert(0, _tests_root)

from fixtures.factories import make_notification_provider  # noqa: E402

from app.core.exceptions import NotFoundException, ValidationException  # noqa: E402
from app.schemas.notification_provider_schema import (  # noqa: E402
    NotificationProviderCreate,
    NotificationProviderResponse,
    NotificationProviderUpdate,
)
from app.services.core.notification_core_service import NotificationCoreService  # noqa: E402

pytestmark = pytest.mark.integration


def _create_data(**overrides) -> NotificationProviderCreate:
    """Webhook config — simplest valid provider."""
    defaults = {
        "provider_type": "webhook",
        "friendly_name": "ops-webhook",
        "config": {"post_url": "https://hooks.example.test/notify"},
        "is_enabled": True,
        "is_default_enabled": False,
    }
    defaults.update(overrides)
    return NotificationProviderCreate(**defaults)


# ---------------------------------------------------------------------------
# create_provider
# ---------------------------------------------------------------------------


class TestCreateProvider:
    async def test_creates_webhook_provider(self, db: AsyncSession):
        provider = await NotificationCoreService.create_provider(db, _create_data())
        assert provider.id is not None
        assert provider.provider_type == "webhook"
        assert provider.friendly_name == "ops-webhook"
        assert provider.config["post_url"] == "https://hooks.example.test/notify"
        assert provider.is_enabled is True

    def test_unknown_provider_type_raises(self):
        # provider_type is a constrained enum — an unknown type is rejected at
        # schema construction, earlier than the old service-layer check.
        with pytest.raises(ValidationError, match="Input should be"):
            _create_data(provider_type="pigeon-mail")

    async def test_invalid_webhook_config_raises(self, db: AsyncSession):
        """post_url is required; missing it must fail at create time, not at
        send time. The validation gate runs before the row is inserted."""
        with pytest.raises(ValidationException, match="Invalid provider configuration"):
            await NotificationCoreService.create_provider(
                db,
                _create_data(config={"unrelated": "value"}),
            )

    async def test_invalid_webhook_url_raises(self, db: AsyncSession):
        with pytest.raises(ValidationException, match="Invalid provider configuration"):
            await NotificationCoreService.create_provider(
                db,
                _create_data(config={"post_url": "ftp://not-http.example.test"}),
            )

    async def test_webhook_blocks_cloud_metadata_url(self, db: AsyncSession):
        """M-3 (LUXSWIRL-190): a webhook URL must not reach the cloud-metadata
        endpoint (SSRF). Rejected at create time."""
        with pytest.raises(ValidationException, match="Invalid provider configuration"):
            await NotificationCoreService.create_provider(
                db,
                _create_data(config={"post_url": "http://169.254.169.254/latest/meta-data/"}),
            )

    async def test_webhook_additional_headers_masked_in_response(self, db: AsyncSession):
        """M-2 (LUXSWIRL-190): secrets in additional_headers must not be echoed back
        in plaintext on a read; header names stay visible."""
        provider = await NotificationCoreService.create_provider(
            db,
            _create_data(
                config={
                    "post_url": "https://hooks.example.test/notify",
                    "additional_headers": {"Authorization": "Bearer super-secret", "X-Env": "prod"},
                }
            ),
        )
        resp = NotificationProviderResponse.model_validate(provider)
        headers = resp.config["additional_headers"]
        assert headers["Authorization"] == "***MASKED***"
        assert headers["X-Env"] == "***MASKED***"
        assert set(headers) == {"Authorization", "X-Env"}  # names preserved


# ---------------------------------------------------------------------------
# get / list
# ---------------------------------------------------------------------------


class TestGetProvider:
    async def test_returns_existing(self, db: AsyncSession):
        p = make_notification_provider()
        db.add(p)
        await db.flush()

        loaded = await NotificationCoreService.get_provider_by_id(db, p.id)
        assert loaded.id == p.id

    async def test_missing_raises(self, db: AsyncSession):
        with pytest.raises(NotFoundException):
            await NotificationCoreService.get_provider_by_id(db, uuid4())

    async def test_soft_deleted_excluded_by_default(self, db: AsyncSession):
        from app.core.datetime_utils import utc_now

        p = make_notification_provider()
        p.deleted_at = utc_now()
        db.add(p)
        await db.flush()

        with pytest.raises(NotFoundException):
            await NotificationCoreService.get_provider_by_id(db, p.id)

    async def test_soft_deleted_returnable_with_include_flag(self, db: AsyncSession):
        from app.core.datetime_utils import utc_now

        p = make_notification_provider()
        p.deleted_at = utc_now()
        db.add(p)
        await db.flush()

        loaded = await NotificationCoreService.get_provider_by_id(db, p.id, include_deleted=True)
        assert loaded.id == p.id


class TestListProviders:
    async def test_pagination(self, db: AsyncSession):
        for i in range(5):
            db.add(make_notification_provider(friendly_name=f"p-{i:02d}"))
        await db.flush()

        rows, total = await NotificationCoreService.list_providers(db, skip=1, limit=2)
        assert total == 5
        assert len(rows) == 2

    async def test_filter_by_provider_type(self, db: AsyncSession):
        db.add(make_notification_provider(provider_type="webhook"))
        db.add(
            make_notification_provider(
                provider_type="email",
                config={
                    "hostname": "smtp.example.test",
                    "port": 587,
                    "to_email": "a@b.c",
                    "from_email": "x@y.z",
                },
            )
        )
        db.add(make_notification_provider(provider_type="webhook"))
        await db.flush()

        rows, total = await NotificationCoreService.list_providers(db, provider_type="webhook")
        assert total == 2
        assert all(p.provider_type == "webhook" for p in rows)


# ---------------------------------------------------------------------------
# update_provider
# ---------------------------------------------------------------------------


class TestUpdateProvider:
    async def test_updates_friendly_name_and_enabled(self, db: AsyncSession):
        p = await NotificationCoreService.create_provider(db, _create_data())

        updated = await NotificationCoreService.update_provider(
            db,
            p.id,
            NotificationProviderUpdate(friendly_name="renamed", is_enabled=False),
        )
        assert updated.friendly_name == "renamed"
        assert updated.is_enabled is False

    async def test_update_validates_new_config(self, db: AsyncSession):
        """If a config update is invalid, the row must not be modified."""
        p = await NotificationCoreService.create_provider(db, _create_data())
        original_url = p.config["post_url"]

        with pytest.raises(ValidationException, match="Invalid provider configuration"):
            await NotificationCoreService.update_provider(
                db,
                p.id,
                NotificationProviderUpdate(config={"missing": "post_url"}),
            )

        # Reload and verify the original config is intact
        reloaded = await NotificationCoreService.get_provider_by_id(db, p.id)
        assert reloaded.config["post_url"] == original_url

    async def test_partial_update_preserves_other_fields(self, db: AsyncSession):
        p = await NotificationCoreService.create_provider(db, _create_data())

        updated = await NotificationCoreService.update_provider(
            db,
            p.id,
            NotificationProviderUpdate(rate_limit_count=50),
        )
        assert updated.rate_limit_count == 50
        assert updated.friendly_name == "ops-webhook"  # untouched

    async def test_missing_provider_raises(self, db: AsyncSession):
        with pytest.raises(NotFoundException):
            await NotificationCoreService.update_provider(
                db,
                uuid4(),
                NotificationProviderUpdate(friendly_name="x"),
            )


# ---------------------------------------------------------------------------
# delete_provider — soft delete by default, hard with flag
# ---------------------------------------------------------------------------


class TestDeleteProvider:
    async def test_soft_delete_sets_deleted_at(self, db: AsyncSession):
        p = await NotificationCoreService.create_provider(db, _create_data())

        await NotificationCoreService.delete_provider(db, p.id)

        # Soft-deleted: still in DB but excluded from default queries
        with pytest.raises(NotFoundException):
            await NotificationCoreService.get_provider_by_id(db, p.id)
        # Available via include_deleted
        still_there = await NotificationCoreService.get_provider_by_id(
            db, p.id, include_deleted=True
        )
        assert still_there.deleted_at is not None

    async def test_hard_delete_removes_row(self, db: AsyncSession):
        p = await NotificationCoreService.create_provider(db, _create_data())

        await NotificationCoreService.delete_provider(db, p.id, hard_delete=True)

        with pytest.raises(NotFoundException):
            await NotificationCoreService.get_provider_by_id(db, p.id, include_deleted=True)

    async def test_missing_provider_raises(self, db: AsyncSession):
        with pytest.raises(NotFoundException):
            await NotificationCoreService.delete_provider(db, uuid4())


# ---------------------------------------------------------------------------
# get_available_provider_types — returns registered providers
# ---------------------------------------------------------------------------


class TestGetAvailableProviderTypes:
    def test_includes_built_in_types(self):
        types = NotificationCoreService.get_available_provider_types()
        # registry.get_provider_info returns dicts with key 'type' (not 'provider_type')
        type_keys = {t["type"] for t in types}
        assert "webhook" in type_keys
        assert "email" in type_keys
        assert "homeassistant" in type_keys
