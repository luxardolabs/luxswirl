# Health Checks

Health checks are the core monitoring configurations in LuxSwirl. Each check defines what to monitor (target), how to monitor it (check type), how often to run (interval), and where to run it (agent assignment), plus validation rules and optional alerts. LuxSwirl supports 8 check types covering network, application, database, and synthetic monitoring. Checks are stored centrally; agents pull their assigned checks on each sync, run them on interval, report results to the server, which stores them in TimescaleDB and evaluates alerts.

**Access:** **Checks** in the sidebar, or `/checks`.

A check has three states: **Enabled** (actively running and reporting), **Disabled** (configured but not executing), and Paused (reserved; not currently used in the UI).

---

## Universal fields

These apply to every check type:

| Field | Required | Default | Notes |
|-------|----------|---------|-------|
| **Display Name** | Yes | target | Human-readable name; falls back to the target if blank. Use descriptive names |
| **Check Type** | Yes | — | One of the 8 types below. **Immutable** — to change type, delete and recreate. The rest of the form adapts to the type |
| **Target** | Yes | — | What to monitor; format depends on type (see each type) |
| **Interval (s)** | Yes | 60 | 10–86400 (10s to 24h). Lower = more frequent = more results and database load. Recommended: 60s production, 300s non-critical |
| **Timeout (s)** | Yes | 10 | 1–300; must be less than the interval (the check must finish before the next run). Global default in Settings → Checks |
| **Retries** | Yes | 2 | 0–10. On failure the agent waits the retry interval (~30s) and retries up to N times before marking down — reduces false positives from transient issues |
| **Tags** | No | — | Comma-separated (e.g. `critical,api,customer-facing`); agent tags also apply to its checks. Used in dashboard and checks-page filters |
| **Alert Rule** | No | — | Attach a configured alert (or "No Alert"); the same alert can cover many checks. Alerts are configured in Settings → Alerts |
| **Enabled** | No | true | Uncheck to create a check without running it yet (prepare now, enable later) |

### Agent assignment

| Mode | Behavior | Use for |
|------|----------|---------|
| **Manual** (default) | Pick one approved agent from a dropdown to run the check | A check that must run from a specific location (datacenter-specific monitoring) |
| **Replicate** | Configure a tag selector; the check runs on **every** matching agent, independently — you get one result series per agent — and new matching agents pick it up automatically | Multi-region / multi-vantage monitoring — probe an external API from every region to compare latency and catch regional outages |
| **Distribute** | Configure a tag selector; the check is assigned to exactly **one** agent from the matching pool, chosen deterministically by hash; if the pool changes, the hash re-maps it | Sharing a large monitoring workload evenly across a fleet (load spread + basic HA) rather than running every check on every agent |

### Depends On (alert dependency)

Set a **parent** check to suppress this check's alerts while the parent is failing — one alert for the root cause instead of an alert storm from every downstream check.

- **Eligible parents:** any check that does not itself have a parent (single-level only).
- **Cross-agent:** allowed — the parent can run on a different agent (e.g. a firewall agent pings the gateway, LAN-side agents point their device checks at it).
- **Behavior:** when the parent's most recent result is failing, notifications for this check are written to the notification log as `suppressed` with the reason "Suppressed: parent check '\<name\>' is down" instead of being delivered. They resume automatically when the parent recovers.
- **Caveat:** the parent must be monitored well enough to detect its own failure. If the parent's check stays "up" while its real upstream dies, this check still alerts independently.

Two ways to configure dependencies:

- **Bottom-up (one check at a time):** open a check's edit form and pick its parent from the **Depends On** dropdown (grouped by agent; "None" is the default). Best when adding a single new check.
- **Top-down (bulk-assign children to a parent):** open the **parent** check's detail panel and click the `↓ Add dependents` chip in the header. A side panel lists every eligible child with filters (type, agent, tag, and free-text search across name and target), checkboxes pre-checked for current children, "Select all visible" / "Clear all", and one "Save dependents". The classic workflow: create the "gateway-ping" check, then on its detail panel filter `type=ping`, search `192.168.1.`, Select all visible, save — 100 device checks adopt the parent in one click. (Checks that are already parents are excluded, preserving the single-level rule.)

**Indicators:** the checks list **Deps** column shows `↑` (has a parent — click to navigate), `↓N` (N dependents — click to open the dependents manager), or `—`. The detail panel shows an `↑ Depends on: <name>` pill with a green/red/gray dot for the parent's current state, and a clickable `↓ N dependent(s)` chip showing blast radius before you pause or delete the parent.

