"""A request body holds only text a column can store.

A JSON escape such as `"\\ud800"` decodes to a lone surrogate, text UTF-8
cannot encode, and PostgreSQL refuses a NUL. Stored in a free-form JSON field,
either makes every later read of that row fail, and a NUL in a text column
fails the write itself. So every free-form JSON field a route takes is a
StorableJson, every text field refuses both a NUL and a surrogate, and a
report's result holding one is refused on its own.
"""

from __future__ import annotations

import annotationlib
import importlib
import inspect
import json
import pkgutil
import types
import typing
from collections.abc import AsyncIterator
from typing import Any, Union, get_args, get_origin

import app.services.core as app_services_core
import pytest
import pytest_asyncio
from _route_table import iter_routes
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.rate_limit import limiter
from app.main import app
from app.models.check_result_model import CheckResult
from app.schemas.api_token_schema import ApiTokenCreate
from app.schemas.base_schema import (
    IpAddressText,
    StorableText,
    canonical_ip,
    refuse_control_chars,
    refuse_control_chars_in_notes,
    refuse_unstorable_text,
    storable_json,
)
from app.schemas.check_result_schema import CheckResultCreate
from app.schemas.status_page_schema import StatusPageCreate
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.api_token_core_service import ApiTokenCoreService
from app.services.core.status_page_core_service import StatusPageCoreService
from app.services.views.web_users_view_service import WebUsersViewService
from fastapi import params
from fastapi.routing import APIRoute
from fixtures.factories import make_agent, make_check, make_user
from httpx import ASGITransport, AsyncClient
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
)
from pydantic.fields import FieldInfo
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

SURROGATE = "a\ud800"
NUL = "a\x00b"


def _members(annotation: Any) -> list[Any]:
    """The types a field annotation can hold, with Optional and unions opened."""
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        return [m for arg in get_args(annotation) for m in _members(arg)]
    return [annotation]


# What, on a text field, refuses both a NUL and a lone surrogate. A string
# constraint, a pattern or stripping refuses a surrogate but takes a NUL, so
# none of them is enough. test_each_text_guard_refuses_what_no_column_can_store
# holds each one to it.
_TEXT_VALIDATORS = {
    canonical_ip,
    refuse_control_chars,
    refuse_control_chars_in_notes,
    refuse_unstorable_text,
}


def _guards_text(meta: Any) -> bool:
    if isinstance(meta, FieldInfo):
        return any(_guards_text(m) for m in meta.metadata)
    return getattr(meta, "func", None) in _TEXT_VALIDATORS


def _walk(
    annotation: Any,
    guarded: bool,
    where: str,
    found: list[str],
    pending: list[type[BaseModel]],
) -> None:
    """Record each place under `annotation` that takes what no column can store."""
    if isinstance(annotation, typing.TypeAliasType):
        annotation = annotation.__value__
    origin = get_origin(annotation)
    if origin is typing.Annotated:
        base, *meta = get_args(annotation)
        if any(getattr(m, "func", None) is storable_json for m in meta):
            return
        guarded = guarded or any(_guards_text(m) for m in meta)
        _walk(base, guarded, where, found, pending)
    elif annotation is str:
        if not guarded:
            found.append(f"{where}: text taking a NUL or a surrogate")
    elif annotation in (Any, dict, object):
        found.append(f"{where}: free-form JSON")
    elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
        pending.append(annotation)
    elif origin is not typing.Literal:
        for arg in get_args(annotation):
            _walk(arg, guarded, where, found, pending)


# A body field taken raw because a service validates each of its items on its
# own, against the model named here: a report's results, so that one refused
# result is counted and the rest are stored.
ITEMS_VALIDATED_IN_SERVICES: dict[str, type[BaseModel]] = {
    "AgentReportSubmission.checks": CheckResultCreate,
}


def _submodules(package: types.ModuleType) -> list[types.ModuleType]:
    return [
        importlib.import_module(info.name)
        for info in pkgutil.iter_modules(package.__path__, package.__name__ + ".")
    ]


def _form_models() -> set[type[BaseModel]]:
    """The models a core service takes, whatever reached it: a web form, an MCP
    tool, an import. Each is a parameter of a core service's public method, so
    a form's input is held to the same rule as a JSON body's."""
    models: set[type[BaseModel]] = set()
    for module in _submodules(app_services_core):
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ != module.__name__:
                continue
            for name, func in inspect.getmembers(cls, inspect.isfunction):
                if name.startswith("_"):
                    continue
                # A name imported for type checking alone (an ORM model) stays
                # a forward reference: no request schema is imported that way.
                hints = typing.get_type_hints(
                    func, format=annotationlib.Format.FORWARDREF
                )
                hints.pop("return", None)
                for hint in hints.values():
                    models.update(
                        m
                        for m in _members(hint)
                        if isinstance(m, type) and issubclass(m, BaseModel)
                    )
    return models


