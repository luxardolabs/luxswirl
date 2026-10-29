"""A check's health in SQL: its current result, and the predicate on it.

Statement builders shared by the check and check-result queries. Health follows
app.core.liveness: a check is up or down by its newest result within its silence
threshold, and unknown with none (it never reported, or has gone silent).
"""

from sqlalchemy import BigInteger, DateTime, cast, desc, func, literal, select
from sqlalchemy.sql import ColumnElement
from sqlalchemy.sql.selectable import LateralFromClause

from app.core.datetime_utils import utc_now
from app.core.liveness import (
    DEFAULT_CHECK_INTERVAL_SECONDS,
    DEFAULT_MISSED_INTERVALS,
    GRACE_SECONDS,
)
from app.models.check_model import Check
from app.models.check_result_model import CheckResult
from app.models.enum_model import CheckHealthStatus


def silent_after_seconds() -> ColumnElement[int]:
    """`app.core.liveness.silent_after(Check.interval_seconds)`, in SQL: an
    interval of 0, like none, reads as the default, as `interval or default` does,
    and the arithmetic is in bigint, so no stored interval overflows it."""
    return (
        cast(
            func.coalesce(
                func.nullif(Check.interval_seconds, 0), DEFAULT_CHECK_INTERVAL_SECONDS
            ),
            BigInteger,
        )
        * DEFAULT_MISSED_INTERVALS
        + GRACE_SECONDS
    )


def current_result() -> LateralFromClause:
    """Each check's current result: its newest within its silence threshold, one
    index probe per check, joined on `true()`. A check with none is silent, and
    its health unknown."""
    return (
        select(CheckResult.success, CheckResult.latency_ms, CheckResult.timestamp)
        .where(
            CheckResult.check_id == Check.id,
            CheckResult.timestamp
            >= literal(utc_now(), DateTime(timezone=True))
            - func.make_interval(0, 0, 0, 0, 0, 0, silent_after_seconds()),
        )
        .order_by(desc(CheckResult.timestamp))
        .limit(1)
        .lateral("current_result")
    )


def health_condition(
    current: LateralFromClause, status: CheckHealthStatus
) -> ColumnElement[bool]:
    """The checks of health `status`, on an outer join of `current_result()`."""
    if status == CheckHealthStatus.UP:
        return current.c.success.is_(True)
    if status == CheckHealthStatus.DOWN:
        return current.c.success.is_(False)
    return current.c.success.is_(None)
