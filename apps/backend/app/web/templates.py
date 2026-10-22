"""The ONE shared Jinja2Templates instance.

Import `templates` from here; never construct another. A second instance gets
its own `env`, so the filters registered below and `StrictUndefined` would apply
to some renders and not others.

Named `templates.py` per FLEET-LAYOUT-STANDARD §2 (it was `templates_config.py`).
`app.web.templates` the module and `app/templates/` the directory do not
collide — the directory is a sibling of `app/web/`, not inside it.
"""

from pathlib import Path

from fastapi.templating import Jinja2Templates
from jinja2 import StrictUndefined

from app.services.views.nav_view_service import NavViewService
from app.web.template_filters import register_filters

# Single shared templates instance with all filters registered (package-relative
# so it resolves regardless of CWD).
# Fleet-canonical location: app/templates/web/, a sibling of app/static/.
# Refs inside templates stay relative to THIS root ("pages/x.html").
_TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates" / "web"
# NavViewService.nav_context puts the shared layout's links (`nav`), resolved by
# route name from the request, on every TemplateResponse: the sidebar is on every
# page, so its hrefs are built in one place in the view layer, not spelled in the
# template.
templates = Jinja2Templates(
    directory=str(_TEMPLATE_ROOT), context_processors=[NavViewService.nav_context]
)
# StrictUndefined: a missing/renamed/typo'd context variable RAISES at render
# instead of rendering an empty string, so a dropped view-model field is a loud
# 500 in dev rather than a blank spot in production (fw.jinja_strict_undefined).
templates.env.undefined = StrictUndefined
register_filters(templates.env)
