"""A wrong current password on change-password counts toward the lockout.

Change-password checked the current password against the real hash, counted
no failure, ignored the account's lock and took no lock of its own. Someone
holding a stolen session cookie could guess the password without limit, and
change it on an account sign-in had locked.

The check now runs as a sign-in does: the user row is read under the sign-in
lock (the username's advisory lock, then the row FOR UPDATE, waiting at most
LOGIN_LOCK_WAIT_MS), a locked account is refused as a wrong password is, a
wrong password is counted with the sign-in's budget, and a right one resets
the count. A lock it causes suspends the user's sessions without deleting
them, as a sign-in's lock does.
"""

from __future__ import annotations

import asyncio

import pytest
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.exceptions import LoginBusyException
from app.core.rate_limit import limiter
from app.main import app
from app.models.session_model import Session
from app.models.user_model import User
from app.services.core.auth_core_service import AuthCoreService
from fixtures.factories import make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm.exc import StaleDataError

pytestmark = pytest.mark.integration

RIGHT = "TestPass123!"
NEW = "Br4nd-New!Pass-xyz"
WRONG = "Current password is incorrect"


async def _seed(worker_db, username: str, failed: int = 0, locked: bool = False):
    async with worker_db() as s:
        user = make_user(username=username, failed_login_attempts=failed)
        if locked:
            user.locked_until = utc_now().replace(year=utc_now().year + 1)
        s.add(user)
        await s.commit()
        return user.id


async def _stored(worker_db, username: str) -> User:
    async with worker_db() as s:
        return (await s.scalars(select(User).where(User.username == username))).one()


async def _change(auth: AuthCoreService, s, uid, current: str) -> str | None:
    """None when the change landed, else the refusal's message."""
    try:
        await auth.change_password(s, uid, current, NEW)
    except ValueError as exc:
        return str(exc)
    return None


async def test_a_wrong_current_password_is_counted(worker_db) -> None:
    uid = await _seed(worker_db, "cp-count", failed=1)
    auth = AuthCoreService()
    async with worker_db() as s:
        assert await _change(auth, s, uid, "wrong") == WRONG
        await s.commit()
    assert (await _stored(worker_db, "cp-count")).failed_login_attempts == 2


async def test_the_limit_locks_the_account_and_refuses_the_right_password(
    worker_db,
) -> None:
    uid = await _seed(worker_db, "cp-limit", failed=4)
    auth = AuthCoreService()
    async with worker_db() as s:
        assert await _change(auth, s, uid, "wrong") == WRONG
        await s.commit()
    locked = await _stored(worker_db, "cp-limit")
    assert locked.failed_login_attempts == 5
    assert locked.is_locked

    async with worker_db() as s:
        assert await _change(auth, s, uid, RIGHT) == WRONG
        await s.commit()
    after = await _stored(worker_db, "cp-limit")
    assert after.password_hash == locked.password_hash, "changed while locked"
    # Sign-in is refused on the lock too.
    async with worker_db() as s:
        assert await auth.authenticate_user(s, "cp-limit", RIGHT) is None


