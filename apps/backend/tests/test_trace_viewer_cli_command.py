"""The trace viewer's Copy CLI Command gives a shell the artifact's filename as one argument.

The filename comes from whoever holds an agent key, and an operator pastes the
command into a shell. Each command here is read from the served page and run by a
real shell, with `playwright` defined to print the arguments it was given.
"""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from html.parser import HTMLParser
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.main import app
from app.models.check_artifact_model import CheckArtifact
from app.models.enum_model import CheckArtifactType
from app.services.core.auth_core_service import AuthCoreService
from fixtures.factories import make_agent, make_check, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

_TRACE_VIEWER_JS = Path(__file__).resolve().parents[1] / "app/static/js/trace-viewer.js"


class _Config(HTMLParser):
    """The page's `#trace-config` attributes, decoded as a browser decodes them."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.attrs: dict[str, str | None] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if ("id", "trace-config") in attrs:
            self.attrs = dict(attrs)


def _arguments_a_shell_gives_playwright(command: str, cwd: Path) -> list[str]:
    script = 'playwright() { printf "%s\\0" "$@"; }\n' + command
    done = subprocess.run(
        ["sh", "-c", script], capture_output=True, cwd=cwd, timeout=10, check=True
    )
    return done.stdout.decode().split("\0")[:-1]


@pytest_asyncio.fixture
async def client(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    user = make_user(role="editor")
    db.add(user)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, user.id)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as c:
        c.cookies.set(settings.security.session_cookie_name, token)
        yield c


async def _served_command(client: AsyncClient, db: AsyncSession, filename: str) -> str:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(agent_id=agent.id, check_type="synthetic")
    db.add(check)
    await db.flush()
    now = utc_now()
    artifact = CheckArtifact(
        id=uuid4(),
        check_id=check.id,
        check_result_id=uuid4(),
        check_result_timestamp=now,
        artifact_type=CheckArtifactType.TRACE,
        content_type="application/zip",
        filename=filename,
        size_bytes=2,
        data=b"PK",
        created_at=now,
        updated_at=now,
    )
    db.add(artifact)
    await db.flush()

    resp = await client.get(f"/artifacts/{artifact.id}/viewer")

    assert resp.status_code == 200, resp.text
    parser = _Config()
    parser.feed(resp.text)
    command = parser.attrs.get("data-cli-command")
    assert command, resp.text
    return command


async def test_a_plain_name_reads_as_written(
    client: AsyncClient, db: AsyncSession
) -> None:
    command = await _served_command(client, db, "trace.zip")

    assert command == "playwright show-trace trace.zip"


@pytest.mark.parametrize(
    "filename",
    [
        "My Check_trace.zip",
        "trace.zip; touch pwned #",
        "$(touch pwned).zip",
        "`touch pwned`.zip",
        "it's; touch pwned; '.zip",
        "a\nb; touch pwned",
        "trace.zip | touch pwned",
        "*.zip",
    ],
)
async def test_a_shell_gets_the_filename_as_one_argument(
    client: AsyncClient, db: AsyncSession, tmp_path: Path, filename: str
) -> None:
    command = await _served_command(client, db, filename)
    (tmp_path / "other.zip").touch()  # What `*.zip` would expand to.

    assert _arguments_a_shell_gives_playwright(command, tmp_path) == [
        "show-trace",
        filename,
    ]
    assert not (tmp_path / "pwned").exists()


async def test_a_name_that_reads_as_an_option_is_given_as_a_path(
    client: AsyncClient, db: AsyncSession, tmp_path: Path
) -> None:
    command = await _served_command(client, db, "--browser=x.zip")

    assert _arguments_a_shell_gives_playwright(command, tmp_path) == [
        "show-trace",
        "./--browser=x.zip",
    ]


def test_the_button_copies_the_servers_command() -> None:
    source = _TRACE_VIEWER_JS.read_text()

    assert "config.dataset.cliCommand" in source
    assert "show-trace" not in source
