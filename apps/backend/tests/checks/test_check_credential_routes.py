"""A credential that takes another path to a viewer than the check's target: the name a
bulk import gives a check, a bulk failure's text, and the MCP audit row."""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from urllib.parse import urlparse
from uuid import UUID

import pytest
import pytest_asyncio
from app.background.maintenance_handlers import bulk_check_create
from app.background.maintenance_worker import JobFailed
from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app
from app.models.enum_model import MaintenanceJobKind
from app.models.maintenance_job_model import MaintenanceJob
from app.models.mcp_tool_call_model import MCPToolCall
from app.schemas.api_activity_schema import ActivityFilters
from app.schemas.api_token_schema import ApiTokenCreate
from app.schemas.check_schema import BulkCheckCreateRequest
from app.services.core.api_token_core_service import ApiTokenCoreService
from app.services.core.check_core_service import CheckCoreService
from app.services.core.maintenance_job_core_service import MaintenanceJobCoreService
from app.services.core.mcp_audit_core_service import (
    ARGUMENT_LENGTH,
    MCPAuditCoreService,
    masked_arguments,
)
from fixtures.factories import make_agent, make_check, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

SECRET = "S3cretZZ9"


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    limiter.reset()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    ) as http:
        yield http


async def _viewer(db: AsyncSession) -> dict[str, str]:
    viewer = make_user(role="viewer")
    db.add(viewer)
    await db.flush()
    _, token = await ApiTokenCoreService.create_token(
        db, viewer.id, ApiTokenCreate(name="viewer", scopes=["*"])
    )
    return {"Authorization": f"Bearer {token}"}


async def _bulk_job(db: AsyncSession, url: str) -> tuple[MaintenanceJob, UUID]:
    agent = make_agent()
    owner = make_user(role="editor")
    db.add_all([agent, owner])
    await db.flush()
    job = MaintenanceJob(
        kind=MaintenanceJobKind.BULK_CHECK_CREATE,
        owner_id=owner.id,
        params={"agent_id": str(agent.id), "requests": [{"url": url}], "alert_ids": []},
    )
    db.add(job)
    await db.flush()
    return job, job.id


@pytest.mark.integration
@pytest.mark.parametrize(
    "url",
    [
        f"https://hooks.slack.com/services/T000/B000/{SECRET}",
        f"https://discord.com/api/webhooks/123/{SECRET}",
    ],
    ids=["slack", "discord"],
)
async def test_a_bulk_name_holds_no_more_than_the_masked_target(
    client: AsyncClient, db: AsyncSession, url: str
) -> None:
    job, _ = await _bulk_job(db, url)
    await bulk_check_create.handle(db, job)
    headers = await _viewer(db)

    listed = await client.get("/api/v1/admin/checks", headers=headers)
    found = await client.get(
        "/api/v1/admin/checks", params={"search": SECRET}, headers=headers
    )

    assert listed.json()["total"] == 1
    assert SECRET not in listed.text
    assert found.json()["items"] == []


def test_a_bulk_name_keeps_an_ordinary_path() -> None:
    parsed = urlparse("https://api.example.com:8443/v1/health")

    assert CheckCoreService.bulk_display_name(parsed) == (
        "api-example-com-v1-health-8443"
    )


def test_a_port_that_is_no_number_is_not_quoted() -> None:
    with pytest.raises(ValueError, match="no valid port") as refused:
        CheckCoreService.bulk_display_name(
            urlparse(f"https://db.example.com:{SECRET}/")
        )

    assert SECRET not in str(refused.value)


@pytest.mark.integration
async def test_a_bulk_failure_does_not_quote_what_a_parser_misread(
    client: AsyncClient, db: AsyncSession
) -> None:
    job, job_id = await _bulk_job(db, f"{SECRET}:x@db.example.com/app")

    with pytest.raises(JobFailed) as failed:
        await bulk_check_create.handle(db, job)
    await MaintenanceJobCoreService.update_progress(db, job_id, failed.value.progress)
    await MaintenanceJobCoreService.mark_failed(db, job_id, str(failed.value))
    await db.flush()
    db.expire_all()

    response = await client.get(
        f"/api/v1/admin/maintenance-jobs/{job_id}", headers=await _viewer(db)
    )

    assert response.status_code == 200, response.text
    assert SECRET not in response.text
    assert SECRET.lower() not in response.text
    assert "Entry 1: The URL's scheme is no check type" in response.text


