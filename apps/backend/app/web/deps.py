"""Typed FastAPI dependency aliases for the web (HTMX/Jinja) surface.

Split from the old `app/core/auth_deps.py` by SURFACE — see `app/api/deps` for
why that split matters. These refuse by REDIRECTING: `get_current_user_web`
sends an unauthenticated visitor to `/login` (preserving the requested path),
and `require_admin_web` sends a non-admin to `/?error=admin_required`. A browser
gets a page; the API's bare variants would have handed it a JSON body.

Home per FLEET-LAYOUT-STANDARD §2: `app/web/deps` for web dependency aliases.
"""

from typing import Annotated

from fastapi import Depends

from app.core.security import (
    get_current_user_web,
    get_optional_user,
    require_admin_web,
    require_editor_web,
)
from app.schemas.auth_schema import CurrentUserDTO

CurrentUserWeb = Annotated[CurrentUserDTO, Depends(get_current_user_web)]
AdminUserWeb = Annotated[CurrentUserDTO, Depends(require_admin_web)]
EditorUserWeb = Annotated[CurrentUserDTO, Depends(require_editor_web)]
OptionalUserWeb = Annotated[CurrentUserDTO | None, Depends(get_optional_user)]
