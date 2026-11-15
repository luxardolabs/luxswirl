"""Alert decision worker - decides the alerts of the results reports queued,
off every request (`AlertDecisionCoreService`).

Single asyncio task launched in main.py lifespan, beside the notification
delivery worker. Each pass lists the checks with queued results, the longest
waiting first, and decides each one's oldest queued results in a short
transaction of its own: its notifications are written as `pending` rows and
its cursor moved, committed together. A check whose decision lock another
worker holds (several server processes each run one) is left for the next
pass. Nothing is lost to a crash: a transaction that does not commit leaves
its results queued.
"""

import asyncio
import contextlib
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import worker_session
from app.services.core.alert_decision_core_service import AlertDecisionCoreService
from app.shared.logger import get_logger

logger = get_logger("luxswirl.background.alert_decision_worker")

# Opens one transaction: committed on a clean exit, rolled back on an error.
SessionOpener = Callable[[], AbstractAsyncContextManager[AsyncSession]]

_worker_task: asyncio.Task[None] | None = None
# Checks listed per pass.
CHECKS_PER_PASS = 100
# Queued results of one check decided per transaction: a backlog is decided
# over several short transactions, never one long one.
RESULTS_PER_DECISION = 200
_POLL_IDLE_SECONDS = 1.0
_POLL_ERROR_BACKOFF_SECONDS = 5.0


async def decide_queued_alerts(open_session: SessionOpener = worker_session) -> int:
    """One pass: decide the oldest queued results of each listed check.

    Args:
        open_session: Opens each of the pass's transactions.

    Returns:
        How many queued results were taken.
    """
    async with open_session() as db:
        check_ids = await AlertDecisionCoreService.checks_awaiting_decision(
            db, CHECKS_PER_PASS
        )
    taken = 0
    for check_id in check_ids:
        # swallowed-exceptions: one check whose decision transaction fails (rolled back, its results still queued) is logged with its id; the pass goes on to the other checks, and the next pass tries it again
        try:
            async with open_session() as db:
                taken += await AlertDecisionCoreService.decide_check(
                    db, check_id, RESULTS_PER_DECISION
                )
        except Exception:
            logger.exception(
                "Alert decisions for a check failed", extra={"check_id": str(check_id)}
            )
    return taken


async def _alert_decision_loop() -> None:
    """Main worker loop. Runs until cancelled."""
    logger.info("Alert decision worker started")
    while True:
        try:
            if not await decide_queued_alerts():
                await asyncio.sleep(_POLL_IDLE_SECONDS)
        except asyncio.CancelledError:
            logger.info("Alert decision worker cancelled")
            break
        except Exception:
            logger.exception("Alert decision pass crashed")
            await asyncio.sleep(_POLL_ERROR_BACKOFF_SECONDS)


def start_alert_decision_worker() -> None:
    """Start the loop (once)."""
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        logger.warning("Alert decision worker already running")
        return
    _worker_task = asyncio.create_task(
        _alert_decision_loop(), name="alert_decision_worker"
    )


async def stop_alert_decision_worker() -> None:
    """Stop the loop. A decision cut off here is rolled back, its results
    still queued for this server once restarted, or for another."""
    global _worker_task
    if _worker_task is None:
        return
    _worker_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _worker_task
    _worker_task = None
    logger.info("Alert decision worker stopped")
