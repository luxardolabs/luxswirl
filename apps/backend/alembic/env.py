"""Alembic migration environment."""

import asyncio
import logging
from logging.config import fileConfig

import app.models  # noqa: F401 — registers tables on Base.metadata
from alembic import context
from app.core.config import settings
from app.models.base_model import Base
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Target metadata for autogenerate support.
target_metadata = Base.metadata

logger = logging.getLogger(__name__)


def include_object(object, name, type_, reflected, compare_to):
    """Exclude TimescaleDB-managed objects from autogenerate / `alembic check`.

    `create_hypertable()` (run at app boot, not in migrations) auto-creates a
    time index named ``<table>_<timecol>_idx`` on each hypertable. It isn't in
    the models, so without this filter every booted DB (dev AND production) would
    report a spurious "remove_index" — and alembic must never try to drop an
    index TimescaleDB owns. Model-declared indexes use idx_/ix_/uq_ prefixes, so
    a reflected `*_idx` index without one of those is TimescaleDB's.
    """
    timescale_owned = (
        type_ == "index"
        and reflected
        and compare_to is None
        and bool(name)
        and name.endswith("_idx")
        and not name.startswith(("idx_", "ix_", "uq_"))
    )
    return not timescale_owned


def get_url() -> str:
    """The CALLER's url wins; settings are only the fallback.

    env.py runs *after* a caller sets `sqlalchemy.url` on the Config, so an
    env.py that picks its own url silently overrides it — and the test harness
    hands alembic the throwaway database it has just created. Getting this
    backwards means `make test` migrates whatever env.py chose (under the dev
    env file: the dev database) while the harness's own dev-DB guard reports
    fine, because it validates the url the fixture *intends* and the override
    happens two layers down inside alembic.

    `alembic.ini` deliberately carries no `sqlalchemy.url` — with a value there
    the two are indistinguishable through the Config and no expression can
    prefer the caller — so a bare `alembic upgrade head` still falls through to
    settings. Enforced by repo.alembic_env_honors_caller_url.
    """
    return config.get_main_option("sqlalchemy.url") or settings.database.url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emits SQL without connecting."""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations using the given sync connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Build a fresh async engine and run migrations against it."""
    configuration = config.get_section(config.config_ini_section) or {}
    # get_url() consults the Config first, so this assignment re-states the
    # caller's own value rather than overriding it.
    configuration["sqlalchemy.url"] = get_url()

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Online migrations always run via a fresh async engine."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    logger.info("Running migrations in offline mode")
    run_migrations_offline()
else:
    logger.info("Running migrations in online mode")
    run_migrations_online()
