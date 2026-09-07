"""Which provider DTO you hold decides whether a secret is masked.

Two readers want opposite things from the same row. The API must never echo a
stored password back to a client. The edit form must render it, because the
schema marks the field `password` and an operator saving an unrelated change
would otherwise write the mask back over the real secret.

So the seam holds NotificationProviderInDB (unmasked) and the API's
response_model re-validates it into NotificationProviderResponse (masked).
These tests pin both halves — swapping the view seam to the masked schema, or
dropping the API's response_model, breaks one of them.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.datetime_utils import utc_now
from app.schemas.notification_provider_schema import (
    NotificationProviderInDB,
    NotificationProviderResponse,
)
from app.services.core.notification_core_service import NotificationCoreService
from fixtures.factories import make_notification_provider
from sqlalchemy.ext.asyncio import AsyncSession

SECRET = "hunter2-not-a-mask"
MASK = "***MASKED***"


def _provider_row(**config):
    return make_notification_provider(
        provider_type="webhook",
        config={"url": "https://example.invalid/hook", **config},
    )


class TestTheSeamKeepsTheSecret:
    async def test_core_read_returns_the_stored_value(self, db: AsyncSession):
        provider = _provider_row(token=SECRET)
        db.add(provider)
        await db.flush()

        dto = await NotificationCoreService.get_provider(db, provider.id)

        assert dto.config["token"] == SECRET

    async def test_list_returns_stored_values(self, db: AsyncSession):
        provider = _provider_row(token=SECRET)
        db.add(provider)
        await db.flush()

        providers, _ = await NotificationCoreService.list_providers(db, limit=100)

        mine = next(p for p in providers if p.id == provider.id)
        assert mine.config["token"] == SECRET


class TestTheApiSchemaStillMasks:
    def test_masks_every_sensitive_key(self):
        for key in ("password", "api_key", "token", "secret", "api_token"):
            unmasked = NotificationProviderInDB(
                id="00000000-0000-0000-0000-000000000001",
                created_at=utc_now(),
                updated_at=utc_now(),
                provider_type="webhook",
                friendly_name="w",
                config={"url": "https://example.invalid", key: SECRET},
            )

            masked = NotificationProviderResponse.model_validate(
                unmasked, from_attributes=True
            )

            assert masked.config[key] == MASK, key

    def test_masks_header_values_not_names(self):
        unmasked = NotificationProviderInDB(
            id="00000000-0000-0000-0000-000000000002",
            created_at=utc_now(),
            updated_at=utc_now(),
            provider_type="webhook",
            friendly_name="w",
            config={
                "url": "https://example.invalid",
                "additional_headers": {"Authorization": f"Bearer {SECRET}"},
            },
        )

        masked = NotificationProviderResponse.model_validate(
            unmasked, from_attributes=True
        )

        # The name stays visible — it is the value that leaks.
        assert masked.config["additional_headers"] == {"Authorization": MASK}

    def test_an_empty_secret_is_left_alone(self):
        """Masking an empty value would render '***MASKED***' into a field the
        operator never set, and saving it would store the literal mask."""
        unmasked = NotificationProviderInDB(
            id="00000000-0000-0000-0000-000000000003",
            created_at=utc_now(),
            updated_at=utc_now(),
            provider_type="webhook",
            friendly_name="w",
            config={"url": "https://example.invalid", "token": ""},
        )

        masked = NotificationProviderResponse.model_validate(
            unmasked, from_attributes=True
        )

        assert masked.config["token"] == ""

    def test_non_sensitive_keys_survive(self):
        unmasked = NotificationProviderInDB(
            id="00000000-0000-0000-0000-000000000004",
            created_at=utc_now(),
            updated_at=utc_now(),
            provider_type="webhook",
            friendly_name="w",
            config={"url": "https://example.invalid/hook", "method": "POST"},
        )

        masked = NotificationProviderResponse.model_validate(
            unmasked, from_attributes=True
        )

        assert masked.config["url"] == "https://example.invalid/hook"
        assert masked.config["method"] == "POST"