@pytest.mark.integration
@pytest.mark.parametrize(
    "url",
    [
        f"https://m:{SECRET}\u2100@a.example.com/",
        f"https://[{SECRET}]/",
        f"https://m:{SECRET}@[{SECRET}]/",
    ],
    ids=["nfkc", "bracket-host", "bracket-userinfo"],
)
async def test_a_url_the_parser_refuses_is_not_quoted(
    client: AsyncClient, db: AsyncSession, url: str
) -> None:
    job, job_id = await _bulk_job(db, url)

    with pytest.raises(JobFailed) as failed:
        await bulk_check_create.handle(db, job)
    await MaintenanceJobCoreService.update_progress(db, job_id, failed.value.progress)
    await MaintenanceJobCoreService.mark_failed(db, job_id, str(failed.value))
    await db.flush()
    db.expire_all()

    response = await client.get(
        f"/api/v1/admin/maintenance-jobs/{job_id}", headers=await _viewer(db)
    )

    assert response.status_code == 200, response.text
    assert SECRET not in response.text


@pytest.mark.integration
async def test_webhooks_on_one_host_keep_apart_names_after_masking(
    db: AsyncSession,
) -> None:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    requests = [
        BulkCheckCreateRequest(url=f"https://hooks.slack.com/services/T/B/{i}{SECRET}")
        for i in range(3)
    ]

    first = await CheckCoreService.bulk_create_checks(db, agent.id, requests)
    second = await CheckCoreService.bulk_create_checks(db, agent.id, requests[:1])

    names = [r.display_name for r in [*first.results, *second.results]]
    assert first.succeeded == 3
    assert len(set(names)) == 4, names
    assert all(SECRET not in (name or "") for name in names)


@pytest.mark.integration
async def test_the_web_flow_keeps_webhooks_apart(db: AsyncSession) -> None:
    """The form posts the names its preview made, as given names."""
    agent = make_agent()
    db.add(agent)
    await db.flush()
    urls = [f"tcp://hooks.example.com:{port}/services/T0/B0/k" for port in (1, 2)]
    urls += [f"https://hooks.slack.com/services/T0/B0/k{i}" for i in range(3)]
    previews = await CheckCoreService.bulk_preview_checks(db, urls)
    names = [preview["generated_name"] for preview in previews]
    assert len(set(names)) == len(names), names

    made = await CheckCoreService.bulk_create_checks(
        db,
        agent.id,
        [
            BulkCheckCreateRequest(url=url, display_name=name)
            for url, name in [*zip(urls, names, strict=True), (urls[0], names[0])]
        ],
    )

    stored = [r.display_name for r in made.results]
    assert made.succeeded == len(urls) + 1
    assert len(set(stored)) == len(stored), stored


@pytest.mark.integration
async def test_a_preview_name_is_numbered_once_against_an_agents_checks(
    db: AsyncSession,
) -> None:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    db.add(make_check(agent_id=agent.id, display_name="h"))
    await db.flush()

    made = await CheckCoreService.bulk_create_checks(
        db,
        agent.id,
        [
            BulkCheckCreateRequest(url="tcp://h:1", display_name="h"),
            BulkCheckCreateRequest(url="tcp://h:2", display_name="h (2)"),
        ],
    )

    assert [r.display_name for r in made.results] == ["h (2)", "h (3)"]


@pytest.mark.integration
async def test_a_given_name_no_check_has_is_kept_as_it_is(db: AsyncSession) -> None:
    agent = make_agent()
    db.add(agent)
    await db.flush()

    made = await CheckCoreService.bulk_create_checks(
        db, agent.id, [BulkCheckCreateRequest(url="tcp://h:1", display_name="web (2)")]
    )

    assert made.results[0].display_name == "web (2)"


