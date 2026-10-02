"""
Core package - application configuration and utilities.
"""

from app.core.config import get_settings, settings
from app.core.exceptions import (
    AgentNotFoundException,
    CheckNotFoundException,
    DatabaseException,
    DuplicateResourceException,
    LuxSwirlException,
    NotFoundException,
    ValidationException,
)

# `app.core.security` is deliberately NOT re-exported here. Importing ANY
# `app.core.X` module runs this file first, so a re-export from security put
# security on the import path of `app.models.base_model` — and security needs
# the user model and the auth service, which need the models package. That cycle
# is what forced six "import here to avoid circular dependency" bodies inside
# security.py, database_maintenance.py and scheduler_init.py, plus the whole of
# the dependency-alias module's existence. Nothing imported the three re-exported names
# from `app.core` (every call site imports `app.core.security` directly), so the
# façade cost a cycle and bought nothing.

__all__ = [
    "settings",
    "get_settings",
    "LuxSwirlException",
    "NotFoundException",
    "AgentNotFoundException",
    "CheckNotFoundException",
    "DuplicateResourceException",
    "ValidationException",
    "DatabaseException",
]
