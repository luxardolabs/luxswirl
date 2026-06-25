"""Typed FastAPI dependency aliases (LUXSWIRL-167).

These live in their own leaf module — NOT in core/security.py — on purpose.
`core/security.py` is pulled into the models-init chain
(`app.models.base` → `app.core.__init__` → `app.core.security`), so a *runtime*
`User` import there closes a circular import
(`security → user_model → base`) that only surfaces on the alembic startup path
(not on `import app.main`). This module is imported only by routers, after the
models package is fully initialized, so importing `User` here is safe.

Routers annotate a handler param with these instead of importing the User ORM
model and wiring `Depends()` by hand. The `_Web` variants redirect on auth
failure; the bare ones raise JSON 401/403 for the API.
"""

from typing import Annotated

from fastapi import Depends

from app.core.security import (
    get_current_user,
    get_current_user_web,
    get_optional_user,
    require_admin,
    require_admin_web,
    require_editor_web,
)
from app.models.user_model import User

CurrentUser = Annotated[User, Depends(get_current_user)]
AdminUser = Annotated[User, Depends(require_admin)]
CurrentUserWeb = Annotated[User, Depends(get_current_user_web)]
AdminUserWeb = Annotated[User, Depends(require_admin_web)]
EditorUserWeb = Annotated[User, Depends(require_editor_web)]
OptionalUserWeb = Annotated[User | None, Depends(get_optional_user)]
