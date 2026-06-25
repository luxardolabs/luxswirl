"""Centralized Jinja2 templates configuration for all web routers.

Import `templates` from this module instead of creating new Jinja2Templates instances.
"""

from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.web.template_filters import register_filters

# Single shared templates instance with all filters registered (package-relative
# so it resolves regardless of CWD).
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
register_filters(templates.env)
