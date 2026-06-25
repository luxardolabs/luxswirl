"""Maintenance job handlers — register them with the worker at startup."""

from app.background.maintenance_handlers.agent_delete import handle as _agent_delete
from app.background.maintenance_handlers.bulk_check_create import handle as _bulk_check_create
from app.background.maintenance_handlers.bulk_check_delete import handle as _bulk_check_delete
from app.background.maintenance_handlers.bulk_check_import import handle as _bulk_check_import
from app.background.maintenance_handlers.bulk_check_modify import handle as _bulk_check_modify
from app.background.maintenance_handlers.bulk_check_toggle import handle as _bulk_check_toggle
from app.background.maintenance_handlers.status_page_delete import handle as _status_page_delete
from app.background.maintenance_worker import register_handler
from app.models.enum_model import MaintenanceJobKind


def register_all() -> None:
    """Wire every known kind to its handler. Called from main.py lifespan."""
    register_handler(MaintenanceJobKind.AGENT_DELETE.value, _agent_delete)
    register_handler(MaintenanceJobKind.BULK_CHECK_CREATE.value, _bulk_check_create)
    register_handler(MaintenanceJobKind.BULK_CHECK_DELETE.value, _bulk_check_delete)
    register_handler(MaintenanceJobKind.BULK_CHECK_IMPORT.value, _bulk_check_import)
    register_handler(MaintenanceJobKind.BULK_CHECK_MODIFY.value, _bulk_check_modify)
    register_handler(MaintenanceJobKind.BULK_CHECK_TOGGLE.value, _bulk_check_toggle)
    register_handler(MaintenanceJobKind.STATUS_PAGE_DELETE.value, _status_page_delete)
