"""
Database maintenance CRUD - table-health introspection, VACUUM and aggregate refresh.

Every statement is a literal: VACUUM is DDL and `FROM <table>` is an identifier,
so neither takes a bind parameter, and the per-table VACUUM is pre-built for a fixed
allowlist instead of interpolated. Adding a table means adding it to `_MODELS`
and `_TABLE_SQL` (a module-load check keeps them in step).

VACUUM and `refresh_continuous_aggregate()` cannot run inside a transaction
block, so they take their own AUTOCOMMIT connection from the engine rather than
the caller's session. That connection also lifts the engine's 5-second
`statement_timeout` (a web-path guard) for the duration: a VACUUM FULL or a
full-range refresh legitimately runs for minutes.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import TextClause, func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.db.database import get_engine
from app.models.agent_metric_model import AgentMetric
from app.models.agent_model import Agent
from app.models.base_model import Base
from app.models.check_artifact_model import CheckArtifact
from app.models.check_model import Check
from app.models.check_result_model import CheckResult
from app.models.job_model import Job
from app.models.notification_log_model import NotificationLog
from app.models.session_model import Session
from app.models.user_model import User

# The tables maintenance may touch, by name, with the model that maps each.
_MODELS: dict[str, type[Base]] = {
    "check_results": CheckResult,
    "check_artifacts": CheckArtifact,
    "checks": Check,
    "agents": Agent,
    "jobs": Job,
    "notification_logs": NotificationLog,
    "agent_metrics": AgentMetric,
    "sessions": Session,
    "users": User,
}
MAINTAINED_TABLES = frozenset(_MODELS)

_TABLE_SQL: dict[str, dict[str, TextClause]] = {
    "check_results": {
        "vacuum": text("VACUUM ANALYZE check_results"),
        "vacuum_full": text("VACUUM FULL ANALYZE check_results"),
    },
    "check_artifacts": {
        "vacuum": text("VACUUM ANALYZE check_artifacts"),
        "vacuum_full": text("VACUUM FULL ANALYZE check_artifacts"),
    },
    "checks": {
        "vacuum": text("VACUUM ANALYZE checks"),
        "vacuum_full": text("VACUUM FULL ANALYZE checks"),
    },
    "agents": {
        "vacuum": text("VACUUM ANALYZE agents"),
        "vacuum_full": text("VACUUM FULL ANALYZE agents"),
    },
    "jobs": {
        "vacuum": text("VACUUM ANALYZE jobs"),
        "vacuum_full": text("VACUUM FULL ANALYZE jobs"),
    },
    "notification_logs": {
        "vacuum": text("VACUUM ANALYZE notification_logs"),
        "vacuum_full": text("VACUUM FULL ANALYZE notification_logs"),
    },
    "agent_metrics": {
        "vacuum": text("VACUUM ANALYZE agent_metrics"),
        "vacuum_full": text("VACUUM FULL ANALYZE agent_metrics"),
    },
    "sessions": {
        "vacuum": text("VACUUM ANALYZE sessions"),
        "vacuum_full": text("VACUUM FULL ANALYZE sessions"),
    },
    "users": {
        "vacuum": text("VACUUM ANALYZE users"),
        "vacuum_full": text("VACUUM FULL ANALYZE users"),
    },
}

# The continuous aggregates init_db() creates, refreshed by name (a literal each).
_REFRESH_SQL: dict[str, TextClause] = {
    "check_results_5min": text(
        "CALL refresh_continuous_aggregate('check_results_5min', NULL, NULL)"
    ),
    "check_results_hourly": text(
        "CALL refresh_continuous_aggregate('check_results_hourly', NULL, NULL)"
    ),
    "check_results_daily": text(
        "CALL refresh_continuous_aggregate('check_results_daily', NULL, NULL)"
    ),
}
CONTINUOUS_AGGREGATES = tuple(_REFRESH_SQL)

if set(_TABLE_SQL) != MAINTAINED_TABLES:
    raise RuntimeError(
        "MAINTAINED_TABLES and _TABLE_SQL drifted: add SQL for every maintained table"
    )

# check_results is a hypertable: its data lives in chunks, which
# pg_total_relation_size() on the parent does not count.
_HYPERTABLE_SIZE = text("SELECT hypertable_size('check_results')")
_RELATION_SIZE = text("SELECT pg_total_relation_size(:t)")
_TUPLE_STATS = text(
    "SELECT n_dead_tup, n_live_tup FROM pg_stat_user_tables WHERE relname = :t"
)


_NO_TIMEOUT = text("SET statement_timeout = 0")
# Back to the connection's startup value (the engine's server_settings), so the
# pooled connection carries the web-path timeout again for its next user.
_RESET_TIMEOUT = text("RESET statement_timeout")


@asynccontextmanager
async def _maintenance_connection() -> AsyncIterator[AsyncConnection]:
    """An AUTOCOMMIT engine connection with no statement timeout, reset on exit."""
    async with get_engine().connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(_NO_TIMEOUT)
        try:
            yield conn
        finally:
            await conn.execute(_RESET_TIMEOUT)


def _require_maintained(table: str) -> None:
    if table not in MAINTAINED_TABLES:
        raise ValueError(
            f"Invalid table name: {table}. "
            f"Allowed values: {', '.join(sorted(MAINTAINED_TABLES))}"
        )


def _size_query(table: str) -> tuple[TextClause, dict[str, str]]:
    if table == "check_results":
        return _HYPERTABLE_SIZE, {}
    return _RELATION_SIZE, {"t": table}


class DatabaseMaintenanceCRUD:
    """Table-health reads and the two statements that need autocommit."""

    @staticmethod
    async def table_size_bytes(db: AsyncSession, table: str) -> int | None:
        """Total on-disk size of `table` (chunks included for the hypertable)."""
        _require_maintained(table)
        sql, params = _size_query(table)
        return (await db.execute(sql, params)).scalar()

    @staticmethod
    async def row_count(db: AsyncSession, table: str) -> int:
        """Exact row count of `table`."""
        _require_maintained(table)
        stmt = select(func.count()).select_from(_MODELS[table])
        return (await db.execute(stmt)).scalar_one()

    @staticmethod
    async def tuple_stats(db: AsyncSession, table: str) -> tuple[int, int]:
        """(dead, live) tuple counts from pg_stat_user_tables; (0, 0) if unseen."""
        _require_maintained(table)
        row = (await db.execute(_TUPLE_STATS, {"t": table})).one_or_none()
        if row is None:
            return 0, 0
        dead, live = row
        return int(dead or 0), int(live or 0)

    @staticmethod
    async def vacuum(table: str, *, full: bool) -> tuple[int | None, int | None]:
        """VACUUM (FULL) ANALYZE `table`; returns its size before and after.

        One AUTOCOMMIT connection for all three statements, so the size read
        after the VACUUM sees its effect.
        """
        _require_maintained(table)
        sql, params = _size_query(table)
        vacuum_sql = _TABLE_SQL[table]["vacuum_full" if full else "vacuum"]
        async with _maintenance_connection() as conn:
            before = (await conn.execute(sql, params)).scalar()
            await conn.execute(vacuum_sql)
            after = (await conn.execute(sql, params)).scalar()
        return before, after

    @staticmethod
    async def refresh_continuous_aggregate(name: str) -> None:
        """Refresh one continuous aggregate over its whole range (AUTOCOMMIT)."""
        if name not in _REFRESH_SQL:
            raise ValueError(f"Unknown continuous aggregate: {name}")
        async with _maintenance_connection() as conn:
            await conn.execute(_REFRESH_SQL[name])
