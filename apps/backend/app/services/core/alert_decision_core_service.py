"""
Alert decisions - when each stored result's alerts are decided.

A report only stores its results and queues the key of each one an enabled
alert evaluates (`queue_report_results`), in its own transaction: it takes no
decision lock, reads no decision, writes no notification, so the agent is
answered as soon as its results are stored. The alert decision worker
(`app/background/alert_decision_worker.py`) then drives `decide_check` for
each check with queued results, one short transaction per check: its results
are decided oldest first (`AlertEvaluationCoreService.evaluate_check_result`),
each notification decided is written as a `pending` row of
`notification_logs`, and the check's cursor moves to the newest result taken,
all committed together. The delivery worker sends the rows.
"""

import zlib
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.alert_crud import AlertCRUD
from app.crud.alert_decision_crud import AlertDecisionCRUD
from app.crud.check_crud import CheckCRUD
from app.crud.check_result_crud import CheckResultCRUD
from app.services.core.alert_evaluation_core_service import AlertEvaluationCoreService
from app.shared.logger import get_logger

logger = get_logger("luxswirl.services.alert_decision")


class AlertDecisionCoreService:
    """Queues stored results for their alert decisions and decides them."""

    @staticmethod
    def decision_lock_key(check_id: UUID) -> int:
        """The advisory lock key a check's alert decisions are taken under
        (the check id's CRC-32, as the signed 32-bit integer the lock takes)."""
        return zlib.crc32(check_id.bytes) - 2**31

    @staticmethod
    async def queue_report_results(
        db: AsyncSession, result_keys: Sequence[tuple[UUID, datetime]]
    ) -> int:
        """Queue the stored results an enabled alert evaluates, in the
        report's transaction; how many were queued.

        A check no alert evaluates queues nothing, and its results are never
        decided: an alert attached to it later decides the results stored
        after. A result already stored (a report sent again after its answer
        was lost) is queued again, and is decided only if it is newer than
        the check's cursor.
        """
        alerted = await AlertCRUD.checks_with_result_alerts(
            db, {check_id for check_id, _ in result_keys}
        )
        keys = [key for key in result_keys if key[0] in alerted]
        await AlertDecisionCRUD.enqueue(db, keys)
        return len(keys)

    @staticmethod
    async def checks_awaiting_decision(db: AsyncSession, limit: int) -> list[UUID]:
        """Up to `limit` checks with results queued, the longest waiting first."""
        return await AlertDecisionCRUD.checks_with_queued(db, limit)

    @staticmethod
    async def decide_check(db: AsyncSession, check_id: UUID, limit: int) -> int:
        """Decide the alerts of a check's oldest `limit` queued results, in
        the caller's transaction, which the caller commits.

        The check's decision lock is tried, never waited for: a check another
        transaction is deciding is left queued for the next pass. The check
        is locked FOR KEY SHARE (the order every writer of checks takes), and
        a check deleted meanwhile has nothing left to decide. Each queued
        result newer than the check's cursor is evaluated in its own
        savepoint, oldest first, against the decisions committed before it; a
        result no newer is stale (a newer one was decided first) and is
        dropped. The queue entries taken are removed and the cursor moved to
        the newest of them, with the notification rows written.

        A check no enabled alert evaluates any longer (its alerts disabled,
        deleted or taken off it since its results were queued) has its whole
        queue dropped undecided and its cursor moved past it, so its queued
        rows never outlast the next pass: an alert that evaluates it again
        decides the results stored after.

        Returns:
            How many queued results were taken (0 when the lock was not had).
        """
        if not await AlertDecisionCRUD.try_lock_check(
            db, AlertDecisionCoreService.decision_lock_key(check_id)
        ):
            return 0
        if not await CheckCRUD.key_share_existing(db, [check_id]):
            return 0
        if not await AlertCRUD.checks_with_result_alerts(db, {check_id}):
            return await AlertDecisionCoreService._drop_queue(db, check_id)
        queued = await AlertDecisionCRUD.queued_for_check(db, check_id, limit)
        if not queued:
            return 0
        cursor = await AlertDecisionCRUD.get_cursor(db, check_id)
        fresh = [stamp for stamp in queued if cursor is None or stamp > cursor]
        results = await CheckResultCRUD.list_for_check_at(db, check_id, fresh)
        evaluated = 0
        for check_result in results:
            # swallowed-exceptions: one result's failed evaluation is logged with its id and rolled back to its savepoint; the check's later results must still be decided, and the queue must move past it
            try:
                async with db.begin_nested():
                    await AlertEvaluationCoreService.evaluate_check_result(
                        db, check_result
                    )
                evaluated += 1
            except Exception:
                logger.exception(
                    "Error evaluating alerts for check result",
                    extra={"check_result_id": str(check_result.id)},
                )
        await AlertDecisionCRUD.advance(db, check_id, max(queued))
        if len(fresh) < len(queued):
            logger.info(
                "Stale results skipped: a newer result of the check was decided first",
                extra={"check_id": str(check_id), "stale": len(queued) - len(fresh)},
            )
        if evaluated:
            logger.debug(
                "Decided alerts for check results",
                extra={"check_id": str(check_id), "alerts_evaluated": evaluated},
            )
        return len(queued)

    @staticmethod
    async def _drop_queue(db: AsyncSession, check_id: UUID) -> int:
        """Drop every queued result of a check undecided, its cursor moved to
        the newest; how many were dropped."""
        newest = await AlertDecisionCRUD.newest_queued(db, check_id)
        if newest is None:
            return 0
        dropped = await AlertDecisionCRUD.advance(db, check_id, newest)
        logger.info(
            "Queued results dropped: no enabled alert evaluates the check",
            extra={"check_id": str(check_id), "dropped": dropped},
        )
        return dropped
