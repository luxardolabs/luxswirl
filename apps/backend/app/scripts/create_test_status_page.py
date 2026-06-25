import asyncio

from app.db.database import async_session_maker, init_db
from app.schemas.status_page_schema import StatusPageCreate
from app.services.core.status_page_core_service import StatusPageCoreService


async def create_test_status_page():
    await init_db()
    async with async_session_maker() as db:
        try:
            data = StatusPageCreate(
                name="Network Infrastructure Status",
                slug="network-status",
                description="Real-time status of our network infrastructure and services",
                is_public=True,
                config={},
                items=[
                    {"type": "check", "check_id": 2},
                    {"type": "check", "check_id": 3},
                    {"type": "group", "name": "Core Services", "checks": [4, 5, 6, 7]},
                    {
                        "type": "group",
                        "name": "UniFi Controllers",
                        "checks": [13, 14, 15],
                    },
                ],
            )
            status_page = await StatusPageCoreService.create_status_page(db, data)
            await db.commit()
            print(f"✅ Created: {status_page.name}")
            print(f"🌐 View at: https://localhost:9000/{status_page.slug}")
        except ValueError as e:
            print(f"ℹ️  {e}")
            print("🌐 View at: https://localhost:9000/network-status")


asyncio.run(create_test_status_page())
