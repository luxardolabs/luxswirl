# Check Model Schema

Schema reference for the `Check` model, which stores health-check definitions.

- **Model:** `apps/backend/app/models/check_model.py` (`Check`, extends `UUIDBaseModel`)
- **Pydantic schemas:** `apps/backend/app/schemas/check_schema.py`
- **DB table:** `checks` (created in `apps/backend/alembic/versions/000_v1_0_baseline.py`)

Each check is uniquely identified by a UUID `id`. A check carries its target, type-specific configuration, retry/notification policy, and agent-assignment strategy. Sensitive fields (`target`, `check_config`, `connection_string_encrypted`) are encrypted at rest.

## Table: `checks`

### Identity and timestamps

Inherited from `UUIDBaseModel` (`UUIDPrimaryKeyMixin` + `TimestampMixin`).

| Column | Type | Nullable | Default | Purpose |
|--------|------|----------|---------|---------|
| `id` | UUID | NOT NULL | `uuid4()` | Primary key. |
| `created_at` | TIMESTAMPTZ | NOT NULL | `now()` | Row creation timestamp. |
| `updated_at` | TIMESTAMPTZ | NOT NULL | `now()` (on update) | Last-modified timestamp. |

### Core fields

| Column | Type | Nullable | Default | Purpose |
|--------|------|----------|---------|---------|
| `agent_id` | UUID | NOT NULL | — | FK to `agents.id`, `ON DELETE CASCADE`. The agent that runs this check. |
| `display_name` | VARCHAR(255) | NOT NULL | — | Friendly, editable display name. |
| `check_type` | VARCHAR(50) | NOT NULL | — | Check type (see [Check types](#check-types)). |
| `target` | EncryptedString(1000) | NOT NULL | — | Check target (URL, hostname, IP, etc.). **Encrypted at rest** — may contain credentials embedded in a URL. |
| `description` | VARCHAR(1000) | NULL | — | Human-readable description. |
| `enabled` | BOOLEAN | NOT NULL | `true` | Whether the check is enabled. |

### Scheduling and execution

| Column | Type | Nullable | Default | Purpose |
|--------|------|----------|---------|---------|
| `interval_seconds` | INTEGER | NULL | — | How often the check runs, in seconds. NULL falls back to a global default. |
| `timeout_seconds` | INTEGER | NULL | — | Per-execution timeout, in seconds. |

### Retry and notification policy

| Column | Type | Nullable | Default | Purpose |
|--------|------|----------|---------|---------|
| `retry_attempts` | INTEGER | NULL | `2` (server default) | Number of retry attempts for a single execution before the check is marked failed. |
| `retry_interval_seconds` | INTEGER | NOT NULL | `30` | Delay between retries, in seconds (heartbeat retry interval). |
| `resend_notification_after` | INTEGER | NULL | — | Resend a down notification after the check has been down this many consecutive times. NULL disables resends. |

### Configuration (encrypted / type-specific)

| Column | Type | Nullable | Default | Purpose |
|--------|------|----------|---------|---------|
| `check_config` | EncryptedJSON (stored as TEXT) | NULL | — | Type-specific configuration as a JSON object. **Encrypted at rest** — may hold API keys, tokens, headers, etc. Accessed via the derived properties below. |
| `script_code` | TEXT | NULL | — | Python script body for `synthetic` checks (Playwright async). |
| `connection_string_encrypted` | EncryptedString(1000) | NULL | — | Database connection string for `mysql`/`postgres` checks. **Encrypted at rest.** Read/written through the `connection_string` property. |

### Organization

| Column | Type | Nullable | Default | Purpose |
|--------|------|----------|---------|---------|
| `tags` | ARRAY(VARCHAR) | NULL | — | PostgreSQL array of tags for organizing/filtering checks. |

### Agent assignment and dependencies

| Column | Type | Nullable | Default | Purpose |
|--------|------|----------|---------|---------|
| `assignment_mode` | VARCHAR(20) | NOT NULL | `manual` | How the check is assigned to agents: `manual`, `replicate`, or `distribute`. |
| `agent_selector` | JSON | NULL | — | Selector used by `replicate`/`distribute` modes, e.g. `{"tags": [...]}` or `{"agent_ids": [...]}`. Plaintext JSON (not encrypted). |
| `depends_on_check_id` | UUID | NULL | — | FK to `checks.id`, `ON DELETE SET NULL`. Parent check this one depends on; notifications are suppressed while the parent is down. |

## Encryption

Three columns use the transparent field-encryption types from `apps/backend/app/core/encrypted_types.py`:

- `target` and `connection_string_encrypted` use `EncryptedString` (backed by a SQL `String`).
- `check_config` uses `EncryptedJSON` (backed by SQL `Text`; the whole JSON object is encrypted as one blob, so no field-level SQL querying is possible).

Both use Fernet (AES-128-CBC + HMAC) with the key from `SECURITY__FIELD_ENCRYPTION_KEY`. Encryption/decryption is automatic on write/read. If no key is configured, values are stored as plaintext, and existing plaintext values decrypt as-is to support migration.

## Derived `@property` accessors

These are **not** database columns. They are read-only Python properties on the model that pull individual keys out of the decrypted `check_config` JSON via `get_config(key)`. The corresponding fields are accepted on the Pydantic create/update schemas and packed into `check_config` by the service layer.

| Property | Type | `check_config` key | Used by |
|----------|------|--------------------|---------|
| `http_method` | str \| None | `http_method` | http / json |
| `verify_ssl` | bool \| None | `verify_ssl` | http / json |
| `expected_status` | int \| None | `expected_status` | http / json |
| `json_path` | str \| None | `json_path` | json |
| `expected_value` | str \| None | `expected_value` | json |
| `record_type` | str \| None | `record_type` | dns |
| `nameserver` | str \| None | `nameserver` | dns |
| `expect_value` | str \| None | `expect_value` | dns |
| `port` | int \| None | `port` | tcp / udp |
| `query` | str \| None | `query` | mysql / postgres |

Two more properties are not backed by `check_config`:

- `connection_string` — read/write property wrapping the encrypted `connection_string_encrypted` column.
- `fully_qualified_name` — `"{agent.agent_name}:{display_name}"`, computed from the loaded `agent` relationship.

## Check types

The schema validator (`CheckBase.validate_check_type`) accepts: `ping`, `http`, `https`, `tcp`, `udp`, `dns`, `json`, `mysql`, `postgres`, `grpc`, `websocket`, `synthetic`, `internal`, `unknown`.

However, only **8 of these have functional agent executors** (registered in `apps/agent/app/agent_main.py`): `ping`, `http`, `tcp`, `dns`, `json`, `mysql`, `postgres`, `synthetic`. The remaining strings (`https`, `udp`, `grpc`, `websocket`, `internal`, `unknown`) pass validation but have no executor and will not run — they are reserved/placeholder values.

## Indexes and constraints

Defined on the model (`__table_args__`) and in the baseline migration:

- `PRIMARY KEY (id)`
- `idx_checks_agent_id` on `agent_id`
- `idx_checks_type` on `check_type`
- `idx_checks_depends_on_check_id` on `depends_on_check_id` (model `__table_args__`)
- FK `agent_id → agents.id` `ON DELETE CASCADE`
- FK `depends_on_check_id → checks.id` `ON DELETE SET NULL`

There is no unique constraint on `(agent_id, display_name)`; display names are not required to be unique.

## Relationships

| Attribute | Target | Notes |
|-----------|--------|-------|
| `agent` | `Agent` | Many-to-one; `lazy="selectin"`. |
| `parent_check` | `Check` | Self-referential via `depends_on_check_id`; `lazy="selectin"`. |
| `dependent_checks` | `list[Check]` | Reverse of `parent_check`; `lazy="noload"`. |
| `check_results` | `list[CheckResult]` | `cascade="all, delete-orphan"`, `lazy="noload"` (can be very large). |
| `alert_mappings` | `list[AlertCheckMapping]` | `cascade="all, delete-orphan"`, `lazy="noload"`. |
| `artifacts` | `list[CheckArtifact]` | `cascade="all, delete-orphan"`, `lazy="noload"`. |

## API schema mapping

The Pydantic schemas in `check_schema.py` expose flattened type-specific fields (e.g. `http_method`, `expected_status`, `connection_string`, `query`) alongside the real columns. On create/update these flattened fields are folded into `check_config` (or, for `connection_string`, into the encrypted column); on read they are surfaced from the derived properties. `CheckResponse` additionally adds computed read-only fields `fully_qualified_name`, `latest_status`, `latest_latency_ms`, and `success_rate_24h`.
