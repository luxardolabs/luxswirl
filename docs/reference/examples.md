# Configuration Examples

**Copy-paste ready configurations for common monitoring scenarios.**


---

## Table of Contents

1. [Schema Overview](#schema-overview)
2. [HTTP Checks](#http-checks)
3. [JSON API Checks](#json-api-checks)
4. [TCP & Ping Checks](#tcp--ping-checks)
5. [Database Checks](#database-checks)
6. [DNS Checks](#dns-checks)
7. [Synthetic Checks](#synthetic-checks)
8. [Alert Configurations](#alert-configurations)
9. [Notification Providers](#notification-providers)
10. [Import/Export Patterns](#importexport-patterns)
11. [Status Page Examples](#status-page-examples)
12. [Complete Monitoring Stacks](#complete-monitoring-stacks)
13. [Tips and Best Practices](#tips-and-best-practices)

---

## Schema Overview

LuxSwirl checks use a **flat schema** — there is no nested `config` object. Check-type-specific fields live directly on the check body and are packed into storage automatically based on `check_type`.

There are two surfaces with slightly different key names:

**REST API (`POST /api/v1/agents/{agent_id}/checks`)** — uses `display_name`, `interval_seconds`, `timeout_seconds`:

| Field | Type | Applies to | Notes |
|-------|------|------------|-------|
| `display_name` | string | all | Required |
| `check_type` | string | all | `ping`, `http`, `tcp`, `json`, `dns`, `mysql`, `postgres`, `synthetic` |
| `target` | string | all | Required |
| `description` | string | all | Optional |
| `interval_seconds` | int | all | 1–86400 |
| `timeout_seconds` | int | all | 1–300, default 10 |
| `enabled` | bool | all | Default true |
| `retry_attempts` | int | all | 0–10 |
| `tags` | string[] | all | Optional |
| `http_method` | string | http, json | e.g. `GET`, `POST` |
| `verify_ssl` | bool | http, json | |
| `expected_status` | int | http, json | 100–599 |
| `json_path` | string | json | JSONata expression |
| `expected_value` | string | json | Compared against `json_path` result |
| `record_type` | string | dns | `A`, `AAAA`, `CNAME`, `MX`, `TXT`, `NS`, `PTR`, `SOA`, `SRV`, `CAA` |
| `nameserver` | string | dns | Default `1.1.1.1` |
| `port` | int | dns, tcp | DNS default 53; required for TCP |
| `expect_value` | string | dns | DNS expected record value (note: `expect_value`, not `expected_value`) |
| `connection_string` | string | mysql, postgres | Optional; falls back to `target` |
| `query` | string | mysql, postgres | Default `SELECT 1` |
| `script_code` | string | synthetic | Python/Playwright script |

**Import/Export JSON** — uses `name` plus `interval`/`timeout` (instead of `display_name`/`interval_seconds`/`timeout_seconds`). Supported keys: `name`, `check_type`, `target`, `interval`, `timeout`, `retry_attempts`, `enabled`, `description`, `http_method`, `expected_status`, `json_path`, `expected_value`, `tags`.

> **Note on JSON comparisons:** the agent supports a `comparison_type` (`equals`, `contains`, `regex`, `greater_than`/`gt`, `less_than`/`lt`, `not_equals`/`ne`) that defaults to `equals`. It is applied agent-side; the create/import schema does not persist a custom comparison, so JSON checks created through the API/import use `equals`.

---

## HTTP Checks

### Simple Website Uptime

**Use case**: Monitor public website availability

```json
{
  "display_name": "Company Website",
  "check_type": "http",
  "target": "https://www.example.com",
  "interval_seconds": 60,
  "timeout_seconds": 30,
  "http_method": "GET",
  "expected_status": 200,
  "verify_ssl": true
}
```

---

### API Health Check

**Use case**: Monitor an API health endpoint

```json
{
  "display_name": "Production API Health",
  "check_type": "http",
  "target": "https://api.example.com/health",
  "interval_seconds": 30,
  "timeout_seconds": 10,
  "http_method": "GET",
  "expected_status": 200,
  "verify_ssl": true,
  "tags": ["production", "api"]
}
```

---

### POST Endpoint

**Use case**: Monitor a webhook or data-submission endpoint that accepts POST

```json
{
  "display_name": "Webhook Endpoint",
  "check_type": "http",
  "target": "https://api.example.com/webhooks/test",
  "interval_seconds": 300,
  "timeout_seconds": 15,
  "http_method": "POST",
  "expected_status": 202,
  "verify_ssl": true
}
```

---

### Self-Signed / Internal HTTPS

**Use case**: Monitor an internal endpoint with a self-signed certificate

```json
{
  "display_name": "Internal Admin Dashboard",
  "check_type": "http",
  "target": "https://admin.internal.example.com/dashboard",
  "interval_seconds": 120,
  "timeout_seconds": 20,
  "http_method": "GET",
  "expected_status": 200,
  "verify_ssl": false
}
```

> SSL certificate metadata (issuer, subject, expiry) is collected automatically for any HTTPS target and surfaced on the check detail view — no separate certificate check or extra fields are required.

---

## JSON API Checks

JSON checks fetch a URL, parse the response as JSON, and evaluate a **JSONata** expression (`json_path`) against `expected_value`. The default comparison is equality.

### Simple Field Validation

**API Response**:
```json
{
  "status": "healthy",
  "version": "2.1.0",
  "uptime": 86400
}
```

**Check Configuration**:
```json
{
  "display_name": "API Status Check",
  "check_type": "json",
  "target": "https://api.example.com/status",
  "interval_seconds": 60,
  "timeout_seconds": 10,
  "http_method": "GET",
  "expected_status": 200,
  "json_path": "status",
  "expected_value": "healthy"
}
```

---

### Nested Field Extraction

**API Response**:
```json
{
  "status": "ok",
  "checks": {
    "database": { "connected": true, "latency_ms": 12 },
    "cache": { "connected": true, "hit_rate": 0.95 }
  }
}
```

**Check Configuration**:
```json
{
  "display_name": "Database Connection Check",
  "check_type": "json",
  "target": "https://api.example.com/health",
  "interval_seconds": 60,
  "timeout_seconds": 10,
  "json_path": "checks.database.connected",
  "expected_value": "true"
}
```

---

### JSONata Functions and Filters

**Use case**: Count running services

**API Response**:
```json
{
  "services": [
    {"name": "api", "status": "running"},
    {"name": "worker", "status": "running"},
    {"name": "scheduler", "status": "running"}
  ]
}
```

**Check Configuration**:
```json
{
  "display_name": "Active Services Count",
  "check_type": "json",
  "target": "https://api.example.com/services",
  "interval_seconds": 120,
  "timeout_seconds": 15,
  "json_path": "$count(services[status='running'])",
  "expected_value": "3"
}
```

> `json_path` accepts the full JSONata query language (https://jsonata.org): dotted paths, array indexing (`users[0].name`), quoted keys (`printers."a.b".status`), wildcards (`servers.*.status`), filters (`printers[status='online']`), and functions (`$count`, `$sum`). `expected_value` is supplied as a string; the agent coerces it to match the type of the resolved value (e.g. `"true"` for booleans, `"3"` for numbers).

---

## TCP & Ping Checks

### TCP Port Check

**Use case**: Verify a service port is accepting connections. `target` is the host; the port is a separate `port` field.

```json
{
  "display_name": "Redis Port",
  "check_type": "tcp",
  "target": "redis.example.com",
  "port": 6379,
  "interval_seconds": 60,
  "timeout_seconds": 5
}
```

---

### Ping (ICMP) Check

**Use case**: Verify host reachability. `target` must be a hostname or IP without a protocol prefix.

```json
{
  "display_name": "Gateway Reachability",
  "check_type": "ping",
  "target": "192.168.1.1",
  "interval_seconds": 60,
  "timeout_seconds": 5
}
```

---

## Database Checks

Database checks connect using a DSN (provided as `target`, or optionally `connection_string`) and run `query` (defaults to `SELECT 1`). Results report row count and latency, not row contents.

### MySQL Health Query

```json
{
  "display_name": "MySQL Replica",
  "check_type": "mysql",
  "target": "mysql://monitor:password@replica.example.com:3306/mysql",
  "interval_seconds": 60,
  "timeout_seconds": 10,
  "query": "SELECT 1"
}
```

**Note**: Create a read-only monitoring user:
```sql
CREATE USER 'monitor'@'%' IDENTIFIED BY 'secure_password';
GRANT REPLICATION CLIENT ON *.* TO 'monitor'@'%';
FLUSH PRIVILEGES;
```

MySQL/MariaDB DSNs must use the `mysql://` or `mariadb://` scheme.

---

### MySQL Table Row Count

```json
{
  "display_name": "Recent Orders",
  "check_type": "mysql",
  "target": "mysql://monitor:password@db.example.com:3306/production",
  "interval_seconds": 300,
  "timeout_seconds": 15,
  "query": "SELECT id FROM orders WHERE created_at > NOW() - INTERVAL 5 MINUTE"
}
```

> The check passes if the query executes successfully. Row count and latency are recorded as metrics; thresholds on those values are applied via Alerts (see below), not in the check body.

---

### PostgreSQL Connection Count

```json
{
  "display_name": "PostgreSQL Connections",
  "check_type": "postgres",
  "target": "postgresql://monitor:password@db.example.com:5432/production",
  "interval_seconds": 60,
  "timeout_seconds": 10,
  "query": "SELECT count(*) FROM pg_stat_activity WHERE datname = 'production'"
}
```

PostgreSQL DSNs must use the `postgres://` or `postgresql://` scheme.

---

### PostgreSQL Replication Lag

```json
{
  "display_name": "PostgreSQL Replica Lag",
  "check_type": "postgres",
  "target": "postgresql://monitor:password@replica.example.com:5432/postgres",
  "interval_seconds": 60,
  "timeout_seconds": 10,
  "query": "SELECT EXTRACT(EPOCH FROM (NOW() - pg_last_xact_replay_timestamp()))::INT AS lag_seconds"
}
```

---

## DNS Checks

DNS checks require `record_type`. The expected record value uses the field **`expect_value`** (not `expected_value`). `nameserver` defaults to `1.1.1.1` and `port` defaults to `53`.

### A Record Validation

```json
{
  "display_name": "DNS A Record",
  "check_type": "dns",
  "target": "www.example.com",
  "interval_seconds": 300,
  "timeout_seconds": 10,
  "record_type": "A",
  "expect_value": "93.184.216.34",
  "nameserver": "8.8.8.8"
}
```

---

### MX Record Validation

```json
{
  "display_name": "Email MX Records",
  "check_type": "dns",
  "target": "example.com",
  "interval_seconds": 3600,
  "timeout_seconds": 10,
  "record_type": "MX",
  "expect_value": "mail.example.com",
  "nameserver": "8.8.8.8"
}
```

---

### TXT Record (SPF) Validation

`expect_value` matching is a case-insensitive substring test against each returned record.

```json
{
  "display_name": "SPF Record Check",
  "check_type": "dns",
  "target": "example.com",
  "interval_seconds": 86400,
  "timeout_seconds": 10,
  "record_type": "TXT",
  "expect_value": "include:_spf.google.com",
  "nameserver": "8.8.8.8"
}
```

---

## Synthetic Checks

Synthetic checks run a Python/Playwright script supplied in `script_code`. The script **must define an async function `run_check(page)`** that returns a dict with at least a `status` key (`"success"` or `"failure"`). The Playwright `page` object, `time`, and `re` are available in scope.

### Simple Login Flow

```json
{
  "display_name": "Login Flow Test",
  "check_type": "synthetic",
  "target": "https://app.example.com/login",
  "interval_seconds": 300,
  "timeout_seconds": 60,
  "script_code": "async def run_check(page):\n    steps = []\n    await page.goto('https://app.example.com/login')\n    steps.append('opened login page')\n    await page.fill('#email', 'test@example.com')\n    await page.fill('#password', 'test_password_here')\n    await page.click('button[type=submit]')\n    await page.wait_for_selector('.dashboard-header', timeout=10000)\n    steps.append('reached dashboard')\n    title = await page.title()\n    if title != 'Dashboard - Example App':\n        return {'status': 'failure', 'steps': steps, 'errors': [f'unexpected title: {title}']}\n    return {'status': 'success', 'steps': steps}\n"
}
```

For readability, here is the same `script_code` un-escaped:

```python
async def run_check(page):
    steps = []
    await page.goto('https://app.example.com/login')
    steps.append('opened login page')
    await page.fill('#email', 'test@example.com')
    await page.fill('#password', 'test_password_here')
    await page.click('button[type=submit]')
    await page.wait_for_selector('.dashboard-header', timeout=10000)
    steps.append('reached dashboard')
    title = await page.title()
    if title != 'Dashboard - Example App':
        return {'status': 'failure', 'steps': steps, 'errors': [f'unexpected title: {title}']}
    return {'status': 'success', 'steps': steps}
```

---

### Multi-Step Checkout

```python
async def run_check(page):
    steps = []
    await page.goto('https://shop.example.com')
    steps.append('homepage')

    await page.click('.product-item:first-child .add-to-cart')
    await page.wait_for_selector('.cart-badge', state='visible')
    steps.append('added to cart')

    await page.click('.cart-icon')
    await page.wait_for_url('**/cart')

    await page.click('button:has-text("Checkout")')
    await page.wait_for_url('**/checkout')

    await page.fill('#email', 'test@example.com')
    await page.fill('#name', 'Test User')
    await page.fill('#address', '123 Test St')
    await page.fill('#city', 'Test City')
    await page.click('input[value="test-payment"]')
    await page.click('button:has-text("Place Order")')
    steps.append('order submitted')

    await page.wait_for_selector('.order-confirmation', timeout=15000)
    message = await page.text_content('.confirmation-message')
    if 'Thank you' not in (message or ''):
        return {'status': 'failure', 'steps': steps, 'errors': ['no confirmation message']}
    return {'status': 'success', 'steps': steps}
```

> Synthetic scripts execute server-side and are gated by AST validation. Use them only in trusted, self-hosted deployments. The script body is sent as the `script_code` string when creating the check.

---

## Alert Configurations

Alerts are separate from checks. An alert has a `trigger_type` (`status_change`, `threshold`, or `repeated_failure`), a `trigger_config` object, and a list of notification providers. It can target specific checks (`check_ids`) or be global (`is_global: true`) with optional `trigger_config.check_filters`.

### Status-Change Alert (specific checks)

**Use case**: Notify when a check transitions to error

```json
{
  "name": "API Down Alert",
  "description": "Notify when the production API goes down",
  "trigger_type": "status_change",
  "trigger_config": {
    "on_status": ["error"],
    "consecutive_failures": 1
  },
  "is_enabled": true,
  "is_global": false,
  "notify_on_recovery": true,
  "notification_provider_ids": ["<provider-uuid>"],
  "check_ids": ["<check-uuid>"]
}
```

---

### Global Alert with Check Filters

**Use case**: Apply one rule to every production HTTP check, with recovery notices and resends

```json
{
  "name": "Critical Services Down",
  "description": "Alert when critical production services fail",
  "trigger_type": "status_change",
  "trigger_config": {
    "on_status": ["error"],
    "consecutive_failures": 3,
    "check_filters": {
      "check_types": ["http"],
      "tags": ["production", "critical"]
    }
  },
  "is_enabled": true,
  "is_global": true,
  "notify_on_recovery": true,
  "resend_interval_minutes": 30,
  "max_resends": 5,
  "notification_provider_ids": ["<provider-uuid>"],
  "check_ids": []
}
```

> Both `check_ids` (the checks to watch) and `notification_provider_ids` (the providers to fire) are **UUIDs**. `custom_subject` and `custom_message` may be supplied to override the notification templates. The simplest way to attach checks to an alert is through the UI (**Alerts → Edit → Checks**).

---

## Notification Providers

Notification providers are created with a `provider_type`, a `friendly_name`, and a provider-specific `config` object. Sensitive fields (password, api_key, token, secret) are masked in API responses.

### Email (SMTP)

**Required config**: `hostname`, `port`, `from_email`, `to_email`. **Optional**: `username`, `password`, `security` (`none`, `starttls`, or `ssl`), `cc`, `bcc`.

```json
{
  "provider_type": "email",
  "friendly_name": "Ops Email",
  "config": {
    "hostname": "smtp.gmail.com",
    "port": 587,
    "security": "starttls",
    "username": "alerts@example.com",
    "password": "app-specific-password",
    "from_email": "alerts@example.com",
    "to_email": "ops-team@example.com"
  },
  "is_default_enabled": true,
  "rate_limit_count": 100,
  "rate_limit_window_minutes": 60
}
```

---

### Webhook

**Required config**: `post_url`. **Optional**: `request_body_preset` (`json` or `form`, default `json`), `additional_headers` (object), `verify_ssl` (default true), `timeout` (seconds, default 10).

The webhook sends a **fixed JSON payload** describing the event — the body is not user-templated. The payload includes: `check_name`, `check_type`, `target`, `agent_id`, `agent_name`, `status`, `success`, `previous_status`, `latency_ms`, `timestamp`, `error_message`, `error_type`, `http_status_code`, `alert_name`, `alert_description`, `is_recovery`, and `consecutive_failures`.

```json
{
  "provider_type": "webhook",
  "friendly_name": "Incidents Webhook",
  "config": {
    "post_url": "https://hooks.example.com/services/INCOMING/WEBHOOK",
    "request_body_preset": "json",
    "additional_headers": {
      "X-Webhook-Secret": "secret_key_here"
    },
    "verify_ssl": true,
    "timeout": 10
  },
  "is_default_enabled": false
}
```

> To integrate with Slack, Discord, or other services that expect a specific message shape, point `post_url` at a small relay/middleware that reformats the fixed payload above into that service's schema. LuxSwirl does not template per-provider message bodies.

---

## Import/Export Patterns

Exported files use the import/export key names: `name`, `interval`, `timeout` (not `display_name`/`interval_seconds`/`timeout_seconds`). Import matches existing checks by `name`; `merge` mode skips existing checks and `replace` mode updates them.

### Templating Pattern (Single Check -> Many)

**Step 1**: Create a template (`template.json`):
```json
{
  "checks": [
    {
      "name": "SERVICE_NAME Health",
      "check_type": "http",
      "target": "https://SERVICE_URL/health",
      "interval": 60,
      "timeout": 30,
      "http_method": "GET",
      "expected_status": 200
    }
  ]
}
```

**Step 2**: Generate multiple checks:
```python
import json

with open('template.json') as f:
    template = json.load(f)

services = [
    {"name": "API", "url": "api.example.com"},
    {"name": "Auth", "url": "auth.example.com"},
    {"name": "Payment", "url": "payment.example.com"},
    {"name": "Notification", "url": "notify.example.com"},
]

checks = []
for service in services:
    check = template['checks'][0].copy()
    check['name'] = check['name'].replace('SERVICE_NAME', service['name'])
    check['target'] = check['target'].replace('SERVICE_URL', service['url'])
    checks.append(check)

with open('generated-checks.json', 'w') as f:
    json.dump({'checks': checks}, f, indent=2)

print(f"Generated {len(checks)} checks")
```

**Step 3**: Import `generated-checks.json` via the UI.

---

### Environment Promotion (Dev -> Staging -> Prod)

```bash
# Replace dev URLs with staging URLs
jq '.checks[].target |= gsub("dev.example.com"; "staging.example.com")' \
  dev-checks.json > staging-checks.json

# Replace staging URLs with prod URLs
jq '.checks[].target |= gsub("staging.example.com"; "example.com")' \
  staging-checks.json > prod-checks.json
```

Import each file into the corresponding environment.

---

### Bulk Configuration Update

**Scenario**: Change all check intervals to 120s. The export key is `interval`:

```bash
jq '.checks[].interval = 120' checks.json > updated-checks.json
# Import updated-checks.json with `overwrite: true` to update existing checks by name
```

---

## Status Page Examples

A status page has a `name`, a URL-safe `slug`, an `is_public` flag, an optional `config` (display settings such as `theme`, `show_uptime`, `refresh_interval`), and an `items` array. Each item is either a `check` (`check_id` + `order`) or a `group` (`name` + `filter` + `order` + optional `collapsed`). Check IDs are UUID strings.

### Public SaaS Status Page

```json
{
  "name": "Example App Status",
  "slug": "status",
  "description": "Real-time status of Example App services",
  "is_public": true,
  "config": {
    "theme": "dark",
    "show_uptime": true,
    "refresh_interval": 10
  },
  "items": [
    {
      "type": "group",
      "name": "API Services",
      "order": 0,
      "filter": { "tags": ["api", "production"] },
      "collapsed": false
    },
    {
      "type": "check",
      "check_id": "550e8400-e29b-41d4-a716-446655440000",
      "order": 1
    }
  ]
}
```

**Result**: `https://luxswirl.example.com/status/status`

---

### Internal Operations Page (private)

```json
{
  "name": "Internal Infrastructure",
  "slug": "ops",
  "description": "Infrastructure monitoring for the operations team",
  "is_public": false,
  "config": {
    "theme": "light",
    "show_uptime": true
  },
  "items": [
    {
      "type": "group",
      "name": "Production Servers",
      "order": 0,
      "filter": { "tags": ["production", "server"] },
      "collapsed": false
    },
    {
      "type": "group",
      "name": "Databases",
      "order": 1,
      "filter": { "tags": ["database"] },
      "collapsed": true
    }
  ]
}
```

**Access**: `https://luxswirl.example.com/status/ops`

---

## Complete Monitoring Stacks

These stacks use the **import/export** format (`name`, `interval`, `timeout`).

### WordPress Site Stack

```json
{
  "checks": [
    {
      "name": "WordPress Homepage",
      "check_type": "http",
      "target": "https://blog.example.com",
      "interval": 60,
      "timeout": 30,
      "http_method": "GET",
      "expected_status": 200
    },
    {
      "name": "WordPress Admin",
      "check_type": "http",
      "target": "https://blog.example.com/wp-admin",
      "interval": 300,
      "timeout": 30,
      "http_method": "GET",
      "expected_status": 200
    },
    {
      "name": "WordPress Database",
      "check_type": "mysql",
      "target": "mysql://wp_monitor:password@db.example.com:3306/wordpress",
      "interval": 120,
      "timeout": 10
    },
    {
      "name": "WordPress DNS",
      "check_type": "dns",
      "target": "blog.example.com",
      "interval": 3600,
      "timeout": 10
    }
  ]
}
```

> DNS `record_type`/`expect_value` and database `query` are not part of the import/export key set, so they fall back to defaults on import (DNS resolves the record; databases run `SELECT 1`). Set those fields afterward via the UI or the REST API if you need specific values.

---

### Microservices API Stack

```json
{
  "checks": [
    {
      "name": "API Gateway",
      "check_type": "http",
      "target": "https://api.example.com/health",
      "interval": 30,
      "timeout": 10,
      "http_method": "GET",
      "expected_status": 200
    },
    {
      "name": "Auth Service",
      "check_type": "json",
      "target": "https://auth.example.com/health",
      "interval": 60,
      "timeout": 10,
      "http_method": "GET",
      "expected_status": 200,
      "json_path": "status",
      "expected_value": "healthy"
    },
    {
      "name": "User Service",
      "check_type": "json",
      "target": "https://users.example.com/health",
      "interval": 60,
      "timeout": 10,
      "json_path": "database.connected",
      "expected_value": "true"
    },
    {
      "name": "Payment Service",
      "check_type": "http",
      "target": "https://payments.example.com/health",
      "interval": 30,
      "timeout": 15,
      "http_method": "GET",
      "expected_status": 200
    },
    {
      "name": "PostgreSQL Primary",
      "check_type": "postgres",
      "target": "postgresql://monitor:password@db.example.com:5432/production",
      "interval": 60,
      "timeout": 10
    }
  ]
}
```

> For a Redis port check, add a `tcp` check via the REST API or UI (target `redis.example.com`, `port` `6379`) — `port` is not part of the import/export key set.

---

## Tips and Best Practices

### Naming Conventions

**Good**:
- `prod-api-health` (environment-service-type)
- `Payment Gateway` (descriptive)
- `eu-west-1-db-replica` (region-component-type)

**Bad**:
- `check-1` (non-descriptive)
- `test` (ambiguous)
- `my-check` (whose check?)

---

### Interval Selection

| Check Type | Recommended Interval | Rationale |
|------------|---------------------|-----------|
| Critical API | 30-60 seconds | Fast detection |
| Website uptime | 60 seconds | Balance speed and load |
| Database health | 60-120 seconds | Avoid query overhead |
| DNS records | 3600 seconds (hourly) | Changes infrequently |
| Synthetic checks | 300-600 seconds | Resource-heavy operations |

`interval_seconds` (API) / `interval` (import) accepts 1–86400 seconds.

---

### Timeout Guidelines

- **HTTP/JSON checks**: up to 30 seconds
- **Database checks**: ~10 seconds (queries should be fast)
- **DNS checks**: ~10 seconds
- **Synthetic checks**: 60-120 seconds (complex workflows)

`timeout_seconds` (API) / `timeout` (import) accepts 1–300 seconds.

**Rule of thumb**: Timeout should be ~2x expected response time.

---

## Need More Examples?

**Request examples via**:
- GitHub Discussions: https://github.com/luxardolabs/luxswirl/discussions

**Contribute your examples**:
- Submit a PR to this file
- Share in GitHub Discussions (we'll add the best ones)

---

