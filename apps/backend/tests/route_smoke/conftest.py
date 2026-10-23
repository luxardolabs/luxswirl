"""One seeded world plus the three authenticated clients the routes need.

Every route in the app is requested from here, so `filterwarnings = error`
actually fires on the surfaces where a dependency deprecation bites — admin
pages, error handlers, exports — rather than only where a unit test happens to
reach. See `luxarch --playbook route-smoke`.

Auth goes through the REAL paths: the web client POSTs credentials to /login and
keeps the session cookie it gets back; the API and agent clients send the Bearer
tokens the app is configured with. Nothing here forges a cookie or overrides an
auth dependency — that would exercise a fiction.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.db.database import get_db
from app.main import app
from app.models.check_artifact_model import CheckArtifact
from app.models.job_model import Job
from app.models.registration_key_model import RegistrationKey
from app.models.scheduler_model import JobConfiguration
from app.models.status_page_model import StatusPage
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.settings_core_service import SettingsCoreService
from fixtures.factories import (
    make_agent,
    make_alert,
    make_check,
    make_check_result,
    make_notification_provider,
    make_user,
)
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

ADMIN_PASSWORD = "RouteSmoke123!"


async def _seed(session_maker) -> dict[str, Any]:
    """Create one of each entity the route table's path params refer to."""
    now = utc_now()
    async with session_maker() as db:
        # What startup seeds in production: without it every settings route
        # (the metrics card's toggles among them) answered 404.
        await SettingsCoreService.ensure_default_settings(db)
        admin = make_user(username="route-smoke-admin", role="admin")
        admin.password_hash = AuthCoreService.hash_password(ADMIN_PASSWORD)
        other_user = make_user(username="route-smoke-other", role="viewer")
        agent = make_agent(agent_name="route-smoke-agent")
        db.add_all([admin, other_user, agent])
        await db.flush()

        check = make_check(agent_id=agent.id, display_name="route_smoke_check")
        provider = make_notification_provider(friendly_name="route-smoke-provider")
        alert = make_alert(name="route-smoke-alert")
        db.add_all([check, provider, alert])
        await db.flush()

        result = make_check_result(check_id=check.id, agent_id=agent.id)
        db.add(result)
        await db.flush()

        artifact = CheckArtifact(
            id=uuid4(),
            created_at=now,
            updated_at=now,
            check_id=check.id,
            check_result_id=result.id,
            check_result_timestamp=result.timestamp,
            artifact_type="trace",
            content_type="application/zip",
            filename="route-smoke.zip",
            size_bytes=3,
            data=b"zip",
        )
        job = Job(
            id=uuid4(),
            job_type="network_scan",
            status="pending",
            priority=0,
            agent_id=agent.id,
        )
        status_page = StatusPage(
            name="Route Smoke",
            slug="route-smoke",
            is_public=True,
            items=[{"type": "check", "order": 0, "check_id": str(check.id)}],
        )
        key = RegistrationKey(
            name="route-smoke-key", key_hash="not-a-real-hash", usage_count=0
        )
        scheduled = JobConfiguration(
            job_key="route_smoke_job",
            function_name="noop",
            display_name="Route Smoke Job",
            # MUST be a SchedulerJobCategory member — the column is a StrEnum and
            # reading an unknown value back raises LookupError, which is how the
            # scheduler pages 500'd as soon as the sweep reached them.
            category="cleanup",
            enabled=True,
            trigger_type="interval",
            interval_seconds=3600,
            timezone="UTC",
            max_runtime_seconds=60,
            jitter_ms=0,
            retry_limit=0,
            retry_count=0,
            backoff_seconds=30,
            notify_on_failure=False,
            total_runs=0,
            failed_runs=0,
            next_run_at=now + timedelta(hours=1),
        )
        db.add_all([artifact, job, status_page, key, scheduled])
        await db.commit()

        return {
            "user_id": str(other_user.id),
            "admin_id": str(admin.id),
            "agent_id": str(agent.id),
            "agent_name": agent.agent_name,
            "check_id": str(check.id),
            "check_result_id": str(result.id),
            "artifact_id": str(artifact.id),
            "provider_id": str(provider.id),
            "alert_id": str(alert.id),
            "job_id": str(job.id),
            "job_key": scheduled.job_key,
            "key_id": str(key.id),
            "status_page_id": str(status_page.id),
            "slug": status_page.slug,
            "key": "metrics.agent_timeout_seconds",
            "item_index": "0",
            "session_id": str(uuid4()),
        }