def _body_models() -> set[type[BaseModel]]:
    """The models a JSON request body is validated against."""
    models: set[type[BaseModel]] = set(ITEMS_VALIDATED_IN_SERVICES.values())
    for route, _ in iter_routes(app.routes):
        if (
            isinstance(route, APIRoute)
            and route.body_field is not None
            and not isinstance(route.body_field.field_info, params.Form)
        ):
            models.update(
                m
                for m in _members(route.body_field.field_info.annotation)
                if isinstance(m, type) and issubclass(m, BaseModel)
            )
    return models


def _unguarded() -> list[str]:
    found: list[str] = []
    seen: set[type[BaseModel]] = set()
    pending = list(_body_models() | _form_models())
    while pending:
        model = pending.pop()
        if model in seen:
            continue
        seen.add(model)
        for name, field in model.model_fields.items():
            where = f"{model.__name__}.{name}"
            if where in ITEMS_VALIDATED_IN_SERVICES:
                continue
            _walk(field.rebuild_annotation(), False, where, found, pending)
    return sorted(set(found))


def test_the_walk_sees_the_request_bodies() -> None:
    assert len(_body_models()) >= 30


def test_the_walk_sees_the_form_models() -> None:
    names = {m.__name__ for m in _form_models()}

    assert {"UserCreate", "UserUpdate", "CheckCreate", "AlertCreate"} <= names


def test_every_request_body_field_refuses_what_no_column_can_store() -> None:
    unguarded = _unguarded()
    assert unguarded == [], "\n".join(unguarded)


@pytest.mark.parametrize("bad", [SURROGATE, NUL])
@pytest.mark.parametrize(
    "annotation",
    [
        typing.Annotated[str, AfterValidator(refuse_control_chars)],
        typing.Annotated[str, AfterValidator(refuse_control_chars_in_notes)],
        StorableText,
        typing.Annotated[StorableText | None, Field(max_length=10)],
        IpAddressText,
    ],
)
def test_each_text_guard_refuses_what_no_column_can_store(
    annotation: Any, bad: str
) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(annotation).validate_python(bad)


@pytest.mark.parametrize(
    "annotation",
    [
        typing.Annotated[str, Field(max_length=10)],
        typing.Annotated[str, Field(pattern=r"^.*$")],
        typing.Annotated[str, StringConstraints(strip_whitespace=True)],
    ],
)
def test_a_string_constraint_alone_takes_a_nul(annotation: Any) -> None:
    """Why the walk does not count a constraint as a guard."""
    assert TypeAdapter(annotation).validate_python(NUL) == NUL


def test_storable_text_keeps_what_a_column_can_store() -> None:
    text = "café 😀\n\ttab"

    assert TypeAdapter(StorableText).validate_python(text) == text


def test_a_nul_in_a_body_model_is_refused() -> None:
    """The walk's verdict holds at runtime: a str field it passes refuses a NUL."""

    class Body(BaseModel):
        model_config = ConfigDict(str_strip_whitespace=True)
        text: StorableText

    with pytest.raises(ValidationError):
        Body(text=NUL)


