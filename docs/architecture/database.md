# LuxSwirl Database Schema

This document describes the PostgreSQL/TimescaleDB database schema for LuxSwirl.

The schema is **owned by Alembic** — `apps/backend/alembic/versions/000_v1_0_baseline.py` is the authoritative definition, and `alembic upgrade head` runs from the container CMD on startup. The ORM models in `apps/backend/app/models/*_model.py` mirror it. The only thing created at runtime (by `app/db/database.py::init_db`) is TimescaleDB-specific state: hypertables, compression, retention policies, and continuous aggregates.

## Overview

LuxSwirl uses **TimescaleDB** (PostgreSQL extension) for time-series storage. The relevant tables fall into two groups:

- **Dimension tables**: `agents`, `checks` (metadata, regular PostgreSQL tables)
- **Fact tables (hypertables)**: `check_results`, `agent_metrics`, `notification_logs`, `check_artifacts` (time-series data)

All primary keys are **UUIDs** (`sa.Uuid`), not auto-incrementing integers. Hypertables use a **composite PK** of `(id, <time column>)` because TimescaleDB requires the partitioning column to be part of every unique constraint. Foreign keys (`agent_id`, `check_id`, etc.) are UUIDs.

## Schema

### agents

Stores agent metadata, health/resource telemetry from heartbeats, per-agent config overrides, and the approval workflow. Defined by the `Agent` model (`agent_model.py`); roughly 50 columns.

| Column | Type | Notes |
|--------|------|-------|
| id | UUID PK | UUID primary key (default `uuid4`) |
| agent_name | VARCHAR(255) UNIQUE, NULL | Friendly editable name; NULL until set during approval |
| agent_run_id | VARCHAR(255), NULL | Current run ID (UUID), changes on restart |
| first_seen | TIMESTAMPTZ | When agent was first seen (`server_default NOW()`) |
| last_seen | TIMESTAMPTZ | When agent last reported (`server_default NOW()`) |
| hostname | VARCHAR(255), NULL | Agent hostname |
| ip_address | VARCHAR(45), NULL | Agent IP address (IPv6-capable length) |
| version | VARCHAR(50), NULL | Agent version |
| tags | VARCHAR(1000), NULL | Comma-separated tags for grouping/filtering |
| status | VARCHAR(20), NULL | online / degraded / offline / unknown (`server_default 'unknown'`) |
| uptime_seconds | INT, NULL | Uptime from last heartbeat |
| checks_total / checks_active | INT, NULL | Configured / currently-running check counts |
| checks_executed_total / checks_succeeded_total / checks_failed_total | INT, NULL | Lifetime execution counters |
| cpu_percent | FLOAT, NULL | CPU usage percentage (last heartbeat) |
| memory_mb | INT, NULL | Memory usage in MB |
| queue_depth | INT, NULL | Current result queue depth |
| last_error | VARCHAR(1000), NULL | Last error message |
| server_unreachable_count | INT, NULL | Failed server-connection attempts |
| stored_reports_count | INT, NULL | Stored reports waiting to send |
| stored_reports_oldest_timestamp | FLOAT, NULL | Timestamp of oldest stored report |
| open_file_descriptors / fd_limit_soft / fd_usage_percent | INT/INT/FLOAT, NULL | FD-leak detection (SWIRL-57) |
| subprocess_count | INT, NULL | Number of child subprocesses |
| config_version | VARCHAR(50), NULL | Last known config version from agent |
| checks_updated_at | TIMESTAMPTZ, NULL | When checks were last modified (config-change detection) |
| heartbeat_interval / check_sync_interval | INT, NULL | Per-agent interval overrides (NULL = global default) |
| report_interval / report_batch_size / report_max_files_per_batch / report_process_interval / report_max_queue_size / report_backpressure_threshold | INT/FLOAT, NULL | Reporter config overrides (NULL = global default) |
| max_concurrent_checks / watchdog_interval / watchdog_stall_threshold | INT, NULL | Performance tuning overrides (NULL = global default) |
| log_level | VARCHAR(20), NULL | Per-agent log level (NULL = global default) |
| approval_status | VARCHAR(20) | pending / active / paused / disabled / rejected (`server_default 'pending'`) |
| api_key_hash | VARCHAR(255), NULL | Bcrypt hash of agent API key (NULL until approved) |
| api_key_created_at / api_key_last_used | TIMESTAMPTZ, NULL | API-key lifecycle timestamps |
| approved_at / approved_by | TIMESTAMPTZ / VARCHAR(255), NULL | Approval audit |
| status_reason / status_changed_at / status_changed_by | VARCHAR / TIMESTAMPTZ / VARCHAR, NULL | Status-change audit |
| created_at / updated_at | TIMESTAMPTZ | Row timestamps (from `UUIDBaseModel`) |

