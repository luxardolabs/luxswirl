"""What a bulk enable, disable or delete reports: a count of the checks
changed and of those that were not, with an error for each refusal or failure,
adding up to the checks asked for."""

from typing import Any
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from app.services.core.maintenance_job_core_service import MaintenanceJobCoreService


def with_refused(outcome: dict[str, Any], refused: list[str]) -> dict[str, Any]:
    """`outcome` with the checks refused before it ran counted as failures."""
    return {
        "success_count": outcome["success_count"],
        "failure_count": outcome["failure_count"] + len(refused),
        "errors": [*refused, *outcome["errors"]],
    }


def bulk_failed(check_ids: list[UUID], error: SQLAlchemyError) -> dict[str, Any]:
    """A bulk result whose statement failed: every check asked for unchanged,
    its error as anyone who reads the result may read it (no SQL)."""
    return {
        "success_count": 0,
        "failure_count": len(check_ids),
        "errors": [MaintenanceJobCoreService.readable_error(str(error))],
    }


def bulk_outcome(check_ids: list[UUID], success_count: int) -> dict[str, Any]:
    """A bulk result whose counts add up to the checks asked for: an id the
    statement did not touch (no such check) is a failure, not a silence."""
    missing = len(set(check_ids)) - success_count
    return {
        "success_count": success_count,
        "failure_count": missing,
        "errors": [f"{missing} check(s) not found"] if missing else [],
    }
