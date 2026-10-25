# Settings Reference

LuxSwirl has **two distinct configuration systems**. This reference documents both.

| System                     | Where it lives                                                                                            | Who sets it                                                                                                                            | When it applies                                     |
| -------------------------- | --------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------- |
| **A. Runtime settings**    | The `settings` database table, seeded from `app/core/default_settings.py` (`DEFAULT_SETTINGS`)            | Administrators, via the web UI at **Settings → System Defaults** (and the dedicated Notifications / Alerts / API Keys & Metrics pages) | Immediately — no restart required                   |
| **B. Deployment settings** | Environment variables, parsed by Pydantic in `app/core/config.py`; template at `.env.example` (repo root) | Operators, at deploy time (`.env` file or container env)                                                                               | At process start — **requires a restart** to change |

> Runtime settings (System A) are the ones an admin edits live in the UI. Deployment settings (System B) are infrastructure-level: database URL, CORS, secret keys, retention/heartbeat intervals, and the initial admin bootstrap. They are *not* editable from the UI/DB.

______________________________________________________________________

## A. Runtime Settings (database-backed, editable in the UI)

All runtime settings are stored in the database and editable at **Settings → System Defaults**. Changes take effect immediately. The tables below are generated directly from `DEFAULT_SETTINGS` in `app/core/default_settings.py` — every key, default, type, and validation rule below matches that source exactly.

The eight categories are: `check`, `alert`, `system`, `job`, `database`, `security`, `general`, `metrics`.

### check

Default values applied when creating new health checks.

| Key                             | Default | Type   | Allowed / Validation                      | Description                                                                            |
| ------------------------------- | ------- | ------ | ----------------------------------------- | -------------------------------------------------------------------------------------- |
| `check.default_interval`        | 60      | int    | 10–86400                                  | Default interval in seconds between check executions                                   |
| `check.default_timeout`         | 10      | int    | 1–300                                     | Default timeout in seconds for check execution                                         |
| `check.default_retry_attempts`  | 2       | int    | 0–10                                      | Default number of retry attempts for a single check execution before marking as failed |
| `check.default_retry_interval`  | 30      | int    | 1–300                                     | Default wait time in seconds between retry attempts                                    |
| `check.default_expected_status` | 200     | int    | 100–599                                   | Default expected HTTP status code for HTTP checks                                      |
| `check.default_verify_ssl`      | false   | bool   | —                                         | Default SSL certificate verification setting (false for self-signed certs)             |
| `check.default_http_method`     | GET     | string | enum: GET, POST, PUT, PATCH, DELETE, HEAD | Default HTTP method for HTTP checks                                                    |

### alert

Default notification and alerting behavior for new alert rules.

| Key                                  | Default | Type | Allowed / Validation | Description                                                               |
| ------------------------------------ | ------- | ---- | -------------------- | ------------------------------------------------------------------------- |
| `alert.default_consecutive_failures` | 1       | int  | 1–100                | Default number of consecutive failures before triggering alert            |
| `alert.default_notify_on_recovery`   | true    | bool | —                    | Send notification when service recovers (comes back up)                   |
| `alert.default_latency_threshold`    | 1000    | int  | 1–60000              | Default latency threshold in milliseconds for threshold alerts            |
| `alert.ssl_cert_warning_days`        | 30      | int  | 1–365                | Trigger warning when SSL certificate expires within this many days        |
| `alert.ssl_cert_critical_days`       | 14      | int  | 1–365                | Trigger critical alert when SSL certificate expires within this many days |

### system

Core system behavior for metrics caching, history, and agent liveness.

| Key                                  | Default | Type | Allowed / Validation | Description                                                             |
| ------------------------------------ | ------- | ---- | -------------------- | ----------------------------------------------------------------------- |
| `system.metrics_ttl_seconds`         | 300     | int  | 60–3600              | How long to keep check results in metrics before considering them stale |
| `system.max_history_points`          | 1000    | int  | 100–10000            | Maximum number of historical data points to return per check            |
| `system.agent_active_window_minutes` | 10      | int  | 1–60                 | Consider agent active if heartbeat seen within this many minutes        |

### job

Defaults for background jobs and network discovery scans.

