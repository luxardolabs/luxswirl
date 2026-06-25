#!/usr/bin/env python3
"""
Cleanup script to remove ALL internal checks (like luxswirl_agent_health).

CONTEXT:
Internal checks were a legacy hack where agent health metrics were sent
as fake "checks" mixed with real check results. This has been replaced
with a proper heartbeat system (/api/v1/heartbeat endpoint).

This script:
1. Finds all checks with check_type="internal"
2. Deletes ALL of them (they should not exist anymore)
3. Agent health is now tracked via Agent model + AgentMetric hypertable

Run from the project root:
    cd /mnt/luxardolabs/swirl/1.0/9
    python cleanup_duplicate_internal_checks.py
"""

import asyncio
import sys
from pathlib import Path

# Add src/app to path
sys.path.insert(0, str(Path(__file__).parent / "src" / "app"))

from collections import defaultdict

from sqlalchemy import select

from app.db import get_session_maker
from app.models.check_model import Check


async def cleanup_internal_checks(dry_run: bool = True):
    """
    Remove ALL internal checks (they should not exist anymore).

    Args:
        dry_run: If True, only show what would be deleted without actually deleting
    """
    async with get_session_maker()() as db:
        # Fetch all internal checks
        result = await db.execute(
            select(Check)
            .where(Check.check_type == "internal")
            .order_by(Check.agent_id, Check.display_name, Check.created_at)
        )
        internal_checks = result.scalars().all()

        print(f"\n🔍 Found {len(internal_checks)} internal checks to remove\n")

        if len(internal_checks) == 0:
            print("✅ No internal checks found - system is clean!")
            return

        # Group by agent for better display
        grouped = defaultdict(list)
        for check in internal_checks:
            grouped[check.agent_id].append(check)

        # Display and delete
        for agent_id, checks in grouped.items():
            print(f"📋 Agent {agent_id}:")
            for check in checks:
                print(
                    f"   ❌ Removing: {check.display_name} (id={check.id}, created={check.created_at})"
                )
                if not dry_run:
                    await db.delete(check)

        if not dry_run:
            await db.commit()
            print(f"\n✨ Cleanup complete! Deleted {len(internal_checks)} internal checks")
        else:
            print("\n🔍 DRY RUN - No changes made")
            print(f"📊 Would delete {len(internal_checks)} internal checks")
            print("\n💡 To actually delete these checks, run with: --no-dry-run")
            print("\n💡 Agent health is now tracked via:")
            print("    - Agent model fields (uptime, cpu, memory, etc.)")
            print("    - AgentMetric hypertable (time-series history)")
            print("    - /api/v1/heartbeat endpoint")


async def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Cleanup internal checks (no longer needed - replaced with heartbeat)"
    )
    parser.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Actually delete internal checks (default is dry-run only)",
    )

    args = parser.parse_args()
    dry_run = not args.no_dry_run

    if dry_run:
        print("=" * 70)
        print("🔍 DRY RUN MODE - No changes will be made")
        print("=" * 70)
    else:
        print("=" * 70)
        print("⚠️  DELETE MODE - Internal checks will be removed!")
        print("=" * 70)

    await cleanup_internal_checks(dry_run=dry_run)


if __name__ == "__main__":
    asyncio.run(main())
