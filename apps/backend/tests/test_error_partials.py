"""Both surfaces' error partials render the one error card.

The admin tier (settings, users, scheduler, import/export) and the rest of the
console each own an error partial, so neither surface renders the other's
markup (fw.surface_separation). Both call the `error_card` macro: this checks
that each still renders the error and its dismiss control.
"""

from __future__ import annotations

import pytest
from app.web.templates import templates


@pytest.mark.parametrize(
    "template",
    ["partials/error_message.html", "partials/settings/error_message.html"],
)
def test_error_partial_renders_the_card(template: str) -> None:
    html = templates.get_template(template).render(error="<target> unreachable")

    assert "&lt;target&gt; unreachable" in html
    assert "data-dismiss-error" in html
    assert "bg-red-900/20" in html
