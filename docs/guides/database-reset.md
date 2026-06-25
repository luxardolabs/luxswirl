# Database Reset Instructions

This guide describes how to wipe the LuxSwirl database and bring it back from a clean slate. The schema is owned by Alembic and TimescaleDB setup runs automatically on boot — **there is no manual schema-creation step.**

## How the schema actually gets built

You never run `CREATE EXTENSION`, `Base.metadata.create_all`, or a seed script by hand. The server container does it for you every time it starts:

1. The container `command` (see `compose.yaml`) runs `alembic upgrade head`, which applies the migrations in `apps/backend/alembic/versions/` (baseline `000_v1_0_baseline.py` and onward) to create every table, index, and constraint.
2. The app's `init_db()` (`apps/backend/app/db/database.py`) then runs at startup. It enables the TimescaleDB extension, converts the time-series tables into hypertables, and configures compression, retention, and the continuous aggregates — all idempotently.
3. `init_db()` also seeds the default `settings` rows if that table is empty.

Because all of this is idempotent and runs on every boot, **resetting the database is simply: drop the data volume, then start the stack again.**

> A fresh database comes up **empty** — there is no seeded test data. On first run you create the admin account through the `/setup` first-run wizard (the server logs `first-run setup wizard enabled (/setup)` when no admin exists).

## ⚠️ This is destructive — back up first

`make clean-all` runs `docker compose ... down -v`, which **drops the `luxswirl_db_data` volume and permanently deletes all checks, results, history, users, and settings.** There is no undo. Take a backup first unless you genuinely want an empty database.

## Reset procedure

### 1. (Recommended) Back up the current database

```bash
make db-backup
```

`pg_dump`s the database to `backups/YYYY/MM/DD/luxswirl_<timestamp>.dump` (custom format). Skip only if you are certain you don't need the data.

### 2. Stop the stack

```bash
make dev-down     # or: make prod-down
```

### 3. Drop the database volume (DESTRUCTIVE)

```bash
make clean-all
```

This removes the containers, orphans, **and** the `luxswirl_db_data` volume.

### 4. Start the stack again

```bash
make dev-up       # or: make prod-up
```

On boot the server container automatically runs `alembic upgrade head` and then `init_db()`, recreating the full schema, the TimescaleDB hypertables, the compression/retention policies, the continuous aggregates, and the default settings. The agent comes up once the server is healthy.

### 5. Complete first-run setup

Open the UI and visit `/setup` to create the admin account. There is no seeded test data — the install starts empty.

## Inspecting the database

To poke around the running database (verify migrations applied, check tables, watch results land):

```bash
make db-shell     # psql into the running TimescaleDB as luxswirl/luxswirl
```

Useful one-liners once you're at the `psql` prompt:

```sql
\dt                                  -- list tables
SELECT * FROM alembic_version;       -- confirm migrations are at head
SELECT COUNT(*) FROM check_results;  -- watch results populate as checks run
SELECT hypertable_name FROM timescaledb_information.hypertables;
```

You can also follow startup logs to confirm the migrate + init sequence:

```bash
make dev-logs     # or: make prod-logs
```

## What does NOT apply anymore

For anyone following older notes — these steps are obsolete and will not work:

- Manually `DROP DATABASE` / `CREATE DATABASE` via `psql`.
- Manual `CREATE EXTENSION timescaledb`.
- Hand-running `Base.metadata.create_all` (e.g. `from db.session import engine` / `from models.base import Base`). The schema is managed by Alembic, not by `create_all`.
- `python db_init_test_data.py` — this seed script was **deleted**. Fresh installs are intentionally empty and go through the `/setup` wizard.
