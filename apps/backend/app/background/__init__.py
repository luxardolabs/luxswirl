"""
Background tasks module.

Contains all long-running background tasks for the server.
"""

from app.background.database_maintenance import (
    start_database_maintenance_task,
    stop_database_maintenance_task,
)
from app.background.job_purge import start_job_purge_task, stop_job_purge_task
from app.background.session_cleanup import (
    start_session_cleanup_task,
    stop_session_cleanup_task,
)

__all__ = [
    "start_job_purge_task",
    "stop_job_purge_task",
    "start_session_cleanup_task",
    "stop_session_cleanup_task",
    "start_database_maintenance_task",
    "stop_database_maintenance_task",
]