> **Example:** 100 device pings inside a LAN, all set to depend on one "gateway ping". The gateway dies → one alert for the gateway and 100 suppression-log entries, instead of 100 emails.

---

## Check types

### 1. Ping

ICMP ping to test network connectivity and latency. **Use for:** verifying a server/device is online, measuring latency, monitoring route changes, testing firewall rules.

**Fields:**
- **Target** (required): hostname or IP — **no** `http://`/`https://` (✅ `example.com`, `192.168.1.1`).
- **Count** (optional, default 1, range 1–10): ping packets to send; higher = more accurate latency.
- **Timeout** (required, default 1s), **Retries** (required, default 2).

**Succeeds** if at least one packet replies within the timeout (packet loss < 100%). **Reports** average round-trip latency, packet loss %, and min/max/avg (if count > 1). **Common issues:** "ping failed" (target unreachable, ICMP blocked by firewall, wrong hostname); high packet loss (congestion or unstable connection); timeout (distant target or path issue).

```
Display Name: Production Server Ping
Type: ping     Target: prod-server-01.example.com
Interval: 60s  Timeout: 2s  Retries: 2  Count: 3
```

### 2. HTTP / HTTPS

Monitor web endpoints, APIs, and HTTP services with advanced validation. **Use for:** API health checks, website availability, SSL-certificate expiry tracking, response-time monitoring, status-code validation, content (regex) validation.

**Fields:**
- **Target** (required): full URL including protocol (✅ `https://api.example.com/health`; ❌ `example.com`).
- **Method** (optional, default GET): GET, POST, PUT, DELETE, PATCH, HEAD, OPTIONS.
- **Expected Status** (optional, default 200).
- **Verify SSL** (optional): the create form pre-checks this, so new HTTP/JSON checks verify SSL by default. The global `check.default_verify_ssl` setting (Settings → Checks) ships seeded **false** — it's the form that defaults a new check to on, not the global setting. Uncheck to allow self-signed certs (not recommended for production).
- **Follow Redirects** (optional, default true): follow 301/302.
- **Max Response Time** (optional): fail if the response exceeds this (ms).
- **Headers** (optional, JSON): e.g. `{"Authorization": "Bearer token123"}`.
- **Body** (optional): request body for POST/PUT.
- **Content Regex** (optional): a regex that must match in the response body (e.g. `"status":"ok"`).
- **Header Checks** (optional, JSON): validate response headers (e.g. `{"Content-Type": "application/json"}`).

**Succeeds** when the status matches, the response arrives within timeout, and (if set) the content regex, header checks, and max response time all pass. **Reports** latency, status code, response size, and — for HTTPS — SSL certificate info (subject, issuer, expiration date, days until expiry, expiring/expired warnings), which feed SSL-expiry alerts. **Common issues:** SSL errors (expired/mismatched/self-signed cert), timeout, wrong status code, content-validation failure.

```
Display Name: Production API Health
Type: http     Target: https://api.example.com/health
Method: GET    Expected Status: 200
Interval: 60s  Timeout: 5s
Content Regex: "status":"healthy"      Max Response Time: 1000
```

### 3. TCP

Test TCP port connectivity, with an optional protocol exchange. **Use for:** database connectivity (without running queries), service-port availability (Redis, Memcached…), protocol-level checks (SMTP, POP3…), firewall validation.

**Fields:**
- **Target** (required): hostname or IP, no protocol (e.g. `redis.example.com`, `192.168.1.100`).
- **Port** (required, 1–65535): e.g. 6379 (Redis), 5432 (PostgreSQL), 3306 (MySQL), 25 (SMTP).
- **Send String** (optional): data to send after connecting — protocol handshakes (e.g. `PING\r\n` for Redis).
- **Expect String** (optional, requires Send String): expected response (e.g. `+PONG`).
- **Timeout** (required, default 2s), **Retries** (required, default 2).

**Succeeds** when the TCP connection establishes (and, if a send string is set, the response matches the expect string). **Reports** connection latency and any data received. **Common issues:** connection refused (port not listening or firewall), timeout, expect-string mismatch (service responded but not as expected).

```
Display Name: Redis Availability
Type: tcp      Target: redis.example.com   Port: 6379
Send String: PING\r\n   Expect String: +PONG
Interval: 30s  Timeout: 2s
```

### 4. JSON

An HTTP check with JSON-response validation using JSONata queries (Uptime Kuma compatible). **Use for:** API endpoint validation, JSON structure/value checks, nested-field checking, array filtering and counting, complex path queries.