| Key                               | Default | Type | Allowed / Validation | Description                                                    |
| --------------------------------- | ------- | ---- | -------------------- | -------------------------------------------------------------- |
| `job.default_timeout_seconds`     | 300     | int  | 10–3600              | Default timeout for job execution (5 minutes)                  |
| `job.synthetic_timeout_seconds`   | 600     | int  | 60–1800              | Timeout for synthetic monitoring jobs (10 minutes)             |
| `job.network_scan_timeout`        | 10      | int  | 1–60                 | Timeout per host in network discovery scans                    |
| `job.network_scan_max_concurrent` | 100     | int  | 1–500                | Maximum number of parallel host scans during network discovery |

### database

Data retention and compression policies for check results and aggregates.

| Key                                        | Default | Type | Allowed / Validation | Description                                                                                 |
| ------------------------------------------ | ------- | ---- | -------------------- | ------------------------------------------------------------------------------------------- |
| `database.retention_days`                  | 90      | int  | 7–3650               | Keep check results for this many days before deletion. Older data is automatically removed. |
| `database.compress_after_days`             | 7       | int  | 1–365                | Compress check results older than this many days. Compression saves 80-90% space.           |
| `database.artifacts_retention_days`        | 30      | int  | 1–365                | Keep check artifacts (screenshots, logs) for this many days before deletion.                |
| `database.hourly_aggregate_retention_days` | 365     | int  | 30–1825              | Keep hourly aggregated metrics for this many days (1 year default).                         |
| `database.daily_aggregate_retention_days`  | 1825    | int  | 90–3650              | Keep daily aggregated metrics for this many days (5 years default).                         |

### security

Authentication, rate limiting, password policy, and SSRF network protection. The UI groups these by subcategory (Session & Authentication, Rate Limiting, Password Complexity, Network Protection).

> Note: `security.login_rate_limit`, `security.api_rate_limit`, and `security.registration_rate_limit` are **strings** in `count/period` form (e.g. `10/15minutes`), not integers.

#### Session & Authentication

| Key                                      | Default | Type | Allowed / Validation | Description                                                      |
| ---------------------------------------- | ------- | ---- | -------------------- | ---------------------------------------------------------------- |
| `security.session_lifetime_days`         | 7       | int  | 1–90                 | How long users stay logged in before requiring re-authentication |
| `security.max_failed_attempts`           | 5       | int  | 3–10                 | Number of failed login attempts before account locks             |
| `security.account_lock_duration_minutes` | 30      | int  | 5–1440               | How long accounts remain locked after too many failed attempts   |

#### Rate Limiting

| Key                                | Default        | Type   | Allowed / Validation | Description                                                                     |
| ---------------------------------- | -------------- | ------ | -------------------- | ------------------------------------------------------------------------------- |
| `security.rate_limit_enabled`      | true           | bool   | —                    | Enable rate limiting on authentication endpoints to prevent brute-force attacks |
| `security.login_rate_limit`        | `10/15minutes` | string | format: count/period | Rate limit for login attempts per IP address (e.g., `10/15minutes`)             |
| `security.api_rate_limit`          | `100/minute`   | string | format: count/period | Rate limit for general API requests per IP address (e.g., `100/minute`)         |
| `security.registration_rate_limit` | `5/hour`       | string | format: count/period | Rate limit for agent registration per IP address (e.g., `5/hour`)               |

#### Password Complexity

| Key                               | Default | Type | Allowed / Validation | Description                                                                         |
| --------------------------------- | ------- | ---- | -------------------- | ----------------------------------------------------------------------------------- |
| `security.min_password_length`    | 8       | int  | 6–32                 | Minimum number of characters required for passwords                                 |
| `security.require_uppercase`      | true    | bool | —                    | Require at least one uppercase letter in passwords                                  |
| `security.require_lowercase`      | true    | bool | —                    | Require at least one lowercase letter in passwords                                  |
| `security.require_number`         | true    | bool | —                    | Require at least one digit in passwords                                             |
| `security.require_special_char`   | false   | bool | —                    | Require at least one special character (`!@#$%^&*()_+-=[]{}\|;:,.<>?`) in passwords |
| `security.check_common_passwords` | true    | bool | —                    | Reject passwords that appear in common password lists                               |