**Indexes**: `idx_agents_agent_name` on `agent_name`, `idx_agents_last_seen` on `last_seen`, plus the unique `ix_agents_agent_name`.

### checks

Stores health-check definitions. Defined by the `Check` model (`check_model.py`). Sensitive fields (`target`, `check_config`, `connection_string_encrypted`) are **encrypted at rest** via the custom `EncryptedString` / `EncryptedJSON` column types.

| Column | Type | Notes |
|--------|------|-------|
| id | UUID PK | UUID primary key |
| agent_id | UUID FK → agents.id | `ON DELETE CASCADE` |
| depends_on_check_id | UUID FK → checks.id, NULL | Parent check; notifications suppressed when parent is down (`ON DELETE SET NULL`) |
| display_name | VARCHAR(255) | Friendly editable display name |
| check_type | VARCHAR(50) | ping / http / tcp / json / etc. |
| target | EncryptedString(1000) | Target URL/host/IP — **encrypted at rest** |
| interval_seconds / timeout_seconds | INT, NULL | Optional scheduling config |
| description | VARCHAR(1000), NULL | Human-readable description |
| check_config | EncryptedJSON, NULL | Check-type-specific config — **encrypted at rest** (may hold API keys/tokens) |
| retry_attempts | INT, NULL | Retries before marking failed (`server_default '2'`) |
| retry_interval_seconds | INT | Retry interval (`server_default '30'`) |
| resend_notification_after | INT, NULL | Resend if down X times consecutively (NULL = disabled) |
| tags | ARRAY(String), NULL | Tags for organizing/filtering |
| enabled | BOOLEAN | Whether check is enabled (`server_default 'true'`) |
| assignment_mode | VARCHAR(20) | manual / replicate / distribute (`server_default 'manual'`) |
| agent_selector | JSON, NULL | Selector for replicate/distribute modes (`{tags: [...]}` or `{agent_ids: [...]}`) |
| script_code | TEXT, NULL | Playwright async Python for synthetic checks |
| connection_string_encrypted | EncryptedString(1000), NULL | DB connection string — **encrypted at rest** |
| created_at / updated_at | TIMESTAMPTZ | Row timestamps |

**Indexes**: `idx_checks_agent_id` on `agent_id`, `idx_checks_type` on `check_type`, `idx_checks_depends_on_check_id` on `depends_on_check_id`.

Note: there is no unique constraint on `(agent_id, display_name)` — checks are identified by their UUID `id`.

### check_results (TimescaleDB Hypertable)

Stores time-series check results. Defined by the `CheckResult` model (`check_result_model.py`). Partitioned by `timestamp` into 1-day chunks.

The UUID `id` is **generated by the agent** before execution (idempotent ingestion). The PK is **composite `(id, timestamp)`** — required because `timestamp` is the hypertable partition key.

| Column | Type | Notes |
|--------|------|-------|
| id | UUID | Result UUID (agent-generated); part of composite PK |
| timestamp | TIMESTAMPTZ | When the check executed; partition key, part of composite PK |
| agent_id | UUID FK → agents.id | `ON DELETE CASCADE` |
| check_id | UUID FK → checks.id | `ON DELETE CASCADE` |
| success | BOOLEAN | Whether the check succeeded |
| latency_ms | FLOAT, NULL | Check latency in ms (`CHECK latency_ms >= 0`) |
| error | TEXT, NULL | Error message if failed |
| error_type | VARCHAR(100), NULL | Error category (timeout, connection, dns, …) |
| http_status_code | INT, NULL | HTTP status code (HTTP checks) |
| http_response_time_ms | FLOAT, NULL | HTTP response time in ms |
| metrics | TEXT, NULL | Additional metrics as a JSON string |
| response_data | TEXT, NULL | Response body/data (truncated if large) |

