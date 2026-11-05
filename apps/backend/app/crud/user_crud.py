"""
User CRUD - database queries for users.
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import ColumnElement, case, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.datetime_utils import utc_now
from app.models.enum_model import UserRole
from app.models.user_model import User

# The first key of the two-key advisory lock that serialises sign-ins per
# username (the second is the username's hashtext). Any constant works: the
# two-key space does not overlap the one-key space other advisory locks use.
_LOGIN_LOCK_CLASS = 1_826_301_557


class UserCRUD:
    """Database queries for users."""

    # ── Writes (flushed; the session owner commits) ───────────────────────────

    @staticmethod
    async def add_user(db: AsyncSession, user: User) -> User:
        """Insert a user inside a savepoint, so a duplicate (IntegrityError, raised
        to the caller) rolls back only this insert; the refreshed row."""
        async with db.begin_nested():
            db.add(user)
            await db.flush()
        await db.refresh(user)
        return user

    @staticmethod
    async def save_user_in_savepoint(db: AsyncSession, user: User) -> User:
        """Write a user's changed fields inside a savepoint (a duplicate username
        raises IntegrityError and rolls back only this write); the refreshed row."""
        async with db.begin_nested():
            await db.flush()
        await db.refresh(user)
        return user

    @staticmethod
    async def save_user(db: AsyncSession, user: User) -> User:
        await db.flush()
        await db.refresh(user)
        return user

    @staticmethod
    async def record_failed_login(
        db: AsyncSession, user: User, *, max_attempts: int, lock_until: datetime
    ) -> None:
        """Count one failed sign-in in the statement that writes it, and lock the account
        when the count reaches `max_attempts`. The row lock the UPDATE takes makes a
        concurrent failure wait and add to this count, so none is lost. `user` is
        refreshed with the stored values."""
        raised = User.failed_login_attempts + 1
        await db.execute(
            update(User)
            .where(User.id == user.id)
            .values(
                failed_login_attempts=raised,
                locked_until=case(
                    (raised >= max_attempts, lock_until), else_=User.locked_until
                ),
            )
            .execution_options(synchronize_session=False)
        )
        await db.refresh(user, ["failed_login_attempts", "locked_until"])

    @staticmethod
    async def delete_user(db: AsyncSession, user: User) -> None:
        """Delete a user (their sessions cascade)."""
        await db.delete(user)

    @staticmethod
    async def get_by_username(db: AsyncSession, username: str) -> User | None:
        result = await db.execute(select(User).where(User.username == username))
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_username_for_login(
        db: AsyncSession, username: str, *, lock_wait_ms: int
    ) -> User | None:
        """The user a sign-in judges, re-read from the database after every
        earlier sign-in for the same username has committed, and row-locked
        until the transaction ends.

        Sign-ins are serialised on the username, not the row: a transaction
        advisory lock keyed on it is taken whether or not the user exists, so
        a concurrent sign-in for an unknown name queues exactly as one for a
        real account does, and neither the wait nor its refusal tells the two
        apart. The second sign-in then reads the failure count and lock the
        first committed.

        Both locks wait at most `lock_wait_ms` (PostgreSQL lock_not_available,
        55P03, past it), well inside a request's statement timeout. The wait is
        set for these reads alone; the savepoint keeps the caller's transaction
        usable after a refusal, and a lock taken inside it is held to the
        transaction's end.
        """
        async with db.begin_nested():
            await UserCRUD._wait_at_most(db, lock_wait_ms)
            await UserCRUD._queue_on_username(db, username)
            user = await UserCRUD._locked_where(db, User.username == username)
            await db.execute(text("SET LOCAL lock_timeout = DEFAULT"))
        return user

    @staticmethod
    async def get_by_id_for_login(
        db: AsyncSession, user_id: UUID, *, lock_wait_ms: int
    ) -> User | None:
        """The user a signed-in user's current-password check judges, taken
        under the same locks a sign-in for their username takes, in the same
        order (the username's advisory lock, then the row), with the same
        `lock_wait_ms`. A sign-in and a password change for one account are
        then judged one at a time against the committed failure count and
        lock, from one budget. The row is re-read by id under its lock, so a
        rename between the two reads still yields this user, serialised by
        the row lock every sign-in also takes.

        The caller is signed in, so its session row has a pending write (the
        auth dependency's last-activity stamp). The savepoint is taken on the
        connection, not the session, so nothing is flushed: holding that
        row while waiting here would deadlock against a password change of
        the same user, whose session purge deletes it."""
        connection = await db.connection()
        async with connection.begin_nested():
            await UserCRUD._wait_at_most(db, lock_wait_ms)
            username = await db.scalar(select(User.username).where(User.id == user_id))
            user = None
            if username is not None:
                await UserCRUD._queue_on_username(db, username)
                user = await UserCRUD._locked_where(db, User.id == user_id)
            await db.execute(text("SET LOCAL lock_timeout = DEFAULT"))
        return user

    @staticmethod
    async def _wait_at_most(db: AsyncSession, lock_wait_ms: int) -> None:
        await db.execute(
            select(func.set_config("lock_timeout", f"{lock_wait_ms}ms", True))
        )

    @staticmethod
    async def _queue_on_username(db: AsyncSession, username: str) -> None:
        await db.execute(
            select(
                func.pg_advisory_xact_lock(_LOGIN_LOCK_CLASS, func.hashtext(username))
            )
        )

    @staticmethod
    async def _locked_where(
        db: AsyncSession, condition: ColumnElement[bool]
    ) -> User | None:
        result = await db.execute(
            select(User)
            .where(condition)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_id_for_update(db: AsyncSession, user_id: UUID) -> User | None:
        """The user, re-read and row-locked until the transaction ends, for a
        password change: a sign-in for the account (which locks the same row)
        waits for the change to commit and then checks the new hash, so it
        cannot open a session the change's session purge misses."""
        result = await db.execute(
            select(User)
            .where(User.id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def lock_row(db: AsyncSession, user_id: UUID) -> None:
        """Row-lock the user until the transaction ends, loading nothing (so a
        caller's unflushed changes to the user are kept). A sign-in holds this
        row from its password check to its commit, so a purge of the user's
        sessions that takes it first waits for that sign-in's session and ends
        it too, and a later sign-in waits for the purge's transaction."""
        await db.execute(select(User.id).where(User.id == user_id).with_for_update())

    @staticmethod
    async def get_by_id(db: AsyncSession, user_id: UUID) -> User | None:
        result = await db.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

    @staticmethod
    async def count_active(db: AsyncSession) -> int:
        result = await db.execute(
            select(func.count(User.id)).where(User.is_active.is_(True))
        )
        return result.scalar_one()

    @staticmethod
    async def list_paginated(
        db: AsyncSession,
        *,
        skip: int = 0,
        limit: int = 100,
        role: str | None = None,
        is_active: bool | None = None,
        search: str | None = None,
    ) -> tuple[Sequence[User], int]:
        """Paginated list of users with optional filters."""
        stmt = select(User)
        if role:
            stmt = stmt.where(User.role == role)
        if is_active is not None:
            stmt = stmt.where(User.is_active.is_(is_active))
        if search:
            pattern = f"%{search}%"
            stmt = stmt.where(
                or_(
                    User.username.ilike(pattern),
                    User.full_name.ilike(pattern),
                )
            )

        total = (
            await db.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()
        result = await db.execute(
            stmt.order_by(User.created_at.desc()).offset(skip).limit(limit)
        )
        return result.scalars().all(), total

    @staticmethod
    async def count_total(db: AsyncSession) -> int:
        result = await db.execute(select(func.count()).select_from(User))
        return result.scalar_one()

    @staticmethod
    async def count_by_role(db: AsyncSession, role: str) -> int:
        result = await db.execute(select(func.count(User.id)).where(User.role == role))
        return result.scalar_one()

    @staticmethod
    async def count_other_active_admins(db: AsyncSession, user_id: UUID) -> int:
        """Active admins other than `user_id`: the ones who could still sign in.

        Every active admin row is locked until the transaction ends, so two
        requests each removing a different admin run one after the other: the
        second reads the first's committed change, not the row as it was.
        """
        result = await db.execute(
            select(User.id)
            .where(User.role == UserRole.ADMIN, User.is_active.is_(True))
            .with_for_update()
        )
        return sum(1 for admin_id in result.scalars() if admin_id != user_id)

    @staticmethod
    async def count_locked(db: AsyncSession) -> int:
        """Users with locked_until still in the future."""
        result = await db.execute(
            select(func.count(User.id)).where(User.locked_until > utc_now())
        )
        return result.scalar_one()