#### Network Protection (SSRF)

Controls which network targets a health check may point at, to prevent Server-Side Request Forgery. The target hostname is resolved to IP(s) and each IP is checked against the blocked ranges. Validation runs in **two places**: on the server when a check is created or updated, and **again on the agent at fetch time** (immediately before each connection, on every redirect hop for HTTP/JSON). The fetch-time check is what defeats DNS-rebinding and HTTP-redirect bypasses — a target that resolved to a safe IP at create time, then later resolves (or redirects) to the cloud-metadata endpoint, is blocked at the moment the agent would connect. Every address a check connects to is validated: the target, a DNS check's nameserver and a database check's connection string. The agent enforces `security.block_private_networks` as the server sets it (the checks list and every heartbeat response carry it, so a change reaches an agent within one heartbeat); the cloud-metadata range is always enforced on the agent regardless of the server toggle. Network scan jobs never reach the cloud-metadata range either (a subnet that overlaps it is refused), but they are not held to `security.block_private_networks`: sweeping private networks is their purpose.

> Residual: the agent validates the resolved IP immediately before connecting, closing the create→fetch window. A sub-millisecond TTL-0 rebind between that check and the socket's own resolution is not closed (it would require pinning the validated IP through TLS/SNI) and is tracked as a future hardening item.

| Key                               | Default | Type | Allowed / Validation | Description                                                                                                                                                                                                                                              |
| --------------------------------- | ------- | ---- | -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `security.block_cloud_metadata`   | true    | bool | —                    | Block checks from targeting cloud metadata and link-local addresses (169.254.x.x, fe80::/10, fd00:ec2::254, 100.100.100.200). Prevents metadata exfiltration in cloud/container environments.                                                            |
| `security.block_private_networks` | false   | bool | —                    | Block checks from targeting private and internal addresses: RFC 1918 (10.x, 172.16-31.x, 192.168.x), IPv6 unique-local, loopback (127.x, ::1), 0.0.0.0 and CGNAT (100.64.x). Disabled by default since most self-hosted users monitor internal services. |

### general

Display and UI behavior defaults.

| Key                                     | Default | Type   | Allowed / Validation                                                                                                                                                                                                             | Description                                                                           |
| --------------------------------------- | ------- | ------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| `general.timezone`                      | UTC     | string | enum: UTC, America/New_York, America/Chicago, America/Denver, America/Los_Angeles, America/Phoenix, America/Anchorage, Pacific/Honolulu, Europe/London, Europe/Paris, Europe/Berlin, Asia/Tokyo, Asia/Shanghai, Australia/Sydney | Default timezone for displaying dates and times across the application.               |
| `general.date_format`                   | long    | string | enum: long, short, iso                                                                                                                                                                                                           | How dates are displayed (long: November 8, 2025 / short: 11/8/2025 / iso: 2025-11-08) |
| `general.time_format`                   | 24h     | string | enum: 24h, 12h                                                                                                                                                                                                                   | Format for displaying times.                                                          |
| `general.default_page_size`             | 50      | int    | enum: 10, 25, 50, 100, 200                                                                                                                                                                                                       | Default number of items to show per page in lists and tables.                         |
| `general.dashboard_refresh_interval`    | 10      | int    | 5–300                                                                                                                                                                                                                            | How often the status dashboard automatically refreshes                                |
| `general.default_chart_time_range`      | 4h      | string | enum: 1h, 4h, 8h, 12h, 24h, 3d, 7d                                                                                                                                                                                               | Default time range for performance charts                                             |
| `general.agent_stale_threshold_seconds` | 300     | int    | 60–3600                                                                                                                                                                                                                          | Highlight agents in yellow if not seen for this many seconds                          |

### metrics

Prometheus `/metrics` endpoint configuration. Editable at **Settings → API Keys & Metrics**.

| Key                             | Default | Type   | Allowed / Validation | Description                                                                                |
| ------------------------------- | ------- | ------ | -------------------- | ------------------------------------------------------------------------------------------ |
| `metrics.enabled`               | true    | bool   | —                    | Enable or disable the /metrics endpoint entirely                                           |
| `metrics.auth_required`         | false   | bool   | —                    | Require bearer token authentication for /metrics endpoint (default: public for Prometheus) |
| `metrics.bearer_token`          | (empty) | string | —                    | The only bearer token /metrics accepts when authentication is required                     |
| `metrics.agent_timeout_seconds` | 300     | int    | 30–3600              | Seconds without heartbeat before agent is marked as down in luxswirl_agent_up metric       |

