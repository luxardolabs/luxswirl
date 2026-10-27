"""Agent registration keys on the admin API: an admin's alone.

A key's plaintext is in the answer that creates it and nowhere else; only its
hash is stored. A revoked key registers no agent, and its revocation stands
as first recorded.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import bcrypt
import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app
from app.models.registration_key_model import RegistrationKey
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.api_token_core_service import ApiTokenCoreService
from fixtures.factories import make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

KEYS = "/api/v1/admin/registration-keys"


@pytest_asyncio.fixture
async def client(monkeypatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    limiter.reset()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    ) as c:
        yield c
    limiter.reset()


async def _as(
    db: AsyncSession, role: str, username: str | None = None
) -> dict[str, str]:
    user = make_user(role=role, **({"username": username} if username else {}))
    db.add(user)
    await db.flush()
    _, plaintext = await ApiTokenCoreService.create_token(
        db, user.id, ApiTokenCreate(name=role, scopes=["*"])
    )
    return {"Authorization": f"Bearer {plaintext}"}


async def _create(client: AsyncClient, admin: dict[str, str], name: str) -> dict:
    resp = await client.post(KEYS, json={"name": name}, headers=admin)
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.parametrize("role", ["viewer", "editor"])
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "", None),
        ("GET", "/{id}", None),
        ("POST", "", {"name": "x"}),
        ("POST", "/{id}/revoke", None),
    ],
)
async def test_only_an_admin_reaches_registration_keys(
    db: AsyncSession,
    client: AsyncClient,
    role: str,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    admin = await _as(db, "admin")
    other = await _as(db, role)
    key = await _create(client, admin, "guarded")

    resp = await client.request(
        method, KEYS + path.format(id=key["id"]), json=body, headers=other
    )

    assert resp.status_code == 403, resp.text
    after = (await client.get(f"{KEYS}/{key['id']}", headers=admin)).json()
    assert after["status"] == "active"


async def test_no_token_is_refused(client: AsyncClient) -> None:
    assert (await client.get(KEYS)).status_code == 401


async def test_a_key_is_shown_once_and_only_its_hash_stored(
    db: AsyncSession, client: AsyncClient
) -> None:
    admin = await _as(db, "admin", username=f"ops-{uuid4().hex[:6]}")

    created = await _create(client, admin, "prod agents")

    plaintext = created["key"]
    assert created["status"] == "active" and created["usage_count"] == 0
    assert created["created_by"].startswith("ops-")
    read = await client.get(f"{KEYS}/{created['id']}", headers=admin)
    listed = await client.get(KEYS, headers=admin)
    assert plaintext not in read.text and plaintext not in listed.text
    assert "key" not in read.json() and "key_hash" not in read.json()
    stored = await db.scalar(
        select(RegistrationKey.key_hash).where(RegistrationKey.id == created["id"])
    )
    assert stored is not None and plaintext not in stored
    assert bcrypt.checkpw(plaintext.encode(), stored.encode())


async def test_a_new_key_registers_an_agent(
    db: AsyncSession, client: AsyncClient
) -> None:
    admin = await _as(db, "admin")
    plaintext = (await _create(client, admin, "fresh"))["key"]

    resp = await client.post(
        "/api/v1/agent/register",
        json={"hostname": "new-host"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )

    assert resp.status_code == 201, resp.text


async def test_a_revoked_key_registers_nothing_and_stays_as_revoked(
    db: AsyncSession, client: AsyncClient
) -> None:
    admin = await _as(db, "admin", username=f"sec-{uuid4().hex[:6]}")
    created = await _create(client, admin, "leaked")

    revoked = await client.post(
        f"{KEYS}/{created['id']}/revoke", json={"reason": "leaked"}, headers=admin
    )
    again = await client.post(
        f"{KEYS}/{created['id']}/revoke", json={"reason": "other"}, headers=admin
    )
    register = await client.post(
        "/api/v1/agent/register",
        json={"hostname": "intruder"},
        headers={"Authorization": f"Bearer {created['key']}"},
    )

    assert revoked.status_code == 200, revoked.text
    body = revoked.json()
    assert (body["status"], body["revoked_reason"]) == ("revoked", "leaked")
    assert body["revoked_by"].startswith("sec-")
    assert again.status_code == 422, again.text
    kept = (await client.get(f"{KEYS}/{created['id']}", headers=admin)).json()
    assert (kept["revoked_reason"], kept["revoked_at"]) == (
        "leaked",
        body["revoked_at"],
    )
    assert register.status_code == 401, register.text


async def test_the_list_leaves_revoked_keys_out_unless_asked(
    db: AsyncSession, client: AsyncClient
) -> None:
    admin = await _as(db, "admin")
    live = await _create(client, admin, "live")
    gone = await _create(client, admin, "gone")
    await client.post(f"{KEYS}/{gone['id']}/revoke", headers=admin)

    active = await client.get(KEYS, params={"per_page": 200}, headers=admin)
    every = await client.get(
        KEYS, params={"per_page": 200, "include_revoked": True}, headers=admin
    )

    active_ids = {k["id"] for k in active.json()["items"]}
    every_ids = {k["id"] for k in every.json()["items"]}
    assert live["id"] in active_ids and gone["id"] not in active_ids
    assert {live["id"], gone["id"]} <= every_ids


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"name": ""},
        {"name": "x", "key_hash": "y"},
        {"name": "x", "description": "d" * 1001},
    ],
)
async def test_a_bad_create_is_refused(
    db: AsyncSession, client: AsyncClient, body: dict[str, object]
) -> None:
    admin = await _as(db, "admin")

    resp = await client.post(KEYS, json=body, headers=admin)

    assert resp.status_code == 422, resp.text


async def test_an_unknown_key_is_404(db: AsyncSession, client: AsyncClient) -> None:
    admin = await _as(db, "admin")
    missing = uuid4()

    read = await client.get(f"{KEYS}/{missing}", headers=admin)
    revoke = await client.post(f"{KEYS}/{missing}/revoke", headers=admin)

    assert (read.status_code, revoke.status_code) == (404, 404)


# ── Text a key's name, notes and revocation reason may hold ──────────────────


@pytest.mark.parametrize(
    "body",
    [
        {"name": "nul\x00byte"},
        {"name": "evil\nline"},
        {"name": "bidi‮abc"},
        {"name": "esc\x1b[31mred"},
        {"name": "x", "description": "notes\x00here"},
    ],
)
async def test_control_characters_are_refused_not_a_server_error(
    db: AsyncSession, client: AsyncClient, body: dict[str, object]
) -> None:
    admin = await _as(db, "admin")

    resp = await client.post(KEYS, json=body, headers=admin)

    assert resp.status_code == 422, resp.text
    assert "$2b$" not in resp.text


async def test_a_description_may_span_lines(
    db: AsyncSession, client: AsyncClient
) -> None:
    admin = await _as(db, "admin")

    resp = await client.post(
        KEYS, json={"name": "lines", "description": "one\ntwo\tthree"}, headers=admin
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["description"] == "one\ntwo\tthree"


async def test_a_revoke_reason_with_a_nul_is_refused(
    db: AsyncSession, client: AsyncClient
) -> None:
    admin = await _as(db, "admin")
    created = await _create(client, admin, "nul-reason")

    resp = await client.post(
        f"{KEYS}/{created['id']}/revoke", json={"reason": "bad\x00"}, headers=admin
    )

    assert resp.status_code == 422, resp.text
    kept = (await client.get(f"{KEYS}/{created['id']}", headers=admin)).json()
    assert kept["status"] == "active"
