"""
One-time migration script to encrypt existing check data.

Loads all checks through the ORM and re-saves them, triggering automatic encryption
of target, check_config, and connection_string_encrypted fields.
"""

import asyncio
import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import attributes

from app.models.check_model import Check

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql+asyncpg://luxswirl:luxswirl@timescaledb:5432/luxswirl"
)

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def migrate_checks():
    """Load and re-save all checks to trigger encryption."""
    async with async_session() as session:
        # Load all checks
        result = await session.execute(select(Check))
        checks = result.scalars().all()

        print(f"Found {len(checks)} checks to migrate")

        encrypted_count = 0
        for check in checks:
            # Force SQLAlchemy to detect changes by explicitly flagging fields as modified
            # This will trigger UPDATE and call the TypeDecorator's process_bind_param
            attributes.flag_modified(check, "target")
            if check.check_config is not None:
                attributes.flag_modified(check, "check_config")
            if check.connection_string_encrypted is not None:
                attributes.flag_modified(check, "connection_string_encrypted")

            encrypted_count += 1

            if encrypted_count % 10 == 0:
                print(f"Processed {encrypted_count}/{len(checks)} checks")

        # Commit all changes (triggers encryption)
        await session.commit()
        print(f"✅ Successfully encrypted {encrypted_count} checks")


if __name__ == "__main__":
    print("Starting check encryption migration...")
    asyncio.run(migrate_checks())
    print("Migration complete!")