# Tables this fixture seeds that worker_db does not already truncate. Cleaned up
# here so a committed route-smoke world cannot leak into another test's counts.
_SEEDED_TABLES = (
    "settings",
    "alert_check_mappings",
    "alert_notification_mappings",
    "alerts",
    "notification_providers",
    "status_pages",
    "registration_keys",
    "jobs",
    "users",
)


async def _truncate_seeded(session_maker) -> None:
    async with session_maker() as db:
        existing = (
            (
                await db.execute(
                    text(
                        "SELECT tablename FROM pg_tables "
                        "WHERE schemaname = 'public' AND tablename = ANY(:t)"
                    ),
                    {"t": list(_SEEDED_TABLES)},
                )
            )
            .scalars()
            .all()
        )
        if existing:
            await db.execute(
                text(f"TRUNCATE {', '.join(existing)} RESTART IDENTITY CASCADE")
            )
            await db.commit()


@pytest_asyncio.fixture
async def empty_install(worker_db, monkeypatch):
    """The app on a FIRST-RUN install: no users, no session, no API token set.

    /setup exists only for this state — with any user present it is 404,
    which is what the seeded sweep saw, so setup.html was never
    rendered by anything. This fixture is the only place that state exists.
    """
    monkeypatch.setattr(settings.security, "rate_limit_enabled", False)
    await _truncate_seeded(worker_db)

    async def _override_get_db():
        async with worker_db() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://route-smoke",
        follow_redirects=False,
    ) as client:
        yield client
    app.dependency_overrides.clear()
    await _truncate_seeded(worker_db)


@pytest_asyncio.fixture
async def smoke_world(worker_db, monkeypatch):
    """The app with a seeded DB and an admin session cookie."""
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    # The limiter is deliberately outside RATE_LIMIT_ENABLED for login; this
    # suite logs in once, so reset rather than disable it.
    monkeypatch.setattr(settings.security, "rate_limit_enabled", False)

    await _truncate_seeded(worker_db)
    ids = await _seed(worker_db)

    async def _override_get_db():
        # MUST mirror the real get_db, which commits on a clean return. Without
        # the commit this session only ever closed, so every write a route made
        # was rolled back — including the session row that /login inserts. The
        # sweep then ran UNAUTHENTICATED: 47 web GETs bounced 307 to /login,
        # which is < 500, so the old asserter counted them as covered while no
        # handler had run. v16's EXPECT_3XX net is what surfaced it.
        async with worker_db() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://route-smoke", follow_redirects=False
    ) as client:
        login = await client.post(
            "/login",
            data={"username": "route-smoke-admin", "password": ADMIN_PASSWORD},
        )
        assert login.status_code in (302, 303), (
            f"the real login route did not issue a session: {login.status_code}"
        )
        cookie = login.headers.get("set-cookie", "")
        assert "luxswirl_session=" in cookie, "login returned no session cookie"
        session_token = cookie.split("luxswirl_session=")[1].split(";")[0]
        yield {
            "client": client,
            "ids": ids,
            "web_headers": {"Cookie": f"luxswirl_session={session_token}"},
            "admin_password": ADMIN_PASSWORD,
        }
    app.dependency_overrides.clear()
    await _truncate_seeded(worker_db)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "route_smoke: requests every registered route once."
    )
