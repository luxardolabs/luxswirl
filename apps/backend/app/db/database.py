"""
Database session management and connection pooling.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings
from app.shared.logger import get_logger

logger = get_logger("luxswirl.database")


# The engine and the ONE session maker every session in the app comes from, built
# at import. Building them opens no connection: the pool connects on first use.
# Every importer holds this same maker object, which is what lets the test harness
# (luxarch's test-db-bound block) bind it to each test's rolled-back transaction.
#
# statement_timeout + idle_in_transaction_session_timeout are belt-and-suspenders
# for the "web routes commit fast" rule. Any web handler that accidentally holds a
# transaction longer than 5s gets killed before it can block other UI requests.
# The maintenance worker borrows from this same pool but issues
# `SET LOCAL ... = 0` per transaction to lift the limits for cascading mutations.
engine: AsyncEngine = create_async_engine(
    str(settings.database.url),
    echo=settings.database.echo,
    pool_size=settings.database.pool_size,
    max_overflow=settings.database.max_overflow,
    pool_pre_ping=settings.database.pool_pre_ping,
    pool_recycle=3600,
    connect_args={
        "server_settings": {
            "statement_timeout": "5000",
            "idle_in_transaction_session_timeout": "30000",
        },
    },
)

AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


def get_engine() -> AsyncEngine:
    """
    The global database engine.

    Returns:
        AsyncEngine instance
    """
    return engine


def get_session_maker() -> async_sessionmaker[AsyncSession]:
    """
    The global session maker.

    Returns:
        async_sessionmaker instance
    """
    return AsyncSessionLocal


async def get_db() -> AsyncGenerator[AsyncSession]:
    """
    Dependency to get a database session.

    Yields:
        AsyncSession instance

    Example:
        @router.get("/agents")
        async def list_agents(db: AsyncSession = Depends(get_db, scope="function")):
            ...
    """
    session_maker = get_session_maker()
    async with session_maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


@asynccontextmanager
async def worker_session() -> AsyncGenerator[AsyncSession]:
    """The background worker's equivalent of `get_db()`.

    Background tasks (the maintenance worker) run outside the FastAPI request
    lifecycle, so they have no `get_db()` wrapping their unit of work. This is
    that boundary: one transaction per `async with`, committed on clean exit and
    rolled back on exception — so worker helpers and the core services they call
    stay commit-free, exactly like the request path.

    Example:
        async with worker_session() as db:
            await MaintenanceJobCoreService.mark_running(db, job.id)
    """
    session_maker = get_session_maker()
    async with session_maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db() -> None:
    """
    Initialize TimescaleDB hypertables, compression, retention, continuous aggregates.

    Table schema is owned by Alembic (`alembic upgrade head` runs from the
    container CMD before this function executes). This function only handles
    TimescaleDB-specific runtime state, which is idempotent.
    """
    await init_timescale(get_engine())


async def init_timescale(engine: AsyncEngine) -> None:
    """The TimescaleDB runtime state, against a GIVEN engine.

    Split out from init_db so the test harness can run the real thing against
    the throwaway database instead of keeping its own copy of the hypertable
    list. That copy had drifted — it named `notification_logs`, which this
    function deliberately does not convert — and because the fixture wrapped
    every conversion in a blanket suppress, the drift was invisible until three
    /database-health routes started returning 500 for no stated reason. A second
    description of the same DDL is the bug; there is now one.

    Takes the engine rather than reading settings so the caller decides which
    database it is, which is the whole point for a suite whose dedicated
    TEST_DATABASE_URL must never be overridable by the app's own variable.
    """
    logger.info("Initializing TimescaleDB extensions")

    async with engine.begin() as conn:
        # Check if TimescaleDB extension exists
        result = await conn.execute(
            text("SELECT COUNT(*) FROM pg_extension WHERE extname = 'timescaledb'")
        )
        row = result.fetchone()
        has_timescale = row is not None and row[0] > 0

        if has_timescale:
            logger.info("TimescaleDB extension detected")

            # Convert check_results to hypertable if not already
            try:
                await conn.execute(
                    text(
                        """
                        SELECT create_hypertable(
                            'check_results',
                            'timestamp',
                            if_not_exists => TRUE,
                            chunk_time_interval => INTERVAL '1 day'
                        )
                        """
                    )
                )
                logger.info("Created TimescaleDB hypertable for check_results")

                # Convert agent_metrics to hypertable
                await conn.execute(
                    text(
                        """
                        SELECT create_hypertable(
                            'agent_metrics',
                            'timestamp',
                            if_not_exists => TRUE,
                            chunk_time_interval => INTERVAL '1 day'
                        )
                        """
                    )
                )
                logger.info("Created TimescaleDB hypertable for agent_metrics")

                # Add retention policy for agent_metrics (30 days)
                await conn.execute(
                    text(
                        """
                        SELECT add_retention_policy('agent_metrics',
                            INTERVAL '30 days',
                            if_not_exists => TRUE
                        )
                        """
                    )
                )
                logger.info("Added retention policy for agent_metrics: 30 days")

                # notification_logs is a plain table (audit log, not metrics) —
                # retention via the cleanup_notification_logs DELETE job.

                # Convert check_artifacts to hypertable
                # Note: No FK to check_results - both are hypertables with matching retention
                await conn.execute(
                    text(
                        """
                        SELECT create_hypertable(
                            'check_artifacts',
                            'created_at',
                            if_not_exists => TRUE,
                            chunk_time_interval => INTERVAL '1 day'
                        )
                        """
                    )
                )
                logger.info("Created TimescaleDB hypertable for check_artifacts")

                # Enable compression on check_artifacts
                await conn.execute(
                    text(
                        """
                        ALTER TABLE check_artifacts SET (
                            timescaledb.compress,
                            timescaledb.compress_segmentby = 'check_id',
                            timescaledb.compress_orderby = 'created_at DESC'
                        )
                        """
                    )
                )
                logger.info("Enabled compression on check_artifacts hypertable")

                # Add compression policy for check_artifacts (7 days)
                await conn.execute(
                    text(
                        """
                        SELECT add_compression_policy('check_artifacts',
                            INTERVAL '7 days',
                            if_not_exists => TRUE
                        )
                        """
                    )
                )
                logger.info("Added compression policy for check_artifacts: 7 days")

                # Add retention policy for check_artifacts (30 days)
                await conn.execute(
                    text(
                        """
                        SELECT add_retention_policy('check_artifacts',
                            INTERVAL '30 days',
                            if_not_exists => TRUE
                        )
                        """
                    )
                )
                logger.info("Added retention policy for check_artifacts: 30 days")

                # Create continuous aggregates for 5-minute rollups
                await conn.execute(
                    text(
                        """
                        CREATE MATERIALIZED VIEW IF NOT EXISTS check_results_5min
                        WITH (timescaledb.continuous) AS
                        SELECT
                            time_bucket('5 minutes', timestamp) AS bucket,
                            agent_id,
                            check_id,
                            COUNT(*) as total_checks,
                            SUM(CASE WHEN success THEN 1 ELSE 0 END) as successful_checks,
                            AVG(latency_ms) as avg_latency_ms,
                            MIN(latency_ms) as min_latency_ms,
                            MAX(latency_ms) as max_latency_ms,
                            PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY latency_ms) as p50_latency_ms,
                            PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms) as p95_latency_ms,
                            PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY latency_ms) as p99_latency_ms
                        FROM check_results
                        GROUP BY bucket, agent_id, check_id
                        WITH NO DATA
                        """
                    )
                )
                logger.info("Created continuous aggregate: check_results_5min")

                # Add refresh policy for 5-minute aggregates
                await conn.execute(
                    text(
                        """
                        SELECT add_continuous_aggregate_policy('check_results_5min',
                            start_offset => INTERVAL '1 hour',
                            end_offset => INTERVAL '5 minutes',
                            schedule_interval => INTERVAL '5 minutes',
                            if_not_exists => TRUE
                        )
                        """
                    )
                )
                logger.info("Added refresh policy for check_results_5min")

                # Create hourly continuous aggregate
                await conn.execute(
                    text(
                        """
                        CREATE MATERIALIZED VIEW IF NOT EXISTS check_results_hourly
                        WITH (timescaledb.continuous) AS
                        SELECT
                            check_id,
                            time_bucket('1 hour', timestamp) AS bucket,
                            COUNT(*) as check_count,
                            AVG(latency_ms) as avg_latency,
                            MIN(latency_ms) as min_latency,
                            MAX(latency_ms) as max_latency,
                            SUM(CASE WHEN success THEN 1 ELSE 0 END)::float / COUNT(*) * 100 as success_rate,
                            MAX(timestamp) as last_check_time
                        FROM check_results
                        GROUP BY check_id, bucket
                        WITH NO DATA
                        """
                    )
                )
                logger.info("Created continuous aggregate: check_results_hourly")

                # Add refresh policy for hourly aggregates
                await conn.execute(
                    text(
                        """
                        SELECT add_continuous_aggregate_policy('check_results_hourly',
                            start_offset => INTERVAL '7 days',
                            end_offset => INTERVAL '1 hour',
                            schedule_interval => INTERVAL '1 hour',
                            if_not_exists => TRUE
                        )
                        """
                    )
                )
                logger.info("Added refresh policy for check_results_hourly")

                # Create daily continuous aggregate
                await conn.execute(
                    text(
                        """
                        CREATE MATERIALIZED VIEW IF NOT EXISTS check_results_daily
                        WITH (timescaledb.continuous) AS
                        SELECT
                            check_id,
                            time_bucket('1 day', timestamp) AS bucket,
                            COUNT(*) as check_count,
                            SUM(CASE WHEN success THEN 1 ELSE 0 END)::float / COUNT(*) * 100 as uptime_percent,
                            AVG(latency_ms) as avg_latency,
                            MIN(latency_ms) as min_latency,
                            MAX(latency_ms) as max_latency,
                            SUM(CASE WHEN NOT success THEN 1 ELSE 0 END) as failure_count,
                            MAX(timestamp) as last_check_time
                        FROM check_results
                        GROUP BY check_id, bucket
                        WITH NO DATA
                        """
                    )
                )
                logger.info("Created continuous aggregate: check_results_daily")

                # Add refresh policy for daily aggregates
                await conn.execute(
                    text(
                        """
                        SELECT add_continuous_aggregate_policy('check_results_daily',
                            start_offset => INTERVAL '30 days',
                            end_offset => INTERVAL '1 day',
                            schedule_interval => INTERVAL '1 day',
                            if_not_exists => TRUE
                        )
                        """
                    )
                )
                logger.info("Added refresh policy for check_results_daily")

                # Enable compression on check_results hypertable
                # Compression provides 80-90% space savings using columnar storage
                # Segmented by check_id for efficient querying of single checks
                # Ordered by timestamp DESC for optimal time-series queries
                await conn.execute(
                    text(
                        """
                        ALTER TABLE check_results SET (
                            timescaledb.compress,
                            timescaledb.compress_segmentby = 'check_id',
                            timescaledb.compress_orderby = 'timestamp DESC'
                        )
                        """
                    )
                )
                logger.info("Enabled compression on check_results hypertable")

                # Add compression policy (compress chunks older than 7 days)
                #
                # IMPORTANT: Compressed chunks are READ-ONLY!
                # Once compressed, you cannot INSERT, UPDATE, or DELETE data.
                # Attempts to insert into compressed chunks will fail with:
                #   ERROR: cannot insert into compressed chunk
                #
                # The 7-day delay exists to allow:
                #   1. Late-arriving data from offline agents (stored reports replay)
                #   2. Agent backlog processing when reconnecting after downtime
                #   3. Manual data corrections/updates if needed
                #   4. Avoid decompression overhead (seconds per chunk, but wastes space until re-compressed)
                #
                # Trade-off: Recent data (0-7 days) stays uncompressed and writable,
                # older data (7+ days) gets compressed for 80-90% space savings.
                #
                # If agent is offline > 7 days: Late data is rejected with error.
                # See DATABASE.md for monitoring recommendations and manual decompression procedure.
                await conn.execute(
                    text(
                        """
                        SELECT add_compression_policy('check_results',
                            INTERVAL '7 days',
                            if_not_exists => TRUE
                        )
                        """
                    )
                )
                logger.info("Added compression policy: compress after 7 days")

                # Add retention policy for check_results (90 days default)
                retention_days = settings.server.default_retention_days
                await conn.execute(
                    text(
                        """
                        SELECT add_retention_policy('check_results',
                            CAST(:keep AS interval),
                            if_not_exists => TRUE
                        )
                        """
                    ),
                    {"keep": timedelta(days=retention_days)},
                )
                logger.info(
                    "Added retention policy for check_results",
                    extra={"retention_days": retention_days},
                )

                # Add retention policy for hourly aggregates (365 days)
                await conn.execute(
                    text(
                        """
                        SELECT add_retention_policy('check_results_hourly',
                            INTERVAL '365 days',
                            if_not_exists => TRUE
                        )
                        """
                    )
                )
                logger.info("Added retention policy for check_results_hourly: 365 days")

                # Add retention policy for daily aggregates (1825 days / 5 years)
                await conn.execute(
                    text(
                        """
                        SELECT add_retention_policy('check_results_daily',
                            INTERVAL '1825 days',
                            if_not_exists => TRUE
                        )
                        """
                    )
                )
                logger.info(
                    "Added retention policy for check_results_daily: 1825 days (5 years)"
                )

            # swallowed-exceptions: boot-time policy setup is best-effort —
            # the app runs on plain PostgreSQL without these policies.
            except Exception:
                logger.warning("TimescaleDB setup partially failed", exc_info=True)
        else:
            logger.warning("TimescaleDB extension not found - using regular PostgreSQL")

    logger.info("Database initialization complete")


async def close_db() -> None:
    """
    Close database connections.

    This should be called on application shutdown.
    """
    logger.info("Closing database connections")
    await engine.dispose()
    logger.info("Database connections closed")


async def check_db_health() -> bool:
    """
    Check database health.

    Returns:
        True if database is healthy, False otherwise
    """
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    # swallowed-exceptions: returning False IS the contract — a health check
    # reports unhealthy rather than raising at its caller.
    except Exception:
        logger.exception("Database health check failed")
        return False
