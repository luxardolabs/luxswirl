# Import / Export

Import/Export backs up, restores, migrates, and templates check configurations using JSON files. It works **per agent**: an export contains all checks for one agent, and an import is applied to one agent (chosen in the UI). Use it to back up configurations, migrate checks between agents or environments (dev → staging → prod), build reusable templates, version-control config in git, and bulk-create/update checks without manual entry.

**Access:** **Agents** page → an agent's **Import / Export** button (between Edit and View Checks on the agent card). Requires an **admin** account.

**Exported:** each check's configuration — name, type, target, interval, timeout, retry attempts, enabled status, description, tags, and type-specific settings (HTTP method, JSON path, etc.).

**Not exported:** historical check results and metrics, alert configurations, agent metadata (hostname, API keys, approval status), and notification providers.

---

## Exporting

Open the panel and click **Download JSON** — a one-click operation that includes all checks (enabled and disabled). The file is named `{agent_id}-checks-{timestamp}.json`, where the timestamp is `YYYYMMDD-HHMMSS` (e.g. `prod-web-01-checks-20250111-143022.json`).

```json
{
  "agent_id": "abc123-def456-ghi789",
  "agent_hostname": "prod-web-01.example.com",
  "total_checks": 1,
  "checks": [
    {
      "name": "api_health",
      "check_type": "http",
      "target": "https://api.example.com/health",
      "interval": 60,
      "timeout": 5,
      "retry_attempts": 2,
      "enabled": true,
      "description": "Production API health endpoint",
      "http_method": "GET",
      "expected_status": 200,
      "json_path": null,
      "expected_value": null,
      "tags": "production,critical"
    }
  ]
}
```

`agent_id`, `agent_hostname`, and `total_checks` are **informational only** — on import, checks go to the agent you select in the UI, not the one named in the file. That's what lets you export from one agent and import to another.

### Check fields

| Field | Type | Required for import | Default | Notes |
|-------|------|---------------------|---------|-------|
| `name` | string | **Yes** | — | Unique within the agent |
| `check_type` | string | **Yes** | — | `http`, `ping`, `tcp`, `json`, `dns`, `mysql`, `postgres`, `synthetic` |
| `target` | string | **Yes** | — | URL / host / connection string, per type (below) |
| `interval` | int | No | 60 | Seconds between runs |
| `timeout` | int | No | 5 | Seconds per run |
| `retry_attempts` | int | No | 2 | Retries before marking down |
| `enabled` | bool | No | true | — |
| `description` | string | No | — | — |
| `http_method` | string | No (HTTP only) | — | GET, POST, … |
| `expected_status` | int | No (HTTP only) | — | Expected HTTP status |
| `json_path` | string | No (JSON only) | — | JSONata query |
| `expected_value` | string | No (JSON only) | — | Expected value for comparison |
| `tags` | string | No | — | Comma-separated |

The minimal valid check is `name` + `check_type` + `target`; everything else takes the defaults:

```json
{ "checks": [ { "name": "simple_check", "check_type": "http", "target": "https://example.com" } ] }
```

### Target format by type

| Type | Target | Per-type JSON |
|------|--------|---------------|
| `http` | `https://example.com` | adds `http_method`, `expected_status` |
| `json` | `https://api.example.com/data` | adds `json_path`, `expected_value` |
| `ping` | `192.168.1.100` | — |
| `tcp` | `redis.example.com:6379` (`host:port`) | — |
| `dns` | `example.com` | — |
| `mysql` | `mysql://user:pass@host:3306/database` | — |
| `postgres` | `postgresql://user:pass@host:5432/database` | — |

