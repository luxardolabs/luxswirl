"""The synthetic-check gate — one gate, because it is access control.

A synthetic check runs operator-supplied Python on the agent host. The authz
check and the AST validation used to exist twice, once in `create_check` and
once in `update_check`, which is one copy too many for a rule whose whole
purpose is that the JSON API cannot walk around it.
"""

from __future__ import annotations

import pytest
from app.core.exceptions import AuthorizationException
from app.core.synthetic_guard import (
    SYNTHETIC,
    require_admin_actor,
    validate_script,
)
from app.core.synthetic_security import SyntheticSecurityError

pytestmark = pytest.mark.unit

SAFE = "async def run_check(page):\n    await page.goto('https://example.invalid')\n"
HOSTILE = "import os\nasync def run_check(page):\n    os.system('id')\n"


class TestRequireAdminActor:
    def test_a_non_admin_is_refused(self):
        with pytest.raises(AuthorizationException):
            require_admin_actor(actor_is_admin=False)

    def test_an_admin_passes(self):
        require_admin_actor(actor_is_admin=True)

    def test_the_flag_is_keyword_only(self):
        """A positional call is how a `False` ends up where a `True` was meant,
        so the signature refuses one. Asserted at runtime because the type
        checker's objection is the point being verified."""
        call = require_admin_actor
        with pytest.raises(TypeError):
            call(True)


class TestValidateScript:
    def test_a_hostile_script_raises(self):
        with pytest.raises(SyntheticSecurityError):
            validate_script(HOSTILE, action="CREATE", check_name="x")

    def test_a_safe_script_returns(self):
        validate_script(SAFE, action="CREATE", check_name="x")

    def test_the_refusal_is_audited(self, app_logs):
        with pytest.raises(SyntheticSecurityError):
            validate_script(HOSTILE, action="UPDATE", check_id="abc")
        blocked = [r for r in app_logs if "validation FAILED" in r.message]
        assert blocked, [r.message for r in app_logs]
        assert blocked[0].event == "synthetic_check_blocked"
        assert blocked[0].action == "UPDATE"
        assert blocked[0].check_id == "abc"

    def test_an_accepted_script_is_audited_too(self, app_logs):
        """The accepted one is the code that then runs on a host, so it belongs
        in the trail as much as the blocked one."""
        validate_script(SAFE, action="CREATE", check_name="probe")
        passed = [r for r in app_logs if "validation passed" in r.message]
        assert passed, [r.message for r in app_logs]
        assert passed[0].event == "synthetic_check_create"
        assert passed[0].script_length == len(SAFE)
        assert passed[0].check_name == "probe"

    def test_the_marker_matches_the_check_type_literal(self):
        assert SYNTHETIC == "synthetic"


def test_the_app_loggers_survive_a_migration(app_logs):
    """A regression guard with a wide blast radius.

    The harness migrates in-process at session start, and alembic's `env.py`
    calls `fileConfig`. That function's DEFAULT is
    `disable_existing_loggers=True`, which sets `disabled = True` on every
    logger the ini does not name — alembic.ini names only alembic's own — so a
    plain `fileConfig()` switched the application's logging off for the rest of
    the test run. Nothing failed; log assertions simply went blind.

    This asserts the state directly rather than through a log assertion,
    because a log assertion is exactly what the bug made unreliable.
    """
    import logging

    for name in (
        "luxswirl",
        "luxswirl.core.synthetic_guard",
        "luxswirl.services.check",
    ):
        assert not logging.getLogger(name).disabled, name

    validate_script(SAFE, action="CREATE", check_name="post-migration")
    assert [r for r in app_logs if "validation passed" in r.message]
