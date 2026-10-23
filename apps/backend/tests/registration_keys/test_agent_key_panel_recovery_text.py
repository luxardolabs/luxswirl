"""The agent key panel describes key recovery as the server enforces it.

Recovery needs the registration key AND the agent's own recovery secret (header
X-Agent-Recovery-Secret, kept in the agent's credentials file); a registration
key alone recovers nothing, and an agent that lost its credentials file must
register again.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.web.templates import templates

pytestmark = pytest.mark.unit

PANEL = "partials/panels/registration_keys/agent_key_management_panel.html"


def _render() -> str:
    agent = SimpleNamespace(
        id=uuid4(),
        agent_name="edge-01",
        hostname=None,
        ip_address=None,
        api_key_created_at=None,
        api_key_last_used=None,
    )
    urls = SimpleNamespace(regenerate_key="/regen", revoke_key="/revoke")
    nav = SimpleNamespace(settings_registration_keys="/settings/registration-keys")
    html = templates.get_template(PANEL).render(agent=agent, agent_urls=urls, nav=nav)
    return re.sub(r"\s+", " ", html)


def test_recovery_needs_the_registration_key_and_the_recovery_secret() -> None:
    html = _render()

    assert "Authorization: Bearer &lt;registration_key&gt;" in html
    assert "X-Agent-Recovery-Secret: &lt;recovery_secret&gt;" in html
    assert "A registration key alone recovers nothing." in html


def test_a_lost_credentials_file_means_registering_again() -> None:
    html = _render()

    assert "lost its credentials file" in html
    assert "registers again" in html
