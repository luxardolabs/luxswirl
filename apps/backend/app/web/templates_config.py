"""Centralized Jinja2 templates configuration for all web routers.

Import `templates` from this module instead of creating new Jinja2Templates instances.
"""

from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.web.template_filters import register_filters

# Single shared templates instance with all filters registered (package-relative
# so it resolves regardless of CWD).
# Fleet-canonical location: app/templates/web/, a sibling of app/static/.
# Refs inside templates stay relative to THIS root ("pages/x.html").
_TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates" / "web"
templates = Jinja2Templates(directory=str(_TEMPLATE_ROOT))
register_filters(templates.env)