```json
// HTTP — method + expected status
{ "name": "http_check", "check_type": "http", "target": "https://example.com",
  "http_method": "POST", "expected_status": 201, "tags": "web" }

// JSON — JSONata query + expected value
{ "name": "json_api_check", "check_type": "json", "target": "https://api.example.com/data",
  "http_method": "GET", "expected_status": 200,
  "json_path": "data.users[0].status", "expected_value": "active", "tags": "api,validation" }

// TCP — host:port
{ "name": "redis_port", "check_type": "tcp", "target": "redis.example.com:6379",
  "timeout": 5, "tags": "infrastructure,redis" }

// MySQL / PostgreSQL — connection string in the target
{ "name": "mysql_connection", "check_type": "mysql",
  "target": "mysql://user:pass@host:3306/database", "timeout": 15, "tags": "database,mysql" }
```

> ⚠️ Synthetic checks run arbitrary Python and are admin-only — importing them is discouraged for security. See [SECURITY.md](../../SECURITY.md).

---

## Importing

Pick a mode, **drag and drop** a `.json` file onto the drop zone (or click it to browse — the file browser filters to `.json`), then click **Import Checks**. The result shows counts for **created**, **updated**, **skipped**, and any **errors** (with per-check error messages). If the whole import fails (invalid JSON, no checks, agent not found), an error card explains why.

| Mode | New checks | Existing checks (matched by `name`) |
|------|-----------|-------------------------------------|
| **Merge** (default) | Created | **Skipped**, left unchanged |
| **Replace** | Created | **Updated** to the file's values |

```
Existing: api_health, database_ping        Import file: api_health, web_homepage, dns_check
Merge   → created web_homepage, dns_check;  skipped api_health         (total 4)
Replace → updated api_health;               created web_homepage, dns_check  (total 4)
```

> ⚠️ **Replace overwrites existing check configurations and cannot be undone.** Always export a backup before using Replace.

Matching is by **exact, case-sensitive** `name` — `api_health` and `API_Health` are different checks, so a Replace that "isn't updating" usually means the name doesn't match the existing check.

### Import errors

Import validates JSON syntax, the required fields (`name`, `check_type`, `target`), the check type, and field types. It does **not** test target reachability, credential validity, or whether intervals/timeouts make sense — so test imported checks (ideally on a staging agent) before relying on them.

| Error | Cause | Fix |
|-------|-------|-----|
| `Invalid JSON file: Unexpected token` | Not valid JSON (syntax, encoding, corruption) | Validate with `jq .`; ensure UTF-8; remove anything before/after the JSON |
| `No checks found in file` | Missing top-level `checks` array | Add a `checks` array with ≥1 object |
| `Missing check name` | A check object has no `name` | Add a unique `name` to every check |
| `Invalid check_type` | Unsupported type | Use one of the eight valid types (case-sensitive) |
| `Missing target` | A check object has no `target` | Add a `target` matching the type |
| `Agent not found` | Target agent doesn't exist / was deleted | Verify the agent on the Agents page (must be active) |

The two most common JSON mistakes — comments and trailing commas, neither of which is valid JSON:

```json
// ✗ invalid
{ "checks": [ { "name": "c1", "check_type": "http", "target": "https://x", } ] }
// ✓ valid
{ "checks": [ { "name": "c1", "check_type": "http", "target": "https://x" } ] }
```

---

## Security

Targets can contain secrets — database connection strings carry passwords, and HTTP checks may embed tokens in headers — so export files may hold credentials in plaintext:

```json
{ "name": "database_check", "target": "mysql://dbuser:secretpassword@db.example.com:3306/appdb" }
```

- **Secure storage:** keep exports in encrypted locations; use git-crypt/git-secret for version control; never commit unencrypted files to public repos.
- **Credential scrubbing:** before sharing, replace passwords with placeholders — `sed -i 's/password:[^@]*/password:****/g' export.json`.
- **Access control:** restrict import/export to admins; keep dev/staging/prod on separate agents so their secrets stay separate.
- **Audit:** import/export operations are logged; review periodically for unusual bulk operations.

The web form is CSRF-protected and session-authenticated, so you **can't** automate it with `curl`/`wget` — use the REST API (`POST /api/v1/agents/{agent_id}/checks`) for automation.

---

## Automation (REST API + jq)