______________________________________________________________________

## B. Deployment Settings (environment variables, set at deploy time)

These are parsed by Pydantic in `app/core/config.py` and are **not** stored in the database or editable in the UI. Set them in your `.env` file or container environment; see `.env.example` at the repo root for a starter template. **Changing any of these requires a process/container restart.**

Environment variables use a `<GROUP>__<FIELD>` convention (double underscore nested delimiter): `SERVER__`, `DATABASE__`, `SECURITY__`, `LOG__`.

### First admin and recovery

No credential ships. The first admin comes from one of two paths, decided at startup **only while the users table is empty**; once any user exists both are ignored and the database is authoritative.

| Env Variable                  | Default | Purpose                                                                                                     |
| ----------------------------- | ------- | ----------------------------------------------------------------------------------------------------------- |
| `INITIAL_ADMIN_USERNAME`      | none    | Username of the first admin. Letters, digits, `.`, `_`, `-`.                                                |
| `INITIAL_ADMIN_PASSWORD`      | none    | Its password (12+ characters). The admin must change it at first sign-in.                                   |
| `INITIAL_ADMIN_PASSWORD_FILE` | none    | Read the password from this file (a Docker secret) instead. Set this or `INITIAL_ADMIN_PASSWORD`, not both. |

- **Both set:** the admin is created at first boot.
- **Unset, or rejected** (too short, a bad username, an unreadable file): startup does not fail. The server logs why, opens `/setup`, and prints a **one-time setup token** in its log (`admin_bootstrap … open /setup and enter the token …`). `/setup` asks for that token, so only someone who can read the server log can claim a fresh install. `/setup` returns 404 once any user exists.
- **Locked out:** `docker compose exec luxswirl_server python -m app.cli reset-password <username>` prints a temporary password once; it must be changed at the next sign-in. No email is involved.

### Trusted hosts (`ALLOWED_HOSTS`)

| Env Variable    | Default             | Purpose                                                                                                                                                                                                                                                                                                                                |
| --------------- | ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ALLOWED_HOSTS` | none (**required**) | Comma-separated Host names the server answers; any other `Host` header gets 400. List the public hostname (no scheme, no port) **and** `luxswirl_server` (plus `STACK_SUFFIX`, if set), the name the agent posts to inside the compose network. `*.example.com` admits that domain's subdomains. Unset, empty or `*` refuses to start. |

The container's own healthcheck (`http://localhost:9000/health` from inside the container) passes without `localhost` being listed; any request that arrives through a proxy is checked.

### Server (`SERVER__`)

| Env Variable                                  | Default             | Purpose                                                                                                                                                                        |
| --------------------------------------------- | ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `SERVER__CORS_ORIGINS`                        | `[]` (**required**) | **Must be set.** JSON list of allowed CORS origins — the exact public URL(s) users hit in the browser, e.g. `'["https://luxswirl.example.com:9000"]'`. No safe default exists. |
| `SERVER__HOST`                                | `0.0.0.0`           | Host/interface to bind to                                                                                                                                                      |
| `SERVER__PORT`                                | `9000`              | Port to listen on (1–65535)                                                                                                                                                    |
| `SERVER__ENVIRONMENT`                         | `development`       | `development` \| `staging` \| `production`                                                                                                                                     |
| `SERVER__WORKERS`                             | `1`                 | Number of worker processes (1–16)                                                                                                                                              |
| `SERVER__RELOAD`                              | `false`             | Auto-reload (development only)                                                                                                                                                 |
| `SERVER__DEFAULT_RETENTION_DAYS`              | `90`                | Default data retention in days (1–3650)                                                                                                                                        |
| `SERVER__DEFAULT_HEARTBEAT_INTERVAL`          | `5`                 | Default agent heartbeat interval in seconds (1–600)                                                                                                                            |
| `SERVER__DEFAULT_CHECK_SYNC_INTERVAL`         | `60`                | Default check sync interval in seconds (10–600)                                                                                                                                |
| `SERVER__AGENT_ACTIVE_WINDOW_MINUTES`         | `10`                | Consider agent active if seen within this many minutes (1–1440)                                                                                                                |
| `SERVER__LATEST_RESULTS_WINDOW_MINUTES`       | `5`                 | Show latest results from the last N minutes (1–60)                                                                                                                             |
| `SERVER__METRICS_TTL_SECONDS`                 | `300`               | Metrics considered stale after N seconds (30–3600)                                                                                                                             |
| `SERVER__JOB_RETENTION_DAYS`                  | `7`                 | How long to keep completed jobs before auto-purge (1–365)                                                                                                                      |
| `SERVER__JOB_PURGE_INTERVAL_HOURS`            | `1`                 | How often the job cleanup task runs (1–24)                                                                                                                                     |
| `SERVER__JOB_MAX_DISPATCH_PER_HEARTBEAT`      | `10`                | Max jobs dispatched per heartbeat (1–100)                                                                                                                                      |
| `SERVER__DATABASE_MAINTENANCE_INTERVAL_HOURS` | `24`                | How often database maintenance (VACUUM, bloat cleanup) runs (1–168)                                                                                                            |

