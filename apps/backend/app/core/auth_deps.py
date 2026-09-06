"""Typed FastAPI dependency aliases (LUXSWIRL-167).

These live in their own leaf module — NOT in core/security.py — on purpose.
`core/security.py` is pulled into the models-init chain
(`app.models.base_model` → `app.core.__init__` → `app.core.security`), so a *runtime*
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
from app.schemas.auth_schema import CurrentUserDTO

# The authenticated principal for layers that RECEIVE the user as a positional
# argument rather than via `Depends()` — e.g. view services, which routers hand
# `current_user` down to.
#
# This is the DTO, NOT an alias of the User ORM model. A bare `type AuthedUser = User`
# (what this used to be) only moves the import line: the ORM row still crosses the view
# seam, so the lazy-load-out-of-session risk and the DB-schema coupling are both fully
# intact while the guard can no longer see them. See `luxarch --playbook view-seam`.
type AuthedUser = CurrentUserDTO

CurrentUser = Annotated[CurrentUserDTO, Depends(get_current_user)]
AdminUser = Annotated[CurrentUserDTO, Depends(require_admin)]
CurrentUserWeb = Annotated[CurrentUserDTO, Depends(get_current_user_web)]
AdminUserWeb = Annotated[CurrentUserDTO, Depends(require_admin_web)]
EditorUserWeb = Annotated[CurrentUserDTO, Depends(require_editor_web)]
OptionalUserWeb = Annotated[CurrentUserDTO | None, Depends(get_optional_user)]