@pytest.mark.integration
async def test_a_failure_names_the_callers_entry_after_one_was_refused(
    db: AsyncSession,
) -> None:
    agent = make_agent()
    owner = make_user(role="editor")
    db.add_all([agent, owner])
    await db.flush()
    job = MaintenanceJob(
        kind=MaintenanceJobKind.BULK_CHECK_CREATE,
        owner_id=owner.id,
        params={
            "agent_id": str(agent.id),
            "requests": [
                {"url": "x" * 600},
                {"url": "tcp://93.184.216.34:80"},
                {"url": "ftp://db"},
            ],
            "alert_ids": [],
        },
    )
    db.add(job)
    await db.flush()

    job_id = job.id
    await bulk_check_create.handle(db, job)
    await db.flush()
    db.expire_all()

    stored = await db.get(MaintenanceJob, job_id)
    assert stored is not None
    assert stored.progress is not None
    errors = stored.progress["errors"]
    assert any(error.startswith("Entry 1 refused") for error in errors), errors
    assert any(error.startswith("Entry 3:") for error in errors), errors


@pytest.mark.integration
async def test_a_preview_row_that_does_not_parse_masks_its_generated_name(
    db: AsyncSession,
) -> None:
    previews = await CheckCoreService.bulk_preview_checks(
        db, [f"https://m:{SECRET}@[/"]
    )

    assert all(SECRET not in row["generated_name"] for row in previews), previews


@pytest.mark.parametrize(
    ("url", "name"),
    [
        (f"https://monitor:{SECRET}@db.example.com/", "db-example-com"),
    ],
)
def test_a_bulk_name_reads_its_host_and_port_from_the_masked_url(
    url: str, name: str
) -> None:
    assert CheckCoreService.bulk_display_name(urlparse(url)) == name


def test_the_audit_masks_a_key() -> None:
    url = f"https://m:{SECRET}@h/"

    stored = masked_arguments({url: 1, "extra": {url: 1}})

    assert SECRET not in str(stored)
    assert list(stored) == ["https://***@h/", "extra"]


def test_the_audit_keeps_keys_that_mask_alike_apart() -> None:
    stored = masked_arguments({"x://a:1@h": 1, "x://a:2@h": 2})

    assert sorted(stored) == ["x://***@h", "x://***@h (2)"]


def test_the_audit_masks_after_a_nul_is_stripped() -> None:
    stored = masked_arguments({"target": f"https://a/?pass\x00word={SECRET}"})

    assert SECRET not in str(stored)


@pytest.mark.parametrize(
    "value",
    [
        f"Pwd=one {SECRET} three" + " pad" * 16394 + ";Server=x",
        "password="
        + "a" * 65000
        + f" mysql://m:{SECRET} mid longtail"
        + "b" * 600
        + "@db/app",
    ],
    ids=["dialect-past-the-cut", "userinfo-past-the-cut"],
)
def test_a_string_too_long_to_mask_whole_is_kept_by_its_size(value: str) -> None:
    assert masked_arguments({"target": value}) == {
        "target": f"<{len(value)} characters>"
    }


def test_the_audit_leaves_an_empty_string_as_it_is() -> None:
    assert masked_arguments({"token": ""}) == {"token": ""}


def test_the_audit_cuts_a_long_argument_and_says_how_long_it_was() -> None:
    long_value = "a=" * (2 * 1024 * 1024)

    started = time.perf_counter()
    stored = masked_arguments({"extra": long_value})
    elapsed = time.perf_counter() - started

    assert elapsed < 0.5, elapsed
    assert len(str(stored["extra"])) < 2 * ARGUMENT_LENGTH
    assert f"<{len(long_value)} characters>" in str(stored["extra"])


def test_the_audit_masks_what_sits_at_the_cut() -> None:
    value = "x" * (ARGUMENT_LENGTH - 12) + f"?password={SECRET}"

    assert SECRET[:2] not in str(masked_arguments({"target": value})["target"])


