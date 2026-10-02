"""Typed FastAPI dependency aliases for the JSON API surface.

Split from the old `app/core/auth_deps.py` by SURFACE, which is the point: the
bare and `_Web` dependencies differ in how they REFUSE, and that difference is
the contract of the surface, not a style choice.

- these (API): raise `HTTPException` 401/403, so a programmatic client gets a
  status it can branch on.
- `app/web/deps` (web): raise a 307 to `/login` or `/`, so a browser lands on a
  page instead of a JSON body in an HTMX swap target.

Keeping both sets in one module is how a web page came to answer a JSON 403 and
an API route came to answer a redirect (LUXSWIRL-247). A router importing from
the wrong module now reads wrong at the import line.

Home per FLEET-LAYOUT-STANDARD §2: `app/api/deps` for API dependency aliases.
"""

from typing import Annotated

from fastapi import Depends

from app.core.security import get_current_user, require_admin
from app.schemas.auth_schema import CurrentUserDTO

CurrentUser = Annotated[CurrentUserDTO, Depends(get_current_user)]
AdminUser = Annotated[CurrentUserDTO, Depends(require_admin)]
