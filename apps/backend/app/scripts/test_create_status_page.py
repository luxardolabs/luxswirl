#!/usr/bin/env python3
"""
Quick script to create a test public status page.
"""

import asyncio
import sys

sys.path.insert(0, "src/app")

from app.db.database import async_session_maker
from app.schemas.status_page_schema import StatusPageCreate
from app.services.core.status_page_core_service import StatusPageCoreService


async def create_test_status_page():
    """Create a test public status page with some checks."""

    async with async_session_maker() as db:
        try:
            # Create status page
            data = StatusPageCreate(
                name="Network Infrastructure Status",
                slug="network-status",
                description="Real-time status of our network infrastructure and services",
                is_public=True,
                config={},
                items=[
                    # Add first few checks
                    {"type": "check", "check_id": 2},  # http_gw_tylephony_com_80
                    {"type": "check", "check_id": 3},  # http_gw_tylephony_com_443
                    # Add a group with HTTP checks
                    {
                        "type": "group",
                        "name": "Core Services",
                        "checks": [4, 5, 6, 7],  # Various HTTP checks
                    },
                    # Add another group
                    {
                        "type": "group",
                        "name": "UniFi Controllers",
                        "checks": [13, 14, 15],  # UniFi controller checks
                    },
                ],
            )

            status_page = await StatusPageCoreService.create_status_page(db, data)
            await db.commit()

            print(f"✅ Created status page: {status_page.name}")
            print(f"   Slug: {status_page.slug}")
            print(f"   Public: {status_page.is_public}")
            print(f"   Items: {len(status_page.items)}")
            print(f"\n🌐 View at: https://localhost:9000/{status_page.slug}")

        except ValueError as e:
            # Status page might already exist
            print(f"ℹ️  Status page may already exist: {e}")
            print("🌐 Try viewing at: https://localhost:9000/network-status")
        except Exception as e:
            print(f"❌ Error: {e}")
            raise


if __name__ == "__main__":
    asyncio.run(create_test_status_page())
