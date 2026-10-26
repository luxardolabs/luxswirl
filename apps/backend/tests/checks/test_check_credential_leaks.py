"""A check's credentials reach no one who may not hold them, by any route.

Found by attacking the viewer-masking change: the per-agent export (it took
no login at all), the public /metrics labels, alert notifications (emailed,
posted to webhooks), values the mask let through, and editor-only buttons a
viewer was still offered.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.credential_mask import mask_credentials
from app.core.rate_limit import limiter
from app.main import app
from app.services.core.alert_evaluation_core_service import AlertEvaluationCoreService
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.metrics_collector_core_service import MetricsCollectorCoreService
from fixtures.factories import make_agent, make_check, make_check_result, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

PASSWORD = "s3cret-pw"
DSN = f"postgresql://monitor:{PASSWORD}@db.internal:5432/app"
BASIC = f"https://svc:{PASSWORD}@api.example/health"


def _client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    )


async def _browser(db: AsyncSession, role: str) -> AsyncClient:
    user = make_user(role=role)
    db.add(user)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, user.id)
    client = _client()
    client.cookies.set(settings.security.session_cookie_name, token)
    return client


@pytest_asyncio.fixture
async def setup(db: AsyncSession, monkeypatch) -> AsyncIterator[dict]:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    limiter.reset()
    agent = make_agent()
    db.add(agent)
    await db.flush()
    pg = make_check(
        agent_id=agent.id, check_type="postgres", target=DSN, connection_string=DSN
    )
    http = make_check(agent_id=agent.id, check_type="http", target=BASIC)
    db.add_all([pg, http])
    await db.flush()
    clients = {
        "viewer": await _browser(db, "viewer"),
        "admin": await _browser(db, "admin"),
        "anon": _client(),
    }
    yield {"agent": agent, "pg": pg, "http": http, **clients}
    for client in clients.values():
        await client.aclose()
    limiter.reset()


@pytest.mark.parametrize("who", ["anon", "viewer"])
async def test_only_an_admin_exports_an_agents_checks(setup: dict, who: str) -> None:
    refused = await setup[who].get(f"/agents/{setup['agent'].id}/export")
    assert refused.status_code != 200
    assert PASSWORD not in refused.text


async def test_an_admins_export_round_trips_the_stored_values(setup: dict) -> None:
    exported = await setup["admin"].get(f"/agents/{setup['agent'].id}/export")
    assert exported.status_code == 200
    assert DSN in exported.text  # an import must recreate the check as it was


async def test_the_metrics_labels_carry_no_password(setup: dict) -> None:
    agent = setup["agent"]
    for check in (setup["pg"], setup["http"]):
        result = make_check_result(check_id=check.id, agent_id=agent.id)
        result.agent = agent
        MetricsCollectorCoreService.update_check_result(result, check, agent)

    scraped = await setup["anon"].get("/metrics")

    assert scraped.status_code == 200
    assert PASSWORD not in scraped.text
    assert 'target="postgresql://***@db.internal:5432/app"' in scraped.text


def test_an_alert_notification_carries_no_password(setup: dict) -> None:
    agent, pg = setup["agent"], setup["pg"]
    result = make_check_result(check_id=pg.id, agent_id=agent.id, success=False)
    result.agent = agent
    alert = SimpleNamespace(
        name="a",
        description=None,
        custom_subject=None,
        custom_message=None,
        trigger_type="status_change",
    )

    context = AlertEvaluationCoreService._build_notification_context(
        alert, pg, result, is_recovery=False
    )

    assert context.target == "postgresql://***@db.internal:5432/app"


@pytest.mark.parametrize(
    "value",
    [
        # ODBC: an unquoted value runs to the `;`, spaces included.
        "Driver=x;Server=db;Uid=u;Pwd=my s3cret-pw phrase;",
        # ODBC braced value with a `;` inside.
        "Server=db;Uid=u;Pwd={ab;s3cret-pw};",
        # libpq escaped space.
        "host=db user=u password=ab\\ s3cret-pw dbname=app",
        # libpq sslpassword in a URI with no userinfo.
        "postgresql://db/app?sslpassword=s3cret-pw",
        # A token in an HTTP check's query string.
        "https://hc.example/ping?token=s3cret-pw",
        "https://api.example/health?api_key=s3cret-pw",
        "https://api.example/health?x=1&access_token=s3cret-pw",
    ],
)
def test_the_mask_hides_every_form_of_secret(value: str) -> None:
    assert "s3cret-pw" not in (mask_credentials(value) or "")


async def test_a_viewer_is_not_offered_the_editor_only_check_actions(
    setup: dict,
) -> None:
    check_id = setup["pg"].id
    panel = (await setup["viewer"].get(f"/check/{check_id}")).text
    for path in ("edit-form", "clone-form", "toggle"):
        assert f"/checks/{check_id}/{path}" not in panel, path
