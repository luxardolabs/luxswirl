#!/usr/bin/env python3
"""Docker health check for the LuxSwirl Agent (`python -m app.health.check`).

Liveness is proven by the agent's own work, not by the process merely existing: the
heartbeat sender stamps a liveness file after every heartbeat the server accepts, and
this script asserts that stamp is fresh. A wedged agent — event loop blocked, reporter
deadlocked, credentials rejected — stops stamping, so it reads UNHEALTHY even though
PID 1 is still alive. That distinction is the whole point of the check.

Exit codes:
    0 - Healthy: the agent stamped its liveness file within the allowed window.
    1 - Unhealthy: the stamp is stale, unreadable, or was never written.

Configuration:
    LUXSWIRL_HEALTH_FILE     Path to the liveness stamp (default: /app/data/agent_health)
    LUXSWIRL_HEALTH_MAX_AGE  Max stamp age in seconds (default: 180 — three 60s heartbeats)
"""

import os
import sys
import time
from pathlib import Path

HEALTH_FILE = Path(os.getenv("LUXSWIRL_HEALTH_FILE", "/app/data/agent_health"))
MAX_AGE_SECONDS = int(os.getenv("LUXSWIRL_HEALTH_MAX_AGE", "180"))


def check_liveness_stamp() -> tuple[bool, str]:
    """Verify the agent's liveness stamp exists and is fresh.

    Returns:
        Tuple of (is_healthy, status_message).
    """
    if not HEALTH_FILE.exists():
        return False, (
            f"no liveness stamp at {HEALTH_FILE} — the agent has not completed a "
            f"heartbeat yet (still registering, or the server is unreachable)"
        )

    try:
        stamped_at = float(HEALTH_FILE.read_text().strip())
    except (OSError, ValueError) as exc:
        return False, f"liveness stamp at {HEALTH_FILE} is unreadable: {exc}"

    age = time.time() - stamped_at
    if age > MAX_AGE_SECONDS:
        return False, (
            f"liveness stamp is {age:.0f}s old (limit {MAX_AGE_SECONDS}s) — the agent is "
            f"running but no longer heart-beating successfully"
        )

    return True, f"agent heart-beating (last stamp {age:.0f}s ago)"


def main() -> int:
    healthy, message = check_liveness_stamp()
    print(message)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