CORS credentials/methods/headers are also configurable (`SERVER__CORS_CREDENTIALS` default `true`, `SERVER__CORS_METHODS`/`SERVER__CORS_HEADERS` default `["*"]`).

### Database (`DATABASE__`)

| Env Variable              | Default                                                          | Purpose                                |
| ------------------------- | ---------------------------------------------------------------- | -------------------------------------- |
| `DATABASE__URL`           | `postgresql+asyncpg://luxswirl:luxswirl@localhost:5432/luxswirl` | Database connection URL (async driver) |
| `DATABASE__ECHO`          | `false`                                                          | Echo SQL statements (debug)            |
| `DATABASE__POOL_SIZE`     | `20`                                                             | Connection pool size (1–100)           |
| `DATABASE__MAX_OVERFLOW`  | `10`                                                             | Max overflow connections (0–50)        |
| `DATABASE__POOL_PRE_PING` | `true`                                                           | Verify connections before use          |

### Security (`SECURITY__`)

Secrets (`SECURITY__SECRET_KEY`, `SECURITY__FIELD_ENCRYPTION_KEY`) are auto-generated on first boot and persisted under `/app/data` if not provided. Set them only when injecting from a secrets manager or for multi-host deployments.

There is no shared API token. Agents authenticate with a registration key and then their own agent key; `/metrics` takes its own optional bearer token (`metrics.bearer_token`, Settings > API Keys). `SECURITY__AUTH_TOKENS` and the `/app/data/api_token` file earlier versions generated are no longer read, and both can be removed.