The check API is the automation path; full schemas are at `/docs`.

```bash
# Export an agent's checks
curl -H "Authorization: Bearer $TOKEN" \
  "$BASE/api/v1/agents/$AGENT_ID/checks" > checks.json

# Create checks from a file (one object per check)
cat checks.json | jq -c '.checks[]' | while read -r check; do
  curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d "$check" "$BASE/api/v1/agents/$AGENT_ID/checks"
done
```

`jq` makes bulk edits easy on an exported file before re-importing:

```bash
jq '.checks[].interval = 120' export.json > slower.json                 # change every interval
jq '.checks |= map(select(.tags | contains("production")))' \           # keep only prod checks
   export.json > prod.json
jq '.checks[].interval = (.checks[].interval * 2)' prod.json > dev.json  # derive a dev variant
jq . export.json                                                        # validate JSON
sed -i 's/password:[^@]*/password:****/g' export.json                   # scrub credentials
```

---

## Common workflows

### Back up an agent's configuration

1. Agents → **Import / Export** on the target agent → **Download JSON**.
2. Save it somewhere safe (e.g. `~/luxswirl-backups/prod-web-01-backup-20250111.json`) and commit to git:
   ```bash
   cd ~/luxswirl-backups
   git add prod-web-01-backup-20250111.json
   git commit -m "Backup before migrating to new check intervals"
   ```

Always back up before any bulk operation (Replace import, bulk delete); include a date in the filename and what changed in the commit message.

### Migrate checks between agents

1. Export the **source** agent (e.g. staging) → `staging-checks.json`.
2. Edit targets for the destination — replace staging URLs with production (`https://staging-api.example.com` → `https://api.example.com`).
3. On the **destination** agent (production), select **Merge** mode (to preserve existing checks) and upload the file.
4. Review created/skipped counts and confirm on the Dashboard that the checks are running.

### Create reusable templates

1. Configure one agent perfectly (HTTP, database, DNS, …) and test the checks.
2. Export it to a templates directory:
   ```
   templates/
   ├── web-app-standard.json      # HTTP, DNS, TCP
   ├── database-monitoring.json   # MySQL, Postgres
   └── api-gateway.json           # JSON, latency
   ```
3. For each new agent, **Merge**-import the template, then customize targets/intervals per agent.

### Bulk-update configuration

1. Export the agent's checks → `before.json`.
2. Modify with `jq` (or find/replace): `jq '.checks[].interval = 120' before.json > after.json`; `diff before.json after.json` to review.
3. Import `after.json` in **Replace** mode so existing checks pick up the new values.
4. Verify on the Dashboard.

### Promote across environments (dev → staging → prod)

1. Develop and test checks on a dev agent; export when stable.
2. Adjust targets for staging, **Merge**-import to the staging agent, and test for ~24 hours.
3. Adjust targets for production, **Merge**-import to the production agent, and monitor alerts.

```json
// dev      → "target": "http://localhost:8000/health"
// staging  → "target": "https://staging-api.example.com/health"
// prod     → "target": "https://api.example.com/health"
```

---

## Quick reference

```
File structure                       Import modes
{                                    Merge   → create new, skip existing
  "agent_id": "string",              Replace → create new, update existing
  "agent_hostname": "string",
  "total_checks": 0,                 Required: name, check_type, target
  "checks": [ { ...check fields } ]  Defaults: interval 60, timeout 5,
}                                              retry_attempts 2, enabled true
```

```bash
jq . export.json                                                    # validate
jq '.checks |= map(select(.tags | contains("production")))' export.json   # filter by tag
jq '.checks[].interval = 120' export.json                           # modify intervals
sed -i 's/password:[^@]*/password:****/g' export.json               # scrub credentials
```

---

## Related

- [Checks](checks.md) — the fields you're importing/exporting, and per-type configuration
- [Agents](agents.md) — where the Import/Export action lives
- [Dashboard](dashboard.md) — confirming imported checks are running