@pytest.mark.parametrize(
    "value",
    [{"x": SURROGATE}, {"x": [SURROGATE]}, {"x": {"y": SURROGATE}}, {SURROGATE: 1}],
)
def test_a_surrogate_at_any_depth_is_refused(value: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        storable_json(value)


def test_a_nul_is_refused() -> None:
    with pytest.raises(ValueError):
        storable_json({"x": "a\x00b"})


def test_storable_json_passes_through() -> None:
    value = {"x": ["é", {"y": 1.5}], "z": None}

    assert storable_json(value) == value


@pytest_asyncio.fixture
async def client(db: AsyncSession, monkeypatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    limiter.reset()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    ) as c:
        yield c
    limiter.reset()


def _result(check_id: str, metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "display_name": "web",
        "check_type": "http",
        "target": "https://example.com",
        "timestamp": utc_now().isoformat(),
        "success": True,
        "metrics": metrics,
    }


@pytest.mark.integration
@pytest.mark.parametrize("bad", [SURROGATE, NUL])
async def test_a_result_with_unstorable_metrics_is_refused_on_its_own(
    client: AsyncClient, db: AsyncSession, bad: str
) -> None:
    agent = make_agent(approval_status="pending")
    db.add(agent)
    await db.flush()
    _, key = await AgentCoreService.approve_agent(db, agent.id, actor_is_admin=True)
    check = make_check(agent_id=agent.id)
    db.add(check)
    await db.flush()

    report = {
        "agent_id": str(agent.id),
        "timestamp": utc_now().isoformat(),
        "checks": [
            _result(str(check.id), {"x": bad}),
            _result(str(check.id), {"x": "ok"}),
        ],
    }
    resp = await client.post(
        "/api/v1/agent/reports",
        # ASCII escapes: the JSON a client sends for text UTF-8 cannot encode.
        content=json.dumps(report).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )

    assert resp.status_code == 202, resp.text
    assert resp.json()["results_failed"] == 1
    stored = (
        (
            await db.execute(
                select(CheckResult.metrics).where(CheckResult.check_id == check.id)
            )
        )
        .scalars()
        .all()
    )
    assert [json.loads(m)["x"] for m in stored if m] == ["ok"]

    user = make_user(role="viewer")
    db.add(user)
    await db.flush()
    _, token = await ApiTokenCoreService.create_token(
        db, user.id, ApiTokenCreate(name="reader", scopes=["*"])
    )
    read = await client.get(
        f"/api/v1/admin/checks/{check.id}/results",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert read.status_code == 200, read.text


async def _admin_headers(db: AsyncSession) -> dict[str, str]:
    user = make_user(role="admin")
    db.add(user)
    await db.flush()
    _, token = await ApiTokenCoreService.create_token(
        db, user.id, ApiTokenCreate(name="admin", scopes=["*"])
    )
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@pytest.mark.integration
@pytest.mark.parametrize("name", [SURROGATE, "café 😀"])
async def test_an_import_entry_holding_a_surrogate_is_refused_before_queueing(
    client: AsyncClient, db: AsyncSession, name: str
) -> None:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    body = {
        "checks": [{"name": name, "check_type": "http", "target": "https://x.test"}]
    }

    resp = await client.post(
        f"/api/v1/admin/agents/{agent.id}/checks/import",
        content=json.dumps(body).encode(),
        headers=await _admin_headers(db),
    )

    assert resp.status_code == (422 if name == SURROGATE else 202), resp.text


@pytest.mark.integration
@pytest.mark.parametrize(
    ("case", "full_name"), [("s", SURROGATE), ("n", NUL), ("ok", "café 😀")]
)
async def test_a_user_full_name_holding_unstorable_text_is_refused(
    client: AsyncClient, db: AsyncSession, case: str, full_name: str
) -> None:
    body = {
        "username": "unstorable" + case,
        "password": "Xy7#kq9!Lm2$vB",
        "role": "viewer",
        "full_name": full_name,
    }

    resp = await client.post(
        "/api/v1/admin/users",
        content=json.dumps(body).encode(),
        headers=await _admin_headers(db),
    )

    assert resp.status_code == (201 if case == "ok" else 422), resp.text


@pytest.mark.integration
@pytest.mark.parametrize("bad", [SURROGATE, NUL])
async def test_the_user_form_refuses_unstorable_text(
    db: AsyncSession, bad: str
) -> None:
    """The web form is held to the same schema: a refusal, never a failed write."""
    user, error = await WebUsersViewService().create_user(
        db,
        username="unstorableform",
        password="Xy7#kq9!Lm2$vB",
        role="viewer",
        full_name=bad,
        is_active=True,
        must_change_password=False,
        created_by="admin",
    )

    assert user is None
    assert error is not None and error.startswith("full_name: ")
    target = make_user(role="viewer")
    db.add(target)
    await db.flush()
    with pytest.raises(ValidationError):
        await WebUsersViewService().update_user(
            db,
            target.id,
            role="viewer",
            full_name=bad,
            is_active=True,
            must_change_password=False,
        )


@pytest.mark.integration
@pytest.mark.parametrize("field", [None, "display_name", "target", "description"])
async def test_a_check_holding_a_nul_is_refused(
    client: AsyncClient, db: AsyncSession, field: str | None
) -> None:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    body = {
        "agent_id": str(agent.id),
        "display_name": "web",
        "check_type": "http",
        "target": "https://x.test",
    }
    if field is not None:
        body[field] = NUL

    resp = await client.post(
        "/api/v1/admin/checks",
        content=json.dumps(body).encode(),
        headers=await _admin_headers(db),
    )

    assert resp.status_code == (201 if field is None else 422), resp.text


@pytest.mark.integration
@pytest.mark.parametrize("bad", [SURROGATE, NUL])
async def test_a_status_page_group_holding_unstorable_text_is_refused(
    db: AsyncSession, bad: str
) -> None:
    page = await StatusPageCoreService.create_status_page(
        db, StatusPageCreate(name="page", slug="unstorable")
    )
    await StatusPageCoreService.add_group_to_page(db, page.id, "group", {"tags": []})

    with pytest.raises(ValueError):
        await StatusPageCoreService.update_group_filters(
            db, page.id, 0, {"search": bad}
        )
    with pytest.raises(ValueError):
        await StatusPageCoreService.add_group_to_page(db, page.id, bad, None)
    with pytest.raises(ValueError):
        await StatusPageCoreService.rename_group(db, page.id, 0, bad)
    with pytest.raises(ValueError):
        await StatusPageCoreService.reorder_all_items(
            db, page.id, [{"type": "group", "name": bad, "oldIndex": 0}]
        )
