# Settings - User Guide

## Overview

The Settings section provides centralized configuration for LuxSwirl. From the Settings landing page you reach every configuration area through a grid of cards.

**Key Features:**
- **Notifications** - Configure notification providers (email, webhook, Home Assistant)
- **Alerts** - Manage alert rules and triggers
- **API Keys & Metrics** - Control agent registration keys and the Prometheus metrics endpoint
- **System Defaults** - Tune check intervals, timeouts, thresholds, security, and retention defaults
- **User Management** - Manage user accounts, roles, and permissions (admin only)
- **Component Library** - Brand/design-token reference (admin only)

**Access:** Click **Settings** in the sidebar, or navigate to `/settings`.

**Permissions:** Settings pages require the **Administrator** role.

> **Two kinds of settings.** Everything described here is a *runtime* setting stored in the database and editable live in the UI — changes take effect immediately. Deployment-level configuration (database URL, CORS origins, secret keys, heartbeat/sync intervals, the initial admin bootstrap) is set via environment variables at deploy time and requires a restart. Those are documented in the [Settings Reference](../reference/settings-reference.md#b-deployment-settings-environment-variables-set-at-deploy-time).

---

## Settings Navigation

The Settings landing page (`/settings`) shows a grid of cards. Each card links to a dedicated page.

### Settings Landing Page

**Path:** `/settings`

**Cards:**
- **Notifications** → `/settings/notifications`
- **Alerts** → `/settings/alerts`
- **API Keys & Metrics** → `/settings/registration-keys`
- **System Defaults** → `/settings/defaults`
- **User Management** → `/settings/users` (admin only)
- **Component Library** → `/settings/components` (admin only)

If statistics are available, an overview row shows counts for Notification Providers, Alert Rules, Registration Keys, and Active Users.

### Notifications

**Path:** `/settings/notifications`

**Purpose:** Configure notification providers (Email, Webhook, Home Assistant).

**Documented in:** [Notifications - User Guide](notifications.md)

**Summary:**
- Create, edit, delete notification providers
- Configure provider-specific settings (SMTP, webhook URLs)
- Enable/disable providers and test configurations

### Alerts

**Path:** `/settings/alerts`

**Purpose:** Manage alert rules and triggers.

**Documented in:** [Alerts - User Guide](alerts.md)

**Summary:**
- Create, edit, delete alert rules
- Configure trigger conditions (status change, latency threshold, SSL expiry)
- Assign alerts to checks (global or specific) and attach notification providers
- Configure recovery notifications

The list supports filtering by enabled and global status, with pagination.

### API Keys & Metrics

**Path:** `/settings/registration-keys`

**Purpose:** Manage agent registration keys and the Prometheus metrics endpoint (both live on this page).

**Detailed below.**

### System Defaults

**Path:** `/settings/defaults`

**Purpose:** Configure database-backed default values for checks, alerts, jobs, the system, security, general display, and metrics.

**Detailed below.**

### User Management

**Path:** `/settings/users` (reached from the **User Management** card on the Settings page; admin only)

**Purpose:** Manage user accounts, roles, and permissions.

**Summary:**
- Create, edit, delete user accounts
- Assign roles (Admin, Editor, Viewer)
- Reset passwords (with optional "must change on next login")
- Enable/disable accounts and unlock locked accounts

The list supports filtering by role, active status, and search, with pagination.

### Component Library

**Path:** `/settings/components` (admin only)

**Purpose:** Brand colors, macros, badges, buttons, and design-token reference. No configuration is stored here.

---

## Registration Keys

Registration keys control agent authentication and approval.

**Access:** Settings → API Keys & Metrics (`/settings/registration-keys`)

### Key Concepts

**Registration Key:**
- Shared secret used during agent first-time registration
- Agents provide the key via the `LUXSWIRL_AUTH_KEY` environment variable
- After registration, the agent receives a unique API key for ongoing authentication

**Workflow:**
1. Admin creates a registration key in the LuxSwirl UI
2. Admin deploys the agent with `LUXSWIRL_AUTH_KEY=<registration_key>`
3. Agent registers with the server using the registration key
4. Agent appears in "Pending Approval"
5. Admin approves the agent
6. Server generates a unique API key for the agent
7. Agent stores the encrypted API key locally and uses it for all subsequent requests

### Registration Keys List

**Table Columns:**
- **Key Name** - Friendly name for identification
- **Key Value** - Actual registration key (partially masked)
- **Usage Count** - Number of agents registered with this key
- **Created** - Creation timestamp
- **Status** - Active (green) or Revoked (gray)
- **Actions** - View, Revoke, Delete

### Creating a Registration Key

1. Navigate to **Settings → API Keys & Metrics**
2. Click **Create Registration Key**
3. **Key Name** (required) — friendly identifier, e.g. "Production Agents"
4. **Key Value** (optional) — leave blank to auto-generate a secure random key; must be unique
5. Click **Create Key**
6. **Copy the key immediately** — it is shown once, then partially masked

**Security Note:** Store registration keys securely. Anyone with the key can register agents.

### Viewing a Registration Key

1. Find the key in the list
2. Click **View** (eye icon) to display the full key in a modal
3. Use the **Copy** button

### Revoking a Registration Key

**Effect:** Prevents *new* agents from registering with this key.

**Does NOT affect:** agents already registered or already holding API keys.

1. Find the key in the list
2. Click **Revoke**
3. Status changes to "Revoked"

Un-revoking is not supported — create a new key instead.

### Deleting a Registration Key

**Warning:** Deletion is permanent.

1. Find the key in the list
2. Click **Delete** and confirm

Agents already registered with the key keep their API keys.

---

## Prometheus Metrics Configuration

Configure the `/metrics` endpoint for Prometheus scraping.

**Access:** Settings → API Keys & Metrics (`/settings/registration-keys`, same page as Registration Keys)

### Metrics Configuration Card

**Settings:**
- **Metrics Endpoint** — enable/disable `/metrics` (`metrics.enabled`, default **enabled**)
- **Require Authentication** — require a Bearer token (`metrics.auth_required`, default **off / public**)
- **Agent Timeout** — seconds before an agent is marked down in metrics (`metrics.agent_timeout_seconds`, default **300**, range 30–3600)
- **Bearer Token** — dedicated token for scraping (`metrics.bearer_token`, empty by default)

### Enabling/Disabling the Metrics Endpoint

- **Enabled** — endpoint active at `/metrics`
- **Disabled** — endpoint returns 404

### Configuring Authentication

**Without authentication (default):** `/metrics` is publicly accessible — recommended for internal/trusted networks.

**With authentication:** `/metrics` requires `Authorization: Bearer <token>`; unauthorized requests return 401 — recommended for public-facing servers.

### Generating a Bearer Token

1. Click **Generate New Token**
2. Copy the token from the modal (shown once)
3. Configure your Prometheus scrape config

Generating a new token invalidates the previous one — update all scrapers.

### Updating Agent Timeout

**Default:** 300 seconds. **Range:** 30–3600 seconds.

1. Enter the new timeout (seconds)
2. Click **Update**

This drives the `luxswirl_agent_up` metric (0 if no report within the timeout, 1 otherwise).

### Prometheus Scrape Configuration

**Without authentication:**
```yaml
scrape_configs:
  - job_name: 'luxswirl'
    static_configs:
      - targets: ['server.example.com:9000']
    metrics_path: '/metrics'
```

**With authentication:**
```yaml
scrape_configs:
  - job_name: 'luxswirl'
    static_configs:
      - targets: ['server.example.com:9000']
    metrics_path: '/metrics'
    authorization:
      type: Bearer
      credentials: 'your-generated-bearer-token-here'
```

### Available Metrics (selected)

- `luxswirl_check_success` - Check success/failure (1/0)
- `luxswirl_check_latency_seconds` - Check response time
- `luxswirl_agent_up` - Agent online status (1/0)

---

## System Defaults

Configure database-backed default values. These are stored in the `settings` table and take effect immediately when saved — no restart needed.

**Access:** Settings → System Defaults (`/settings/defaults`)

### Defaults Page Layout

Settings are grouped by category:
- **Check** defaults
- **Alert** defaults
- **System** behavior
- **Job** defaults
- **Database** retention
- **Security** (grouped by subcategory: Session & Authentication, Rate Limiting, Password Complexity, Network Protection)
- **General** display settings
- **Metrics**

Each setting row shows the display name, description, an input control (text/number/checkbox/dropdown), and Save + Reset actions.

### Setting Types

- **Integer** — number input, validated against a min/max range
- **Boolean** — checkbox toggle
- **String** — text input
- **Enum** — dropdown of predefined options

### Updating a Setting

1. Find the setting in its category section
2. Change the value
3. Click **Save** (checkmark)
4. The value is validated and applied immediately

Invalid values are rejected with an inline error.

### Resetting a Setting

Click **Reset** (curved arrow) to revert a setting to its seeded default.

> The tables below list the real keys and exact defaults. For the authoritative, complete list (every key, type, and validation), see the [Settings Reference](../reference/settings-reference.md#a-runtime-settings-database-backed-editable-in-the-ui).

### Check Defaults

**Category:** `check` — default values for new health checks.

- **Default Interval** (`check.default_interval`, int) — seconds between checks. Default **60**, range 10–86400.
- **Default Timeout** (`check.default_timeout`, int) — max seconds for a check. Default **10**, range 1–300.
- **Default Retry Attempts** (`check.default_retry_attempts`, int) — retries before marking failed. Default **2**, range 0–10.
- **Default Retry Interval** (`check.default_retry_interval`, int) — seconds between retries. Default **30**, range 1–300.
- **Default Expected HTTP Status** (`check.default_expected_status`, int) — Default **200**, range 100–599.
- **Default Verify SSL** (`check.default_verify_ssl`, bool) — Default **false** (allows self-signed certs).
- **Default HTTP Method** (`check.default_http_method`, enum) — GET, POST, PUT, PATCH, DELETE, HEAD. Default **GET**.

New checks inherit these; existing checks are unchanged and users can override per-check.

### Alert Defaults

**Category:** `alert` — defaults for new alert rules.

- **Default Consecutive Failures** (`alert.default_consecutive_failures`, int) — failures before alerting. Default **1**, range 1–100.
- **Default Notify on Recovery** (`alert.default_notify_on_recovery`, bool) — Default **true**.
- **Default Latency Threshold (ms)** (`alert.default_latency_threshold`, int) — Default **1000**, range 1–60000.
- **SSL Certificate Warning Days** (`alert.ssl_cert_warning_days`, int) — Default **30**, range 1–365.
- **SSL Certificate Critical Days** (`alert.ssl_cert_critical_days`, int) — Default **14**, range 1–365.

### System Defaults

**Category:** `system` — core metrics/history/agent-liveness behavior.

- **Metrics TTL (seconds)** (`system.metrics_ttl_seconds`, int) — Default **300**, range 60–3600.
- **Max History Points** (`system.max_history_points`, int) — Default **1000**, range 100–10000.
- **Agent Active Window (minutes)** (`system.agent_active_window_minutes`, int) — Default **10**, range 1–60.

### Job Defaults

**Category:** `job` — defaults for background jobs and network scans.

- **Default Job Timeout (seconds)** (`job.default_timeout_seconds`, int) — Default **300**, range 10–3600.
- **Synthetic Job Timeout (seconds)** (`job.synthetic_timeout_seconds`, int) — Default **600**, range 60–1800.
- **Network Scan Timeout (seconds)** (`job.network_scan_timeout`, int) — per-host timeout. Default **10**, range 1–60.
- **Network Scan Max Concurrent** (`job.network_scan_max_concurrent`, int) — Default **100**, range 1–500.

### Database Defaults

**Category:** `database` — retention and compression.

- **Check Results Retention (days)** (`database.retention_days`, int) — Default **90**, range 7–3650.
- **Compress After (days)** (`database.compress_after_days`, int) — Default **7**, range 1–365.
- **Artifacts Retention (days)** (`database.artifacts_retention_days`, int) — Default **30**, range 1–365.
- **Hourly Aggregates Retention (days)** (`database.hourly_aggregate_retention_days`, int) — Default **365**, range 30–1825.
- **Daily Aggregates Retention (days)** (`database.daily_aggregate_retention_days`, int) — Default **1825**, range 90–3650.

### Security Settings

**Category:** `security` — grouped by subcategory in the UI.

#### Session & Authentication
- **Session Lifetime (days)** (`security.session_lifetime_days`, int) — Default **7**, range 1–90.
- **Maximum Failed Login Attempts** (`security.max_failed_attempts`, int) — Default **5**, range 3–10.
- **Account Lock Duration (minutes)** (`security.account_lock_duration_minutes`, int) — Default **30**, range 5–1440.

#### Rate Limiting
- **Enable Rate Limiting** (`security.rate_limit_enabled`, bool) — Default **true**.
- **Login Rate Limit** (`security.login_rate_limit`, **string**) — `count/period` format. Default **`10/15minutes`**.
- **API Rate Limit** (`security.api_rate_limit`, **string**) — `count/period` format. Default **`100/minute`**.
- **Agent Registration Rate Limit** (`security.registration_rate_limit`, **string**) — `count/period` format. Default **`5/hour`**.

> Rate limits are strings such as `10/15minutes` or `100/minute`, not numbers.

#### Password Complexity
- **Minimum Password Length** (`security.min_password_length`, int) — Default **8**, range 6–32.
- **Require Uppercase Letter** (`security.require_uppercase`, bool) — Default **true**.
- **Require Lowercase Letter** (`security.require_lowercase`, bool) — Default **true**.
- **Require Number** (`security.require_number`, bool) — Default **true**.
- **Require Special Character** (`security.require_special_char`, bool) — Default **false**.
- **Check Against Common Passwords** (`security.check_common_passwords`, bool) — Default **true**.

#### Network Protection (SSRF)
- **Block Cloud Metadata Endpoints** (`security.block_cloud_metadata`, bool) — Default **true**. Blocks checks targeting cloud metadata / link-local addresses (169.254.x.x).
- **Block Private Network Targets** (`security.block_private_networks`, bool) — Default **false**. Blocks RFC 1918 private ranges (10.x, 172.16-31.x, 192.168.x); disabled by default so self-hosted users can monitor internal services.

**How SSRF protection works:** validation runs when a check is created or updated. The target hostname is resolved to IP(s), and each IP is checked against the blocked ranges; blocked targets are rejected with a clear error. Existing checks are not retroactively validated.

### General Settings

**Category:** `general` — display and UI behavior.

- **Default Timezone** (`general.timezone`, enum) — Default **UTC**. Options include UTC and major America/Europe/Asia/Australia zones.
- **Date Format** (`general.date_format`, enum) — `long` / `short` / `iso`. Default **long**.
- **Time Format** (`general.time_format`, enum) — `24h` / `12h`. Default **24h**.
- **Default Page Size** (`general.default_page_size`, int) — one of 10, 25, 50, 100, 200. Default **50**.
- **Dashboard Auto-Refresh Interval (seconds)** (`general.dashboard_refresh_interval`, int) — Default **10**, range 5–300.
- **Default Chart Time Range** (`general.default_chart_time_range`, enum) — 1h/4h/8h/12h/24h/3d/7d. Default **4h**.
- **Agent Stale Threshold (seconds)** (`general.agent_stale_threshold_seconds`, int) — Default **300**, range 60–3600.

### Metrics Settings

**Category:** `metrics` — also surfaced on the API Keys & Metrics page (see [Prometheus Metrics Configuration](#prometheus-metrics-configuration)).

- **Enable Metrics Endpoint** (`metrics.enabled`, bool) — Default **true**.
- **Require Authentication** (`metrics.auth_required`, bool) — Default **false**.
- **Bearer Token** (`metrics.bearer_token`, string) — empty by default.
- **Agent Timeout (seconds)** (`metrics.agent_timeout_seconds`, int) — Default **300**, range 30–3600.

---

## Setting Value Types and Validation

### Value Types

- **Integer (`int`)** — whole number, validated with min/max.
- **Boolean (`bool`)** — true/false checkbox.
- **String (`string`)** — text value (e.g. a rate limit like `100/minute`).

### Validation Rules

**Min/Max (numeric):**
```json
{"min": 1, "max": 3600}
```

**Enum (string or int):**
```json
{"enum": ["GET", "POST", "PUT"]}
```

Invalid values are rejected inline; the change is not saved until valid.

---

## User Management

**Path:** `/settings/users` (User Management card on the Settings page; admin only)

### Users List

The table supports filtering by **role** and **active status**, a **search** box, and pagination. Each row shows the user and actions: edit, reset password, unlock (if locked), and delete.

### Creating a User

1. On the User Management page, click **Create User**
2. Fill in **Username** and **Password** (required)
3. Choose a **Role** (Viewer, Editor, or Admin) — defaults to Viewer
4. Optionally set **Full Name**, **Active**, and **Must Change Password** (defaults to on)
5. Submit

### Editing a User

Use the edit (pencil) action to change role, full name, active status, and the must-change-password flag.

### Resetting a Password

Use the reset-password action to set a new password; **Must Change** defaults to on so the user is prompted to change it at next login.

### Unlocking an Account

After too many failed login attempts (see `security.max_failed_attempts`), an account locks for `security.account_lock_duration_minutes`. An admin can unlock it immediately with the unlock action.

### Deleting a User

Use the delete action and confirm. You cannot delete your own account.

---

## Common Workflows

### Changing the Default Check Interval

**Goal:** New checks should run every 5 minutes instead of 1 minute.

1. Go to **Settings → System Defaults**
2. In the **Check** section, find **Default Interval**
3. Change `60` → `300`
4. Click **Save**

Only new checks are affected; existing checks are unchanged.

### Configuring Prometheus Metrics with Authentication

1. Go to **Settings → API Keys & Metrics**
2. Toggle **Require Authentication** on
3. Click **Generate New Token** and copy it
4. Add the token to your Prometheus scrape config (Bearer credentials)
5. Reload Prometheus and verify: `curl -H "Authorization: Bearer <token>" http://server:9000/metrics`

### Strengthening Password Policy

1. Go to **Settings → System Defaults → Security → Password Complexity**
2. Raise **Minimum Password Length** (e.g. `8` → `16`)
3. Ensure the complexity toggles you want are on (uppercase, lowercase, number; special character is off by default)
4. Keep **Check Against Common Passwords** on
5. Save each setting

New passwords enforce the rules; existing passwords are grandfathered until changed.

### Adjusting Data Retention

1. Go to **Settings → System Defaults → Database**
2. Change **Check Results Retention (days)** (e.g. `90` → `30`)
3. Save

A background job removes data older than the retention period.

---

## Troubleshooting

### Setting Not Saving

- **Validation error** — the value is outside the allowed range/enum; correct it (the error shows inline).
- **Permission denied** — Settings require the Administrator role.
- **Database issue** — check Database Health and retry.

### Reset to Default Not Working

- The current value may already equal the default, so nothing changes.
- Confirm you have the Administrator role.

### Metrics Endpoint Not Working (401 / 404)

1. Confirm the endpoint is enabled (Settings → API Keys & Metrics).
2. If authentication is required, verify the Bearer token in your Prometheus config; test with `curl -H "Authorization: Bearer <token>" http://server:9000/metrics`.
3. Generate a fresh token if unsure and update scrapers.
4. Verify network connectivity / firewall on port 9000.

### Registration Key Not Working

- **Revoked or deleted** — create a new key.
- **Typo** — confirm `LUXSWIRL_AUTH_KEY` matches the key from the UI exactly.

### Account Locked

After repeated failed logins, the account locks for `security.account_lock_duration_minutes`. An admin can unlock it immediately from **Settings → User Management** using the unlock action.

---

## Security Considerations

### Registration Keys

- Use unique, long, random keys (auto-generate recommended).
- Revoke compromised keys and rotate on a schedule.
- Share only via secure channels; never commit to version control.

### Metrics Bearer Tokens

- Tokens are cryptographically random and shown once.
- Rotate regularly and update all scrapers together.
- Without auth, anyone who can reach the endpoint can scrape it — keep it internal.

### Password Complexity

Defaults require at least 8 characters, an uppercase letter, a lowercase letter, and a digit, and reject common passwords. A special character is **not** required by default. Raise the minimum length and enable the special-character requirement for high-security environments.

### Rate Limiting

Rate limits are expressed as `count/period` strings (e.g. login `10/15minutes`, API `100/minute`, registration `5/hour`). Tighten them for extra protection or loosen them for legitimate high-traffic scenarios. When LuxSwirl runs behind a reverse proxy, configure `SECURITY__TRUSTED_PROXY_NETWORKS` (a deployment env var) so the real client IP is used for limiting and audit logs.

### Network Protection (SSRF)

Cloud-metadata blocking is on by default; private-network blocking is off by default so self-hosted users can monitor internal services. Enable **Block Private Networks** in cloud/enterprise environments to keep checks from reaching internal services. Changes apply to new check creation/updates immediately.

---

## Related Documentation

- [Settings Reference](../reference/settings-reference.md) - Complete key/default/validation reference plus deployment env vars
- [Notifications - User Guide](notifications.md) - Notification provider configuration
- [Alerts - User Guide](alerts.md) - Alert rule management
- [Agents - User Guide](agents.md) - Agent registration and approval
- [Checks - User Guide](checks.md) - Check configuration defaults
- [Database Health - User Guide](database-health.md) - Database performance and retention

---

## What's Next?

After configuring settings:

1. **Review Defaults** — audit System Defaults for your use case
2. **Set Up Metrics** — configure Prometheus scraping
3. **Create Registration Keys** — prepare for agent deployments
4. **Configure Security** — set password complexity and rate limits
5. **Add Users** — create accounts and assign roles
6. **Test Changes** — create a test check/alert to verify defaults
