"""Web UI dependencies for common template context."""

from typing import Any

from fastapi import Depends, Request

from app.core.security import get_current_user_web
from app.models.user_model import User


async def get_base_template_context(
    request: Request,
    current_user: User = Depends(get_current_user_web),
) -> dict[str, Any]:
    """Base context for web UI template responses."""
    return {
        "request": request,
        "current_user": current_user,
    }