| Env Variable                            | Default                                                                   | Purpose                                                                                                                                                                                                             |
| --------------------------------------- | ------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `SECURITY__SECRET_KEY`                  | `""` (auto-generated)                                                     | JWT signing secret. Resolved at startup: env → `/app/data/secret_key` → generate.                                                                                                                                   |
| `SECURITY__FIELD_ENCRYPTION_KEY`        | `""` (auto-generated)                                                     | Fernet key for encrypting sensitive DB fields (check targets, check_config, connection strings). Auto-generated and persisted to `/app/data/field_encryption_key` if unset; must be a valid Fernet key if provided. |
| `SECURITY__AUTH_ENABLED`                | `true`                                                                    | Enable authentication                                                                                                                                                                                               |
| `SECURITY__ALGORITHM`                   | `HS256`                                                                   | JWT algorithm                                                                                                                                                                                                       |
| `SECURITY__ACCESS_TOKEN_EXPIRE_MINUTES` | `30`                                                                      | Access token expiry in minutes (5–43200)                                                                                                                                                                            |
| `SECURITY__RATE_LIMIT_ENABLED`          | `true`                                                                    | Enable rate limiting on auth endpoints                                                                                                                                                                              |
| `SECURITY__LOGIN_RATE_LIMIT`            | `10/15minutes`                                                            | Login rate limit per IP (`count/period`)                                                                                                                                                                            |
| `SECURITY__API_RATE_LIMIT`              | `100/minute`                                                              | API rate limit per IP (`count/period`)                                                                                                                                                                              |
| `SECURITY__AGENT_API_RATE_LIMIT`        | `600/minute`                                                              | Per-IP limit on agent-authenticated routes (heartbeat, reports, check list, job results); bounds agent-key guessing                                                                                                 |
| `SECURITY__REGISTRATION_RATE_LIMIT`     | `5/hour`                                                                  | Agent registration rate limit per IP (`count/period`)                                                                                                                                                               |
| `SECURITY__TRUSTED_PROXY_NETWORKS`      | `["127.0.0.0/8","::1/128","10.0.0.0/8","172.16.0.0/12","192.168.0.0/16"]` | JSON list of trusted reverse-proxy CIDRs. `X-Forwarded-For` is honored only when the direct TCP peer is within one of these networks (used for rate-limiting + audit client IP).                                    |
| `SECURITY__SESSION_COOKIE_NAME`         | `luxswirl_session`                                                        | Session cookie name (make unique if multiple apps share a subdomain)                                                                                                                                                |
| `SECURITY__SESSION_COOKIE_HTTPONLY`     | `true`                                                                    | HTTPOnly flag (blocks JS access)                                                                                                                                                                                    |
| `SECURITY__SESSION_COOKIE_SECURE`       | `true`                                                                    | Secure flag (HTTPS only)                                                                                                                                                                                            |
| `SECURITY__SESSION_COOKIE_SAMESITE`     | `lax`                                                                     | SameSite attribute (`strict`/`lax`/`none`)                                                                                                                                                                          |
| `SECURITY__SESSION_COOKIE_PATH`         | `/`                                                                       | Cookie path                                                                                                                                                                                                         |

> Note: the per-route limits are read from these environment settings once, at startup. The `security.*` rate-limit values on the Settings page are stored but not consulted by the limiter, and `SECURITY__RATE_LIMIT_ENABLED` does not switch it off.

### Logging (`LOG__`)

Both services write one JSON object per line to stdout (the fleet logging module). Every caller field sits under `attributes`, and every line passes through the credential redactor.

| Env Variable         | Default                                                                        | Purpose                                                                                  |
| -------------------- | ------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------- |
| `LOG__LEVEL`         | `INFO`                                                                         | Root level: `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` \| `CRITICAL` (agent reads it too) |
| `LOG__MODULE_LEVELS` | `{"luxswirl.services.check":"ERROR","luxswirl.services.check_result":"ERROR"}` | Per-logger levels (server), applied over the built-in third-party defaults               |
| `LOG_MODULE_LEVELS`  | —                                                                              | Per-logger levels as JSON, applied last: turn one module up at runtime without a deploy  |
| `LOG_FORMAT`         | `json`                                                                         | `text` gives a readable line for local development; anything that ships uses JSON        |

`LOG__FORMAT`, `LOG__ENABLE_FILE_LOG`, `LOG__LOG_DIR`, `LOG__MAX_BYTES` and `LOG__BACKUP_COUNT` are accepted by the settings model but nothing reads them: there is no file logging, and the line format is fixed.

### Trusted Proxy Networks — what this controls

When LuxSwirl runs behind a reverse proxy (nginx, traefik, k8s ingress, AWS ALB, …), the direct TCP peer is the proxy — not the real client. `SECURITY__TRUSTED_PROXY_NETWORKS` tells LuxSwirl which TCP peers it can trust to set `X-Forwarded-For`; the leftmost-untrusted hop in the chain is then used as the real client IP for rate limiting and audit logging.

- **Default** covers Docker bridge networks, RFC 1918 private ranges, and loopback.
- **Tighten** to your proxy's exact CIDR in production (e.g. a single AWS ALB subnet).
- **Clear** (`[]`) if LuxSwirl is exposed directly with no proxy — `X-Forwarded-For` is then ignored entirely and `request.client.host` is the real client.
- **Security note:** an attacker reaching the FastAPI port directly (bypassing the proxy) would otherwise be able to spoof `X-Forwarded-For` to choose any rate-limit bucket. The trusted-proxy gating prevents that — XFF is only honored when the direct peer is itself within `trusted_proxy_networks`.