**Constraint**: `ck_latency_non_negative` (`latency_ms >= 0`).

**Indexes**:
- `idx_check_results_timestamp` on `timestamp`
- `idx_check_results_agent_timestamp` on `(agent_id, timestamp)`
- `idx_check_results_check_timestamp` on `(check_id, timestamp)`
- `idx_check_results_agent_check_timestamp` on `(agent_id, check_id, timestamp)`
- `idx_check_results_success` on `success`
- `idx_check_results_id` on `id` — lets other tables reference a result by `id` alone

Note: `notification_logs` and `check_artifacts` reference a result via `(check_result_id, check_result_timestamp)` **without** a foreign key — both are hypertables, and FKs into a compressed hypertable are not supported.

### Other tables

The baseline migration also defines `users` and `sessions` (auth), `alerts`, `alert_check_mappings`, `alert_notification_mappings`, `notification_providers`, `notification_logs` (alerting), `agent_metrics` (time-series agent health), `check_artifacts` (synthetic-check screenshots/traces/video/HAR), `jobs` / `job_configurations` / `job_executions` (background jobs), `registration_keys`, `agent_check_assignments`, `status_pages`, and `settings`. All use UUID PKs; the hypertables (`agent_metrics`, `notification_logs`, `check_artifacts`) use a composite PK with their time column.

## TimescaleDB Features

All TimescaleDB runtime setup lives in `app/db/database.py::init_db` and is idempotent (`if_not_exists => TRUE`). It is skipped gracefully if the `timescaledb` extension is absent (plain PostgreSQL still works).

### Hypertables

| Hypertable | Time column | Chunk interval |
|------------|-------------|----------------|
| check_results | timestamp | 1 day |
| agent_metrics | timestamp | 1 day |
| notification_logs | sent_at | 1 day |
| check_artifacts | created_at | 1 day |

```sql
SELECT create_hypertable(
    'check_results',
    'timestamp',
    if_not_exists => TRUE,
    chunk_time_interval => INTERVAL '1 day'
);
```

### Continuous Aggregates

Three continuous aggregates roll up `check_results` at increasing granularity.

**check_results_5min** (refreshed every 5 minutes):
- `bucket` — 5-minute time bucket
- `agent_id`, `check_id` — grouping dimensions
- `total_checks`, `successful_checks`
- `avg_latency_ms`, `min_latency_ms`, `max_latency_ms`
- `p50_latency_ms`, `p95_latency_ms`, `p99_latency_ms` (percentiles)

**check_results_hourly** (refreshed hourly, retained 365 days): grouped by `check_id` + hourly `bucket`; `check_count`, `avg/min/max_latency`, `success_rate`, `last_check_time`.

**check_results_daily** (refreshed daily, retained 1825 days / 5 years): grouped by `check_id` + daily `bucket`; `check_count`, `uptime_percent`, `avg/min/max_latency`, `failure_count`, `last_check_time`.

### Retention Policies

| Table | Retention |
|-------|-----------|
| check_results | **90 days** (default, from `settings.server.default_retention_days`) |
| agent_metrics | 30 days |
| notification_logs | 30 days |
| check_artifacts | 30 days |
| check_results_hourly | 365 days |
| check_results_daily | 1825 days (5 years) |

```sql
-- retention_days defaults to 90
SELECT add_retention_policy('check_results', INTERVAL '90 days', if_not_exists => TRUE);
```

### Compression

TimescaleDB columnar compression provides **80-90% space savings**. `check_results` is segmented by `check_id` (efficient single-check queries) and ordered by `timestamp DESC`:

```sql
ALTER TABLE check_results SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'check_id',
    timescaledb.compress_orderby = 'timestamp DESC'
);

SELECT add_compression_policy('check_results', INTERVAL '7 days', if_not_exists => TRUE);
```

`notification_logs` (segmented by `alert_id`) and `check_artifacts` (segmented by `check_id`) are also compressed after 7 days.

