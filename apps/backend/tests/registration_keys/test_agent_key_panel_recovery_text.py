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


GENERATED_PANEL = "partials/panels/registration_keys/agent_key_generated_panel.html"


def _render_generated(*, recoverable: bool = True) -> str:
    agent = SimpleNamespace(id=uuid4(), agent_name="edge-01")
    html = templates.get_template(GENERATED_PANEL).render(
        agent=agent, plaintext_key="luxswirl_ak_shown_once", recoverable=recoverable
    )
    return re.sub(r"\s+", " ", html)


def test_the_generated_key_panel_never_says_to_set_it_as_the_registration_key() -> None:
    # LUXSWIRL_AUTH_KEY is the registration key the agent recovers its own key
    # with: an agent key put there makes that recovery fail.
    html = _render_generated()

    assert "LUXSWIRL_AUTH_KEY=luxswirl_ak_shown_once" not in html
    assert "Restart the agent to pick up the new key" not in html
    assert "that variable holds the agent's registration key" in html
    assert "fetches a key of its own" in html


def test_regenerate_says_the_agent_recovers_its_new_key_by_itself() -> None:
    html = _render()

    assert "the agent keeps its recovery secret" in html
    assert "recovers a new one by itself" in html


def test_revoke_says_the_install_must_register_again() -> None:
    html = _render()

    assert "void its recovery secret" in html
    assert "can never recover a key" in html
    assert "registers again as a new pending agent" in html
    assert "to stop the agent, disable or archive it" not in html


def test_a_key_for_an_agent_with_no_secret_never_promises_it_fetches_it() -> None:
    # After a revoke the install's secret is void: it cannot fetch the key
    # generated for it, and registers again instead (round 1, attacker I #3).
    html = _render_generated(recoverable=False)

    assert "fetches a key of its own" not in html
    assert "Agent-specific key saved and activated" not in html
    assert "no agent can use it" in html
    assert "registers again as a new pending agent" in html
