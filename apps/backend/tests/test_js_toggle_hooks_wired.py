"""Every `data-toggle-*` hook a template renders is handled by an app script.

A template marks a control with a `data-toggle-<name>` attribute, and a
delegated click handler in static/js acts on it (`hasAttribute`, a
`[data-toggle-<name>]` selector, or `dataset.toggle<Name>`). A hook no script
reads renders a control that does nothing when clicked.
"""

from __future__ import annotations

import re

import pytest
from _paths import BACKEND_ROOT

pytestmark = pytest.mark.unit

_HOOK = re.compile(r"\bdata-toggle-([a-z0-9]+(?:-[a-z0-9]+)*)\b")
# A macro's parameter default and the components page's <code> prose name a hook
# without rendering it.
_NOT_RENDERED = re.compile(r"{%-?\s*macro\b.*?%}|<code>.*?</code>", re.S)


def _rendered_hooks() -> set[str]:
    hooks: set[str] = set()
    for template in (BACKEND_ROOT / "templates").rglob("*.html"):
        hooks.update(_HOOK.findall(_NOT_RENDERED.sub("", template.read_text())))
    return hooks


def _app_scripts() -> str:
    js_root = BACKEND_ROOT / "static/js"
    return "\n".join(
        path.read_text()
        for path in sorted(js_root.rglob("*.js"))
        if "vendor" not in path.relative_to(js_root).parts
    )


def _dataset_name(hook: str) -> str:
    first, *rest = ("toggle-" + hook).split("-")
    return first + "".join(part.capitalize() for part in rest)


def test_templates_render_toggle_hooks() -> None:
    assert _rendered_hooks(), "no data-toggle-* hook found: the scan reads nothing"


@pytest.mark.parametrize("hook", sorted(_rendered_hooks()))
def test_a_rendered_toggle_hook_has_a_handler(hook: str) -> None:
    scripts = _app_scripts()

    assert (
        f"data-toggle-{hook}" in scripts or f"dataset.{_dataset_name(hook)}" in scripts
    ), f"templates render data-toggle-{hook}, and no app script handles it"