**Fields:**
- **Target** (required): full URL (same as HTTP). **Method** (optional, default GET).
- **Headers** (optional): auto-adds `Content-Type: application/json`. **Body** (optional).
- **JSON Path** (required): a JSONata query expression (examples below).
- **Expected Value** (required): the expected result — string, number, boolean, or array (e.g. `"ok"`, `200`, `true`, `["a","b"]`).
- **Comparison Type** (optional, default `equals`): `equals` (exact, case-sensitive), `contains` (substring), `regex`, `gt`, `lt`, `gte`, `lte` (numeric).
- **Verify SSL** (optional): create form pre-checks it; the global `check.default_verify_ssl` setting is seeded false.

**JSONata query examples:**

```javascript
data.status                              // simple field → "ok"
data.users[0].name                       // array index → "John"
printers."printer.example.com".status    // quoted key (dots in the key) → "online"
printers.*.status                        // wildcard → ["online", "offline", "online"]
printers[status="online"]                // filter → array of online printers
$count(printers)                         // count function → 3
$sum(orders.*.total)                     // sum function → 1250.50
users[age > 18 and status="active"]      // predicate → array of adult active users
```

**Succeeds** when the request returns 200, the body is valid JSON, the query runs without error, and its result matches the expected value under the comparison type. **Reports** latency, the query result, and the match status. **Common issues:** invalid JSON, query syntax error or missing path, value mismatch, type mismatch (comparing string to number). Queries can be copied directly from Uptime Kuma — full JSONata is supported; test at <https://try.jsonata.org>.

```
Display Name: API Status Check
Type: json     Target: https://api.example.com/status   Method: GET
JSON Path: data.health.status    Expected Value: "healthy"   Comparison Type: equals
Interval: 60s
```

### 5. DNS

Validate DNS resolution and record values. **Use for:** DNS-server availability, record-correctness validation, propagation monitoring, TTL verification, authoritative-nameserver checks.

**Fields:**
- **Target** (required): the domain to query (e.g. `example.com`, `_dmarc.example.com`).
- **Record Type** (required): A, AAAA, CNAME, MX, TXT, NS, PTR, SOA, SRV, CAA.
- **Nameserver** (optional, default `1.1.1.1`): the DNS server to query (`1.1.1.1` Cloudflare, `8.8.8.8` Google, or a custom server). **Port** (optional, default 53).
- **Expect Value** (optional): validates that at least one returned record matches (e.g. A record `93.184.216.34`, TXT `v=spf1 include:_spf.example.com ~all`).
- **Expect Count** (optional): the number of records must match (e.g. 4 A records for a load-balanced service).
- **Timeout** (required, default 5s), **Retries** (required, default 2).

| Type | Purpose | Example value |
|------|---------|---------------|
| **A** | IPv4 address | `93.184.216.34` |
| **AAAA** | IPv6 address | `2606:2800:220:1:248:1893:25c8:1946` |
| **CNAME** | Canonical name (alias) | `example.com.` |
| **MX** | Mail exchange | `10 mail.example.com.` |
| **TXT** | Text record | `v=spf1 include:_spf.google.com ~all` |
| **NS** | Nameserver | `ns1.example.com.` |
| **PTR** | Reverse DNS lookup | `example.com.` |
| **SOA** | Start of authority | serial/refresh/retry/… |
| **SRV** | Service locator | `10 60 5060 sipserver.example.com.` |
| **CAA** | CA authorization | `0 issue "letsencrypt.org"` |

