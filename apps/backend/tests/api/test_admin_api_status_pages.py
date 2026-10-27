"""Status pages on the admin API: any token reads, an editor changes.

A page's items name only checks and agents that exist, and are replaced
whole. A page is deleted by a maintenance job that re-judges its owner when
it runs. A taken slug is a 409, by either path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.background.maintenance_handlers import status_page_delete
from app.core.config import settings
from app.core.exceptions import DuplicateResourceException
from app.core.rate_limit import limiter
from app.main import app
from app.models.maintenance_job_model import MaintenanceJob
from app.models.status_page_model import StatusPage
from app.models.user_model import User
from app.schemas.api_token_schema import ApiTokenCreate
from app.schemas.status_page_schema import StatusPageCreate, StatusPageUpdate
from app.services.core.api_token_core_service import ApiTokenCoreService
from app.services.core.status_page_core_service import StatusPageCoreService
from fixtures.factories import make_agent, make_check, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

PAGES = "/api/v1/admin/status-pages"


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    limiter.reset()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    ) as c:
        yield c
    limiter.reset()


async def _user(db: AsyncSession, role: str) -> tuple[UUID, dict[str, str]]:
    user = make_user(role=role)
    db.add(user)
    await db.flush()
    _, plaintext = await ApiTokenCoreService.create_token(
        db, user.id, ApiTokenCreate(name=role)
    )
    return user.id, {"Authorization": f"Bearer {plaintext}"}


async def _as(db: AsyncSession, role: str) -> dict[str, str]:
    return (await _user(db, role))[1]


async def _check(db: AsyncSession) -> tuple[UUID, UUID]:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(agent_id=agent.id)
    db.add(check)
    await db.flush()
    return agent.id, check.id


def _slug() -> str:
    return f"sp-{uuid4().hex[:8]}"


async def _create(
    client: AsyncClient, editor: dict[str, str], **fields: object
) -> dict:
    body = {"name": "Production", "slug": _slug(), **fields}
    resp = await client.post(PAGES, json=body, headers=editor)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _pages(db: AsyncSession, page_id: str) -> int:
    db.expire_all()
    return int(
        await db.scalar(
            select(func.count())
            .select_from(StatusPage)
            .where(StatusPage.id == UUID(page_id))
        )
        or 0
    )


# ── Who may ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "", {"name": "x", "slug": "viewer-made"}),
        ("PATCH", "/{id}", {"name": "renamed"}),
        ("PUT", "/{id}/items", {"items": []}),
        ("DELETE", "/{id}", None),
    ],
)
async def test_a_viewer_changes_no_status_page(
    db: AsyncSession,
    client: AsyncClient,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    editor = await _as(db, "editor")
    viewer = await _as(db, "viewer")
    page = await _create(client, editor, name="kept")

    resp = await client.request(
        method, PAGES + path.format(id=page["id"]), json=body, headers=viewer
    )

    assert resp.status_code == 403, resp.text
    after = (await client.get(f"{PAGES}/{page['id']}", headers=viewer)).json()
    assert after["name"] == "kept"
    assert (
        int(await db.scalar(select(func.count()).select_from(MaintenanceJob)) or 0) == 0
    )


async def test_no_token_is_refused(client: AsyncClient) -> None:
    assert (await client.get(PAGES)).status_code == 401


# ── Reads ───────────────────────────────────────────────────────────────────


async def test_a_viewer_lists_and_reads_pages(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    viewer = await _as(db, "viewer")
    public = await _create(client, editor, name="Public", is_public=True)
    private = await _create(client, editor, name="Private")

    listed = await client.get(PAGES, headers=viewer)
    only_public = await client.get(PAGES, params={"is_public": True}, headers=viewer)
    unfiltered = await client.get(PAGES, params={"is_public": ""}, headers=viewer)
    read = await client.get(f"{PAGES}/{private['id']}", headers=viewer)

    assert listed.status_code == 200
    assert {public["id"], private["id"]} <= {p["id"] for p in listed.json()["items"]}
    assert [p["id"] for p in only_public.json()["items"]] == [public["id"]]
    assert unfiltered.json()["total"] == listed.json()["total"]
    assert read.json()["is_public"] is False


async def test_an_unknown_page_is_404(db: AsyncSession, client: AsyncClient) -> None:
    viewer = await _as(db, "viewer")

    assert (await client.get(f"{PAGES}/{uuid4()}", headers=viewer)).status_code == 404


# ── Create and change ───────────────────────────────────────────────────────


async def test_a_new_page_is_private_with_its_items_in_order(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    agent_id, check_id = await _check(db)
    _, other_id = await _check(db)

    page = await _create(
        client,
        editor,
        slug="Prod_Status",
        status_bar_minutes=240,
        items=[
            {"type": "check", "check_id": str(check_id)},
            {"type": "group", "name": "Listed", "checks": [str(other_id)]},
            {
                "type": "group",
                "name": "Filtered",
                "sort_by": "latency",
                "filter": {"agent_id": str(agent_id), "tags": ["prod"]},
            },
        ],
    )

    assert page["is_public"] is False
    assert page["slug"] == "prod_status"
    assert page["status_bar_minutes"] == 240
    assert page["items"] == [
        {"type": "check", "check_id": str(check_id), "order": 0},
        {
            "type": "group",
            "name": "Listed",
            "order": 1,
            "collapsed": False,
            "sort_by": "manual",
            "sort_direction": "asc",
            "checks": [str(other_id)],
        },
        {
            "type": "group",
            "name": "Filtered",
            "order": 2,
            "collapsed": False,
            "sort_by": "latency",
            "sort_direction": "asc",
            "filter": {"agent_id": str(agent_id), "tags": ["prod"]},
        },
    ]


@pytest.mark.parametrize(
    "item",
    [
        lambda missing: {"type": "check", "check_id": missing},
        lambda missing: {"type": "group", "name": "g", "checks": [missing]},
        lambda missing: {"type": "group", "name": "g", "filter": {"agent_id": missing}},
    ],
    ids=["check", "listed-group", "filter-agent"],
)
async def test_an_item_naming_nothing_is_refused(
    db: AsyncSession, client: AsyncClient, item: Callable[[str], dict[str, object]]
) -> None:
    editor = await _as(db, "editor")
    missing = str(uuid4())

    resp = await client.post(
        PAGES,
        json={"name": "x", "slug": _slug(), "items": [item(missing)]},
        headers=editor,
    )

    assert resp.status_code == 422, resp.text
    assert missing in resp.text
    assert int(await db.scalar(select(func.count()).select_from(StatusPage)) or 0) == 0


@pytest.mark.parametrize(
    "body",
    [
        {"name": "x", "slug": "has space"},
        {"name": "x", "slug": "a/b"},
        {"name": "x", "slug": "ok", "status_bar_minutes": 45},
        {"name": "x", "slug": "ok", "config": {}},
        {
            "name": "x",
            "slug": "ok",
            "items": [{"type": "group", "name": "both", "checks": [], "filter": {}}],
        },
        {"name": "x", "slug": "ok", "items": [{"type": "group", "name": "neither"}]},
        {"name": "x", "slug": "ok", "items": [{"type": "panel"}]},
        {
            "name": "x",
            "slug": "ok",
            "items": [{"type": "group", "name": "g", "filter": {"status": "down"}}],
        },
    ],
)
async def test_a_page_the_api_cannot_hold_is_refused(
    db: AsyncSession, client: AsyncClient, body: dict[str, object]
) -> None:
    editor = await _as(db, "editor")

    resp = await client.post(PAGES, json=body, headers=editor)

    assert resp.status_code == 422, resp.text


async def test_a_change_sets_what_it_sends_and_keeps_the_rest(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    _, check_id = await _check(db)
    page = await _create(
        client,
        editor,
        description="notes",
        items=[{"type": "check", "check_id": str(check_id)}],
    )
    stored = await db.get(StatusPage, UUID(page["id"]))
    assert stored is not None
    stored.config = {**(stored.config or {}), "theme": "dark"}
    await db.flush()

    resp = await client.patch(
        f"{PAGES}/{page['id']}",
        json={"description": None, "status_bar_minutes": 60, "is_public": True},
        headers=editor,
    )

    assert resp.status_code == 200, resp.text
    changed = resp.json()
    assert changed["description"] is None
    assert changed["status_bar_minutes"] == 60 and changed["is_public"] is True
    assert changed["name"] == "Production" and changed["items"] == page["items"]
    db.expire_all()
    stored = await db.get(StatusPage, UUID(page["id"]))
    assert stored is not None and stored.config == {
        "theme": "dark",
        "status_bar_minutes": 60,
    }


@pytest.mark.parametrize("field", ["name", "slug", "is_public", "status_bar_minutes"])
async def test_a_required_field_is_not_cleared(
    db: AsyncSession, client: AsyncClient, field: str
) -> None:
    editor = await _as(db, "editor")
    page = await _create(client, editor)

    resp = await client.patch(
        f"{PAGES}/{page['id']}", json={field: None}, headers=editor
    )

    assert resp.status_code == 422, resp.text


async def test_items_are_replaced_whole(db: AsyncSession, client: AsyncClient) -> None:
    editor = await _as(db, "editor")
    _, first = await _check(db)
    _, second = await _check(db)
    page = await _create(
        client, editor, items=[{"type": "check", "check_id": str(first)}]
    )

    resp = await client.put(
        f"{PAGES}/{page['id']}/items",
        json={"items": [{"type": "check", "check_id": str(second)}]},
        headers=editor,
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == [
        {"type": "check", "check_id": str(second), "order": 0}
    ]


async def test_items_for_an_unknown_page_are_404(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")

    resp = await client.put(
        f"{PAGES}/{uuid4()}/items", json={"items": []}, headers=editor
    )

    assert resp.status_code == 404


async def test_a_page_naming_too_many_checks_is_refused_before_any_query(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    # 33,000 ids would pass the bind-parameter limit of one IN (...) lookup.
    groups = [
        {
            "type": "group",
            "name": f"g{i}",
            "checks": [str(uuid4()) for _ in range(1000)],
        }
        for i in range(33)
    ]

    created = await client.post(
        PAGES, json={"name": "x", "slug": _slug(), "items": groups}, headers=editor
    )

    assert created.status_code == 422, created.text[:300]
    assert "at most 10000 checks" in created.text


# ── Slugs ───────────────────────────────────────────────────────────────────


async def test_a_taken_slug_is_a_409(db: AsyncSession, client: AsyncClient) -> None:
    editor = await _as(db, "editor")
    first = await _create(client, editor)
    second = await _create(client, editor)

    created = await client.post(
        PAGES, json={"name": "dup", "slug": first["slug"]}, headers=editor
    )
    renamed = await client.patch(
        f"{PAGES}/{second['id']}", json={"slug": first["slug"].upper()}, headers=editor
    )

    assert created.status_code == 409, created.text
    assert renamed.status_code == 409, renamed.text


async def test_the_web_path_also_gives_a_taken_slug_its_conflict(
    db: AsyncSession,
) -> None:
    slug = _slug()
    await StatusPageCoreService.create_status_page(
        db, StatusPageCreate(name="a", slug=slug)
    )
    other = await StatusPageCoreService.create_status_page(
        db, StatusPageCreate(name="b", slug=_slug())
    )

    with pytest.raises(DuplicateResourceException):
        await StatusPageCoreService.create_status_page(
            db, StatusPageCreate(name="c", slug=slug)
        )
    with pytest.raises(DuplicateResourceException):
        await StatusPageCoreService.update_status_page(
            db, other.id, StatusPageUpdate(slug=slug)
        )


async def test_a_slug_lost_to_a_race_leaves_the_session_usable(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken = _slug()
    await StatusPageCoreService.create_status_page(
        db, StatusPageCreate(name="a", slug=taken)
    )
    other = await StatusPageCoreService.create_status_page(
        db, StatusPageCreate(name="b", slug=_slug())
    )

    async def _not_seen(_db: AsyncSession, _slug: str) -> None:
        return None  # the other request commits between the check and the write

    monkeypatch.setattr(
        StatusPageCoreService, "get_status_page_by_slug", staticmethod(_not_seen)
    )
    with pytest.raises(DuplicateResourceException):
        await StatusPageCoreService.update_status_page(
            db, other.id, StatusPageUpdate(slug=taken)
        )

    assert int(await db.scalar(select(func.count()).select_from(StatusPage)) or 0) >= 2


# ── Delete ──────────────────────────────────────────────────────────────────


async def test_an_editor_deletes_a_page_by_job(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    page = await _create(client, editor)

    queued = await client.delete(f"{PAGES}/{page['id']}", headers=editor)

    assert queued.status_code == 202, queued.text
    assert queued.json()["kind"] == "status_page_delete"
    job = await db.get(MaintenanceJob, UUID(queued.json()["id"]))
    assert job is not None
    await status_page_delete.handle(db, job)
    assert await _pages(db, page["id"]) == 0


async def test_a_delete_whose_owner_was_demoted_changes_nothing(
    db: AsyncSession, client: AsyncClient
) -> None:
    owner_id, editor = await _user(db, "editor")
    page = await _create(client, editor)
    queued = await client.delete(f"{PAGES}/{page['id']}", headers=editor)
    await db.execute(update(User).where(User.id == owner_id).values(role="viewer"))

    job = await db.get(MaintenanceJob, UUID(queued.json()["id"]))
    assert job is not None
    with pytest.raises(Exception, match="no longer an active editor or admin"):
        await status_page_delete.handle(db, job)

    assert await _pages(db, page["id"]) == 1


async def test_a_delete_of_an_unknown_page_is_404(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")

    assert (
        await client.delete(f"{PAGES}/{uuid4()}", headers=editor)
    ).status_code == 404
