"""The provider seam hands the edit form the stored secret, never a mask.

The edit form must render the stored value, because the schema marks the field
`password` and an operator saving an unrelated change would otherwise write a
mask back over the real secret. So the seam holds NotificationProviderInDB
(unmasked); these tests pin that.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.core.notification_core_service import NotificationCoreService
from fixtures.factories import make_notification_provider
from sqlalchemy.ext.asyncio import AsyncSession

SECRET = "hunter2-not-a-mask"


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