**Succeeds** when the query returns ≥1 record (and matches expect value / count if set). **Reports** latency, the returned records and count, TTL, the authoritative flag, and (for CNAME) the canonical name. **Common issues:** NXDOMAIN (domain doesn't exist), no records of that type, timeout, value mismatch.

```
Display Name: Primary DNS A Record
Type: dns      Target: example.com   Record Type: A
Nameserver: 1.1.1.1   Expect Value: 93.184.216.34   Interval: 300s
```

### 6. MySQL

Test MySQL/MariaDB connectivity and execute a query. **Use for:** database availability, query performance, data validation, connection-pool and replication-lag health.

**Fields:**
- **Target / Connection String** (required): `mysql://username:password@hostname:port/database` (port defaults to 3306; add `?ssl=true` for SSL).
- **Query** (optional, default `SELECT 1`): use read-only `SELECT`s — e.g. `SELECT COUNT(*) FROM users WHERE active = 1`, `SHOW STATUS LIKE 'Threads_connected'`, `SELECT @@read_only`.
- **Timeout** (required, default 5s), **Retries** (required, default 2).

**Succeeds** when the connection establishes, the query runs without error, and results return within the timeout. **Reports** connection latency, query latency, total latency, row count, column names, and an error-type classification on failure (connection / syntax / timeout). **Common issues:** connection refused (not listening/firewall), authentication failed, database missing or insufficient permissions, query timeout (slow query or load), SQL syntax error.

**Security:** create a dedicated **read-only** monitoring user with minimal (SELECT-only) permissions; connection strings and the check target/config are **encrypted at rest** (Fernet, AES-128-CBC + HMAC) and passwords are scrubbed from logs; ensure the agent can reach the database (firewall). Prefer simple queries (avoid table scans), monitor connection vs query latency separately, alert on slow queries (>1000ms), and test against a replica first.

```
Display Name: Production DB Health
Type: mysql    Target: mysql://monitor_user:pass123@db-primary.example.com:3306/production
Query: SELECT COUNT(*) FROM users WHERE active = 1
Interval: 60s  Timeout: 5s
```

### 7. PostgreSQL

Test PostgreSQL connectivity and execute a query. Same metrics, common issues, and security model as MySQL.

**Fields:**
- **Target / Connection String** (required): `postgres://username:password@hostname:port/database` (or `postgresql://`; port defaults to 5432; PostgreSQL uses SSL if available).
- **Query** (optional, default `SELECT 1`), **Timeout** (required, default 5s), **Retries** (required, default 2).

Useful PostgreSQL monitoring queries:

```sql
SELECT pg_is_in_recovery();                          -- is this a replica?
SELECT count(*) FROM pg_stat_activity;               -- active connections
SELECT pg_database_size('production');               -- database size
SELECT EXTRACT(EPOCH FROM (now() - pg_last_xact_replay_timestamp()));  -- replica lag (sec)
SELECT count(*) FROM pg_stat_activity                -- long-running queries
  WHERE state = 'active' AND query_start < now() - interval '1 minute';
```

```
Display Name: PostgreSQL Primary Health
Type: postgres   Target: postgres://monitor_user:pass123@pg-primary.example.com:5432/production
Query: SELECT pg_is_in_recovery()
Interval: 60s    Timeout: 5s
```

### 8. Synthetic (Playwright)

Execute Playwright scripts for browser-based monitoring and user-journey testing. **Use for:** login flows, e-commerce checkout, form submissions, multi-step workflows, JavaScript-heavy apps, visual regression (via screenshots).

> ⚠️ **Admin only — arbitrary code execution.** Synthetic checks run user-provided Python via `exec()` with Playwright. Only admins can create/modify them. Scripts are **AST-validated** (blocking obvious attacks — `eval`, `exec`, `os`, `subprocess`, etc.); every operation is logged with a `SECURITY AUDIT` prefix; a prominent warning banner shows when creating/editing. AST validation is a guardrail, **not** a sandbox (it's bypassable) — treat the ability to create a synthetic check as equivalent to host access, and use only in trusted, self-hosted, single-organization deployments. **Not multi-tenant safe** (currently requires additional sandboxing for managed SaaS). See [SECURITY.md](../../SECURITY.md).

**Fields:**
- **Target** (required): the URL to start the browser session.
- **Script Code** (required): Python executed with access to a Playwright `page` object; must return a dict with at least `status`. May include `steps`, `errors`, and any custom fields.
- **Headless** (optional, default true): set false for debugging (requires a display server).
- **Timeout** (required, default 30s): script execution timeout, separate from Playwright's own timeouts.
- **Retries** (required, default 2).

**Return dict fields:** `status` (required — `"success"` or `"failure"`); `steps` (optional list of strings); `errors` (optional list); any custom fields (stored in result metadata).

**Script structure** — the script has access to `page` (a Playwright Page) and must return a dict:

```python
# Navigate to the page
await page.goto("https://example.com")

# Wait for an element, then interact
await page.wait_for_selector("#login-button")
await page.click("#login-button")
await page.fill("#username", "testuser")
await page.fill("#password", "testpass")
await page.click("button[type='submit']")

# Wait for navigation
await page.wait_for_url("**/dashboard")

# Return the result
return {
    "status": "success",  # or "failure"
    "steps": [
        "Navigated to login page",
        "Filled credentials",
        "Logged in successfully",
    ],
    "custom_metric": 1234,
}
```

**Succeeds** when the script runs without exception, returns `status == "success"`, and finishes within the timeout. **Reports** script execution time, captured browser console errors and failed HTTP requests, the returned steps, a final-state **screenshot** (PNG) and a Playwright **trace** (both stored in the DB and viewable in the check detail panel — the trace opens in the Playwright Trace Viewer), and any custom fields. **Common issues:** timeout, element/selector not found, script syntax/runtime error, browser crash (OOM).

**Example — verify a login flow:**

```
Display Name: User Login Flow
Type: synthetic   Target: https://app.example.com/login
Timeout: 30s      Headless: true
Script Code:
  await page.goto("https://app.example.com/login")
  await page.fill("#email", "test@example.com")
  await page.fill("#password", "testpass123")
  await page.click("button[type='submit']")
  await page.wait_for_url("**/dashboard")

  # Confirm the dashboard rendered the user's name
  user_name = await page.text_content(".user-name")

  return {
      "status": "success" if user_name else "failure",
      "steps": ["Logged in", "Verified dashboard"],
      "user_name": user_name,
  }
```

**Security best practices:** review all script code before saving; use dedicated **test** accounts (never production credentials); monitor executions in the audit logs; set resource limits and consider a separate agent for synthetic checks (isolation). **Performance:** synthetic checks are resource-intensive (CPU, memory) — keep concurrency low (agent default 5), use longer intervals (300s+), and watch agent subprocess and file-descriptor counts.

---

## Bulk operations

Select checks with the row checkboxes; when every check on the page is selected, a **Select all X matching filters** link appears to select all matches across every page (for modifying 200+ checks at once). Then act via the bulk bar:

- **Enable** / **Disable** — run as background maintenance jobs (not instant); agents start/stop the checks on their next sync once the job completes.
- **Modify** — opens a panel to change interval (10–86400), timeout (1–300), retries (0–10), agent, and alert rule (No change / Clear all / a specific alert) for all selected checks; blank fields mean no change. Queues a background bulk-modify job.
- **Delete** — confirmation required; queues a background bulk-delete job that removes the checks and cascades through their results.

### Bulk modify panel fields

| Field | Range / options |
|-------|-----------------|
| Interval (seconds) | 10–86400 |
| Timeout (seconds) | 1–300 |
| Retries | 0–10 |
| Agent | Reassign to a different agent |
| Alert | No change / Clear all / select an alert |

---

## Configuration guidelines

| Interval | Use case | Resource impact |
|----------|----------|-----------------|
| 10–30s | Critical production services | Very high |
| 60s | Standard monitoring (recommended) | Moderate |
| 300s (5m) | Non-critical or slow services | Low |
| 900s (15m) | Periodic batch-job checks | Very low |
| 3600s (1h) | Daily backup validation | Minimal |

**Timeouts** (rule of thumb: ⅓–½ of the interval): ping 1–2s, HTTP 3–5s, TCP 2–3s, DNS 5s, database 5–10s, synthetic 30–60s.

**Retries:** 0 = fail immediately on first error (not recommended); 1 = one retry, reduces false positives; 2 = default; 3 = extra resilience for flaky services; >3 = rarely needed, lengthens detection time.

---

## Workflows

**Create a standard HTTP health check.** New Check → Type `http` → Display Name "Production API Health" → Target `https://api.example.com/health` → Method GET → Expected Status 200 → Interval 60s, Timeout 5s → Assignment Manual + production agent → attach an alert (e.g. "Critical Services Alert") → Create Check.

**Clone across environments.** Find the check → **Clone** → change Display Name ("Staging API Health") and Target (`https://staging-api.example.com/health`) → pick the staging agent → Create Check. Repeat per environment.

**Bulk-modify intervals.** Apply filters (Agent = prod, Type = http) → check Select All → click **Select all X matching filters** → **Modify** → Interval 300 → Apply Changes.

**Disable for maintenance.** Filter Tag = customer-facing → select all → **Disable** → do the maintenance → re-select and **Enable**.

**Reassign to a new agent.** Filter Agent = old-agent → select all → **Modify** → Agent = new-agent → Apply Changes → reload the new agent to pull immediately.

---

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| **Check not executing** | Confirm it's enabled (green toggle) and the agent is online and approved; click **Reload** on the agent card to force a sync |
| **No results in the dashboard** | The check hasn't run yet (wait up to one interval); confirm the agent is sending results (reporter backlog = 0); the "Last Check" column should update |
| **"Invalid configuration" on create** | Read the field in the error — common: a scheme in a ping/tcp target, port out of 1–65535, timeout ≥ interval, invalid JSON in headers/body |
| **Succeeds locally, fails on the agent** | The agent is in a different network / DNS / firewall context — SSH to the agent host and test connectivity manually |
| **Bulk operation didn't affect all checks** | Confirm **Select all matching filters** was clicked; review the active filters (they may exclude some); ensure you have permission |

---

## Related

- [Dashboard](dashboard.md) — viewing check results and status
- [Agents](agents.md) — managing the agents that execute checks
- [Alerts](alerts.md) — notifications for failed checks, and the **Depends On** dependency
- [Import/Export](import-export.md) — bulk check configuration as JSON
