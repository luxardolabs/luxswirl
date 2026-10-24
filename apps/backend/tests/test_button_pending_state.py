"""A button that sends a write shows it is in flight until the server answers.

While the request is pending the button is disabled (a second click sends
nothing) and its icon gives way to a spinner. HTMX does both: it sets
`disabled` on each `hx-disabled-elt` and puts `htmx-request` on the element
the request names as its indicator (the button's own spinner), which the
`btn-busy-only` / `btn-idle-only` rules in input.css show in place of the icon.
"""

from __future__ import annotations

import re

import pytest
from app.web.templates import templates

pytestmark = pytest.mark.unit

ICON = '<span class="i">*</span>'


def _render(call: str) -> str:
    # The icon is built with {% set %}, as callers build theirs.
    source = (
        '{% from "macros/buttons.html" import icon_btn, text_btn, '
        "confirm_action_btn %}{% set icon %}" + ICON + "{% endset %}" + call
    )
    html = templates.env.from_string(source).render()
    return re.sub(r"\s+", " ", html)


@pytest.mark.parametrize(
    "call",
    [
        "{{ icon_btn(icon, hx_post='/x') }}",
        "{{ icon_btn(icon, hx_delete='/x') }}",
        "{{ text_btn(icon, 'Go', hx_post='/x') }}",
        "{{ text_btn(icon, 'Go', hx_delete='/x') }}",
        "{{ confirm_action_btn(icon_svg=icon, hx_delete='/x', hx_target='#row') }}",
    ],
)
def test_a_button_that_writes_disables_itself_and_carries_a_spinner(call: str) -> None:
    html = _render(call)

    assert 'hx-disabled-elt="this" hx-indicator="find .btn-busy-only"' in html
    assert 'class="btn-busy-only' in html
    assert re.search(r'class="btn-idle-only[^"]*">\s*' + re.escape(ICON), html)


def test_the_confirm_button_sends_its_request_from_itself() -> None:
    """htmx.ajax puts the in-flight state on its source, which is the button."""
    html = _render("{{ confirm_action_btn(icon_svg=icon, hx_post='/x') }}")

    assert "source: $el" in html


def test_a_submit_button_carries_the_spinner_its_form_shows() -> None:
    """The form sends the request, so the form's in-flight rule shows the spinner
    and holds the button; HTMX disabling it would drop it from the submission."""
    html = _render("{{ text_btn(icon, 'Save') }}")

    assert 'type="submit"' in html
    assert "hx-disabled-elt" not in html
    assert 'class="btn-busy-only' in html


@pytest.mark.parametrize(
    "call",
    [
        "{{ icon_btn(icon, hx_get='/panel') }}",
        "{{ text_btn(icon, 'Open', href='/page') }}",
        "{{ text_btn(icon, 'Copy', type='button') }}",
    ],
)
def test_a_button_that_only_reads_is_left_as_it_was(call: str) -> None:
    html = _render(call)

    assert "hx-disabled-elt" not in html
    assert "btn-busy-only" not in html


def test_a_callers_own_indicator_comes_first_and_wins() -> None:
    """The browser keeps an attribute's first value, so a caller's hx-indicator
    (extra_attrs) is written before the macro's default."""
    html = _render(
        "{{ text_btn(icon, 'Ping', type='button', hx_post='/x', "
        "extra_attrs='hx-indicator=\"#ping-spinner\"') }}"
    )

    assert html.index('hx-indicator="#ping-spinner"') < html.index(
        'hx-indicator="find .btn-busy-only"'
    )