@pytest.mark.parametrize("cut", range(ARGUMENT_LENGTH - 45, ARGUMENT_LENGTH + 5, 7))
def test_a_secret_straddling_the_cut_is_hidden(cut: int) -> None:
    pad = "x " * (cut // 2)
    value = f"{pad}postgresql://m:{SECRET}@db/app " + "y " * ARGUMENT_LENGTH

    assert SECRET[:4] not in str(masked_arguments({"target": value})["target"])


@pytest.mark.parametrize(
    "form",
    [
        f"monitor:{SECRET}@db",
        f"scott/{SECRET}@db",
        f"https://a.example.com/1{SECRET}{'a' * 40}",
    ],
)
def test_a_long_string_with_no_space_is_kept_by_its_size(form: str) -> None:
    value = "x" * (ARGUMENT_LENGTH - 8) + form

    assert SECRET[:4] not in str(masked_arguments({"target": value})["target"])


def test_the_size_note_survives_the_read() -> None:
    once = masked_arguments({"target": "word " * 2000})

    assert masked_arguments(once) == once
    assert "<10000 characters>" in str(once["target"])


@pytest.mark.integration
@pytest.mark.parametrize(
    "document",
    [["a"] * (1024 * 1024), ["a=" * 2048] * 1000],
    ids=["many-short", "many-long"],
)
async def test_writing_a_large_call_leaves_the_event_loop_free(
    db: AsyncSession, document: list[str]
) -> None:
    worst = 0.0
    done = asyncio.Event()

    async def watch() -> None:
        nonlocal worst
        while not done.is_set():
            before = time.perf_counter()
            await asyncio.sleep(0.01)
            worst = max(worst, time.perf_counter() - before)

    watcher = asyncio.create_task(watch())
    await MCPAuditCoreService.log_call(
        db,
        principal=None,
        tool_name="get_check",
        arguments={"extra": document},
        success=True,
        error_message=None,
        duration_ms=1,
        denial_code=None,
        ip_address=None,
        user_agent=None,
    )
    done.set()
    await watcher

    assert worst < 1.0, worst


def test_padding_cannot_erase_what_a_call_named() -> None:
    stored = masked_arguments(
        {"a": ["x " * 32768] * 3 + ["y" * 65455], "check_id": "the-id"}
    )

    assert stored["check_id"] == "the-id"


@pytest.mark.parametrize(
    ("arguments", "secret"),
    [
        ({"password": "hunter2Z"}, "hunter2Z"),
        ({"headers": {"X-Api-Key": "hunter2Z"}}, "hunter2Z"),
        ({"changes": {"connection_password": "hunter2Z"}}, "hunter2Z"),
        ({"script_code": ["page.fill('hunter2Z')"]}, "hunter2Z"),
        ({"script_code": {"body": "hunter2Z"}}, "hunter2Z"),
        ({"changes": {"script_code\x00": "hunter2Z"}}, "hunter2Z"),
    ],
    ids=str,
)
def test_the_audit_hides_what_a_key_says_is_secret_or_a_script(
    arguments: dict[str, object], secret: str
) -> None:
    assert secret not in str(masked_arguments(arguments))


def test_a_nul_key_cannot_replace_the_key_it_reads_as() -> None:
    stored = masked_arguments({"check_id": "real", "check_id\x00": "decoy"})

    assert sorted(stored.values()) == ["decoy", "real"]


def test_long_keys_that_share_a_cut_keep_their_values() -> None:
    stored = masked_arguments({"a" * 5000 + "1": "first", "a" * 5000 + "2": "second"})

    assert sorted(stored.values()) == ["first", "second"]


@pytest.mark.integration
async def test_an_old_row_error_is_masked_on_read(db: AsyncSession) -> None:
    user = make_user(role="viewer")
    db.add(user)
    await db.flush()
    db.add(
        MCPToolCall(
            user_id=user.id,
            tool_name="get_check",
            arguments={},
            success=False,
            error_message=f"failed https://a.example.com/x?client_secret={SECRET}",
            duration_ms=1,
        )
    )
    await db.flush()

    rows = await MCPAuditCoreService.list_calls(
        db, ActivityFilters(days=1, user_id=user.id), 10
    )

    assert rows[0].error_message is not None
    assert SECRET not in rows[0].error_message
    assert rows[0].arguments == {}


def test_a_lone_surrogate_cannot_lose_the_row() -> None:
    stored = masked_arguments({"check_id": "the-id", "extra": "a\ud800b"})

    assert stored["extra"] == "ab"
    json.dumps(stored).encode("utf-8")


def test_the_audit_still_stores_a_script_by_its_size() -> None:
    assert masked_arguments({"script_code": "print(1)"}) == {
        "script_code": "<8 characters>"
    }


def test_masking_a_stored_row_again_changes_nothing() -> None:
    once = masked_arguments({"target": f"https://m:{SECRET}@h/?token={SECRET}"})

    assert masked_arguments(once) == once