#### Why 7 Days?

**CRITICAL**: Compressed chunks are **READ-ONLY**. Once compressed, you cannot INSERT, UPDATE, or DELETE. Inserting into a compressed chunk fails with:
```
ERROR: cannot insert into compressed chunk
```

The 7-day delay exists for:
1. **Late-arriving data**: agents store failed reports locally and replay them on reconnect
2. **Agent backlog**: offline agents can be down for hours/days before reconnecting
3. **Data corrections**: rare manual updates to historical data
4. **Write performance**: avoid decompression overhead for recent data

**Trade-off**:
- Recent data (0–7 days): uncompressed, writable, full write performance
- Older data (7+ days): compressed, read-only, 80-90% space savings

**What happens if an agent is offline > 7 days?**

Late-arriving data older than the compression boundary lands in a compressed chunk and is rejected; the error is logged and the agent retries. To recover, decompress the affected chunk:

```sql
-- Find compressed chunks in a date range
SELECT chunk_name, range_start, range_end
FROM timescaledb_information.chunks
WHERE hypertable_name = 'check_results'
  AND is_compressed = true
  AND range_start >= '2026-01-01';

-- Decompress a specific chunk
SELECT decompress_chunk('_timescaledb_internal._hyper_1_5_chunk');
```

After decompression the replayed reports succeed, and the chunk is re-compressed on the next compression job cycle.

**Recommendations**:
- **Monitor agent connectivity**: alert if an agent is offline > 6 days
- **Adjust the compression interval**: if agents routinely go offline for weeks, raise it to 14 or 30 days (`database.compress_after_days` setting)

## Database Service API

Database access is layered (see [`backend.md`](backend.md) for the enforced layering chain). Domain-specific **core services** in `apps/backend/app/services/core/` own transactions and business logic:

- `agent_core_service.py` — agent CRUD and online status
- `check_core_service.py` — check CRUD and upsert
- `check_result_core_service.py` — result ingestion, history, summaries
- `cleanup_core_service.py` — retention and pruning operations

**CRUD modules** (`apps/backend/app/crud/`) are the only layer that issues SQL (`select`, `update`, `delete`, `text`); core services orchestrate transactions on top of them.

## Connection

The connection string is configured via the Pydantic `DatabaseSettings` model (`app/core/config.py`), reached as `settings.database.url` and consumed in `app/db/database.py::get_engine`.

Format: `postgresql+asyncpg://user:password@host:port/database`

Settings are nested under the `DATABASE__` prefix (note the double underscore — `env_nested_delimiter="__"`):

```bash
DATABASE__URL=postgresql+asyncpg://luxswirl:luxswirl@timescaledb:5432/luxswirl
DATABASE__ECHO=false
DATABASE__POOL_SIZE=20
DATABASE__MAX_OVERFLOW=10
```

The engine also applies a per-connection `statement_timeout` (5s) and `idle_in_transaction_session_timeout` (30s) so a stray web handler holding a transaction gets killed before it blocks other requests (LUXSWIRL-105). The maintenance worker lifts these per-transaction with `SET LOCAL ... = 0` for cascading mutations.

## Performance Considerations

### Indexes

All common query patterns on `check_results` are indexed: time-range scans (`timestamp`), per-agent and per-check time-range queries, and the combined `(agent_id, check_id, timestamp)` path.

### Continuous Aggregates

Use the rollup views for dashboards instead of scanning raw `check_results`:

```sql
SELECT
    bucket,
    successful_checks * 100.0 / total_checks AS success_rate_pct,
    avg_latency_ms,
    p95_latency_ms
FROM check_results_5min
WHERE agent_id = '...'      -- UUID
  AND check_id = '...'      -- UUID
  AND bucket >= NOW() - INTERVAL '24 hours'
ORDER BY bucket DESC;
```

Pick `check_results_5min`, `check_results_hourly`, or `check_results_daily` based on the time window being displayed.

### Retention

The 90-day default retention on `check_results` keeps the database manageable while the hourly/daily aggregates preserve long-term trends for 1–5 years. Adjust via the `database.*_retention_days` settings (seeded into the `settings` table by `init_db`).
