"""Typed FastAPI dependency aliases for the web (HTMX/Jinja) surface.

Split from the old `app/core/auth_deps.py` by SURFACE — see `app/api/deps` for
why that split matters. These refuse by REDIRECTING: `get_current_user_web`
sends an unauthenticated visitor to `/login` (preserving the requested path),
and `require_admin_web` sends a non-admin to `/?error=admin_required`. A browser
gets a page; the API's bare variants would have handed it a JSON body.

Home per FLEET-LAYOUT-STANDARD §2: `app/web/deps` for web dependency aliases.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, Path
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import (
    get_current_user_web,
    get_optional_user,
    require_admin_web,
    require_editor_web,
)
from app.db import get_db
from app.schemas.auth_schema import CurrentUserDTO
from app.services.views.status_pages_view_service import StatusPagesViewService

CurrentUserWeb = Annotated[CurrentUserDTO, Depends(get_current_user_web)]
AdminUserWeb = Annotated[CurrentUserDTO, Depends(require_admin_web)]
EditorUserWeb = Annotated[CurrentUserDTO, Depends(require_editor_web)]
OptionalUserWeb = Annotated[CurrentUserDTO | None, Depends(get_optional_user)]


async def drawn_page_is_current(
    status_page_id: Annotated[UUID, Path(description="Status page UUID")],
    _editor: EditorUserWeb,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    page_version: Annotated[datetime | None, Header(alias="X-Page-Version")] = None,
) -> None:
    """A status-page edit naming an item by its position carries the page version
    it was drawn from; one the page has moved past is refused (409) before the
    route runs. Auth resolves first, so no one but an editor learns a page's state.
    Holds the page's row lock in the request's transaction until the write commits.
    """
    await StatusPagesViewService.require_drawn_version(db, status_page_id, page_version)