async def test_a_locked_account_is_refused_like_a_wrong_password(
    worker_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal says what a wrong password says and costs one bcrypt check
    against the fake hash, so it tells a guesser nothing a sign-in would not."""
    uid = await _seed(worker_db, "cp-locked", failed=5, locked=True)
    checked: list[str] = []
    real = AuthCoreService.verify_password

    def spy(password: str, password_hash: str) -> bool:
        checked.append(password_hash)
        return real(password, password_hash)

    monkeypatch.setattr(AuthCoreService, "verify_password", staticmethod(spy))
    auth = AuthCoreService()
    async with worker_db() as s:
        assert await _change(auth, s, uid, RIGHT) == WRONG
        await s.commit()
    assert checked == [AuthCoreService._FAKE_PASSWORD_HASH]
    stored = await _stored(worker_db, "cp-locked")
    assert stored.failed_login_attempts == 5, "a refusal on a lock was counted"
    assert AuthCoreService.verify_password(RIGHT, stored.password_hash)


async def test_a_right_password_resets_the_count(worker_db) -> None:
    uid = await _seed(worker_db, "cp-reset", failed=3)
    auth = AuthCoreService()
    async with worker_db() as s:
        assert await _change(auth, s, uid, RIGHT) is None
        await s.commit()
    user = await _stored(worker_db, "cp-reset")
    assert user.failed_login_attempts == 0
    assert user.locked_until is None
    assert AuthCoreService.verify_password(NEW, user.password_hash)


async def test_a_change_waits_for_a_sign_in_in_flight(worker_db) -> None:
    """The 5th wrong sign-in (limit 5) locks the account; a change with the
    right password sent while that lock is uncommitted waits and is refused."""
    uid = await _seed(worker_db, "cp-race", failed=4)
    auth = AuthCoreService()
    async with worker_db() as s1, worker_db() as s2:
        assert await auth.authenticate_user(s1, "cp-race", "wrong") is None
        change = asyncio.create_task(_change(auth, s2, uid, RIGHT))
        await asyncio.sleep(0.5)
        assert not change.done(), "the change was judged before the lock committed"
        await s1.commit()
        assert await change == WRONG
        await s2.commit()
    user = await _stored(worker_db, "cp-race")
    assert user.failed_login_attempts == 5
    assert AuthCoreService.verify_password(RIGHT, user.password_hash)


async def test_a_sign_in_waits_for_a_wrong_change_in_flight(worker_db) -> None:
    """Change-password guesses and sign-in guesses share one budget: a sign-in
    sent while the locking change is uncommitted waits and is refused."""
    uid = await _seed(worker_db, "cp-race2", failed=4)
    auth = AuthCoreService()
    async with worker_db() as s1, worker_db() as s2:
        assert await _change(auth, s1, uid, "wrong") == WRONG
        login = asyncio.create_task(auth.authenticate_user(s2, "cp-race2", RIGHT))
        await asyncio.sleep(0.5)
        assert not login.done()
        await s1.commit()
        assert await login is None, "a sign-in got past the limit"
        await s2.commit()
    assert (await _stored(worker_db, "cp-race2")).failed_login_attempts == 5


async def test_concurrent_wrong_changes_both_count(worker_db) -> None:
    uid = await _seed(worker_db, "cp-par", failed=0)
    auth = AuthCoreService()
    async with worker_db() as s1, worker_db() as s2:
        assert await _change(auth, s1, uid, "wrong-1") == WRONG
        second = asyncio.create_task(_change(auth, s2, uid, "wrong-2"))
        await asyncio.sleep(0.5)
        assert not second.done()
        await s1.commit()
        assert await second == WRONG
        await s2.commit()
    assert (await _stored(worker_db, "cp-par")).failed_login_attempts == 2


async def test_a_change_that_waits_too_long_is_refused_as_busy(
    worker_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(AuthCoreService, "LOGIN_LOCK_WAIT_MS", 200)
    uid = await _seed(worker_db, "cp-busy", failed=0)
    auth = AuthCoreService()
    async with worker_db() as s1, worker_db() as s2:
        default = await s2.scalar(select(func.current_setting("lock_timeout")))
        assert await auth.authenticate_user(s1, "cp-busy", "wrong") is None
        with pytest.raises(LoginBusyException) as refused:
            await auth.change_password(s2, uid, RIGHT, NEW)
        assert refused.value.status_code == 429
        assert await s2.scalar(select(func.current_setting("lock_timeout"))) == default
        await s1.commit()
        await s2.rollback()
    user = await _stored(worker_db, "cp-busy")
    assert user.failed_login_attempts == 1
    assert AuthCoreService.verify_password(RIGHT, user.password_hash)


async def _signed_in_client(worker_db, username: str, failed: int):
    uid = await _seed(worker_db, username, failed=failed)
    async with worker_db() as s:
        _, token = await AuthCoreService().create_session(s, uid)
        await s.commit()
    client = AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    )
    client.cookies.set(settings.security.session_cookie_name, token)
    return uid, client


@pytest.mark.parametrize(
    "path", ["/change-password", "/profile/change-password"], ids=["page", "profile"]
)
async def test_the_web_forms_commit_the_count_and_suspend_the_session(
    worker_db, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """Through the real request transaction: the count a refused change makes
    is committed, and the lock it reaches treats the user's sessions as a
    sign-in's lock does: suspended (the next request is sent to sign in) but
    not deleted, so they work again once the lock is lifted."""
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    limiter.reset()
    username = f"cp-web-{path.count('/')}"
    uid, client = await _signed_in_client(worker_db, username, failed=4)
    profile = app.url_path_for("profile.show")
    async with client:
        assert (await client.get(profile)).status_code == 200
        r = await client.post(
            path,
            data={
                "current_password": "wrong",
                "new_password": NEW,
                "confirm_password": NEW,
            },
        )
        assert r.status_code == 400
        user = await _stored(worker_db, username)
        assert user.failed_login_attempts == 5
        assert user.is_locked
        suspended = await client.get(profile)
        assert suspended.status_code in (302, 303, 307)
        assert suspended.headers["location"].startswith(
            app.url_path_for("auth.login_page")
        )
        async with worker_db() as s:
            sessions = await s.scalar(
                select(func.count()).select_from(Session).where(Session.user_id == uid)
            )
            assert sessions == 1
            await s.execute(
                update(User).where(User.id == uid).values(locked_until=None)
            )
            await s.commit()
        assert (await client.get(profile)).status_code == 200
    limiter.reset()


async def _request(auth: AuthCoreService, s, token: str, uid, current: str) -> str:
    """One change-password request as the app runs it: the auth dependency's
    verify_session dirties the session row, then the change, then the commit.
    Returns the outcome, or the SQLSTATE of a database error."""
    try:
        assert await auth.verify_session(s, token) is not None
        outcome = await _change(auth, s, uid, current) or "changed"
        await s.commit()
    except DBAPIError as exc:
        await s.rollback()
        return f"db-error:{getattr(exc.orig, 'sqlstate', '')}"
    except StaleDataError:
        # The guess's own session was ended by the owner's change: its
        # last-activity write finds no row. Not a lock cycle.
        await s.rollback()
        return "session-ended"
    return outcome


async def test_a_guess_from_another_session_does_not_abort_the_owners_change(
    worker_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner changes the password from session A while a stolen cookie
    (session B) sends a wrong guess. The guess's lock wait must not hold
    session B's row, which the owner's change deletes with every session:
    that cycle was a deadlock that rolled the owner's change back."""
    uid = await _seed(worker_db, "cp-dl")
    tokens = []
    for _ in range(2):
        async with worker_db() as s:
            _, token = await AuthCoreService().create_session(s, uid)
            await s.commit()
        tokens.append(token)
    real = AuthCoreService.validate_password_complexity

    async def slow(db, password: str):  # the owner holds the locks a while
        await asyncio.sleep(1.5)
        return await real(db, password)

    monkeypatch.setattr(
        AuthCoreService, "validate_password_complexity", staticmethod(slow)
    )
    auth = AuthCoreService()
    async with worker_db() as s1, worker_db() as s2:
        owner = asyncio.create_task(_request(auth, s1, tokens[0], uid, RIGHT))
        await asyncio.sleep(0.3)
        guess = asyncio.create_task(_request(auth, s2, tokens[1], uid, "wrong"))
        owner_out, guess_out = await asyncio.gather(owner, guess)
    assert owner_out == "changed", owner_out
    assert not guess_out.startswith("db-error"), guess_out
    user = await _stored(worker_db, "cp-dl")
    assert AuthCoreService.verify_password(NEW, user.password_hash)


async def test_a_user_loaded_before_the_wait_is_judged_as_committed(
    worker_db,
) -> None:
    """Over HTTP the auth dependency has already loaded the user (4 failures)
    before the change queues behind the locking sign-in: the change must judge
    the lock that sign-in commits, not the stale user in memory."""
    uid = await _seed(worker_db, "cp-stale", failed=4)
    async with worker_db() as s:
        _, token = await AuthCoreService().create_session(s, uid)
        await s.commit()
    auth = AuthCoreService()
    async with worker_db() as s1, worker_db() as s2:
        session = await auth.verify_session(s2, token)
        assert session is not None
        assert session.user.failed_login_attempts == 4
        assert await auth.authenticate_user(s1, "cp-stale", "wrong") is None
        change = asyncio.create_task(_change(auth, s2, uid, RIGHT))
        await asyncio.sleep(0.5)
        assert not change.done()
        await s1.commit()
        assert await change == WRONG
        await s2.commit()
    user = await _stored(worker_db, "cp-stale")
    assert user.failed_login_attempts == 5
    assert AuthCoreService.verify_password(RIGHT, user.password_hash)
