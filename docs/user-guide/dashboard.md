# Dashboard

The Dashboard is the primary monitoring interface — real-time visibility into every check across every agent, with auto-refresh, filtering, and drill-down detail.

**Access:** **Dashboard** in the sidebar, or `/`. Summary stats and the check table auto-refresh every 10 seconds (configurable in Settings → Defaults → Dashboard Refresh Interval).

---

## Summary statistics

Four cards at the top give instant health metrics:

| Card | Primary | Secondary |
|------|---------|-----------|
| **Total Checks** | Enabled checks | Total including disabled (e.g. `47` / "52 total") |
| **Up** | Checks currently passing (green) | Success rate of enabled checks (e.g. `45` / `95.7%`) |
| **Down** | Checks currently failing (red) | Failure rate (e.g. `2` / `4.3%`) |
| **24h Success Rate** | Overall success % over 24h | Number of agents reporting (e.g. `99.2%` / "3 agents") |

---

## Filtering

| Filter | Options | Use case |
|--------|---------|----------|
| **Status** | All, Up, Down, Unknown (no recent results) | Filter to "Down" to focus on failing infrastructure during an incident |
| **Agent** | All approved agents (name or ID) | View all checks on a specific server/datacenter |
| **Type** | `ping`, `http`, `tcp`, `json`, `dns`, `mysql`, `postgres`, `synthetic` | Audit all database checks, or review all HTTP monitoring |
| **Tag** | All unique check-level and agent-level tags | View all `production` or all `critical` checks |
| **Per Page** | 10, 25, 50 (default), 100, 200 | Set to 200 for a comprehensive view with less scrolling |

Filters persist when you navigate pages, refresh, or open the detail panel.

---

## Check table

Each row is one check. Columns:

| Column | Shows |
|--------|-------|
| **Status** | Green (Up), red (Down), or gray (Unknown — no recent results; check disabled or agent offline) badge |
| **Type** | Icon + uppercase label (PING, HTTP, TCP, JSON, DNS, MYSQL, POSTGRES, SYNTHETIC) |
| **Check Name** | Display name (falls back to target) |
| **Target** | Monospace; per type — `https://api.example.com/health`, `192.168.1.1`, `redis.example.com:6379`, `mysql://user@db:3306/mydb` (password redacted), `example.com`; truncated if long |
| **Agent** | Agent name (or ID) executing the check |
| **Tags** | Blue = agent-level (inherited), green = check-level |
| **Latency** | Most recent response time, color-coded |
| **Uptime (24h)** | Success rate over 24h, color-coded |
| **Last Check** | Relative time of the most recent execution |

**Latency** is color-coded for quick assessment and formatted by magnitude (sub-millisecond `0.45ms`, milliseconds `87ms`, seconds `1.23s`; `-` if no recent result):

| Color | Range |
|-------|-------|
| Green | < 100ms (excellent) |
| Yellow | 100–500ms (acceptable) |
| Red | > 500ms (slow) |

(Interpretation varies by type — a 50ms ping is excellent, but a 50ms database query may be concerning.)

**Uptime (24h)** = (successful checks / total checks) × 100 over the last 24 hours, color-coded for SLA monitoring (`-` for new checks with insufficient history):

| Color | Range |
|-------|-------|
| Green | ≥ 99% |
| Yellow | 95–99% |
| Red | < 95% |

**Last Check** formats: `5s ago` (0–59s), `3m ago` (1–59m), `2h ago` (1–23h), `11:45am` (older than 24h shows local time), `Never` (hasn't run); hover for the full timestamp.

**Pagination** shows "Showing X to Y of Z checks" with Previous/Next and numbered pages (the current page highlighted; 2 pages either side plus first/last). Filters are preserved across pages.

---

## Auto-refresh

The summary stats and check table refresh every 10 seconds (default) via HTMX partial updates — only the changing data is replaced, so there's no full reload or flicker, and your scroll position, filters, and pagination are preserved. The **check detail panel does not auto-update** (you control its time range).

Configure the interval in **Settings → Defaults → Dashboard Refresh Interval** (minimum 5s, recommended 10s). Lower intervals (5s) give more real-time visibility but increase database queries; higher (30s+) reduce load but delay incident awareness.

---

## Check detail panel

Clicking a row opens a slide-out panel on the right.

**Header:** the **Check Details** title, a status badge, a **Time Range** selector (4 Hours, 8 Hours, 24 Hours, 3 Days, 7 Days), and a close button.

**Metadata:** display name, target (monospace), type icon + label, and the check interval.

**Current status:** a pulsing dot (green Up / red Down / gray Unknown), the status text, and the timestamp of the last result.

### 30-minute status bar

A minute-by-minute timeline of the last 30 minutes — 30 bars, one per minute:

- **Green** — the check was passing that minute.
- **Red** — it was failing.
- **Gray** — no data (didn't run, or agent offline).

Hovering a bar shows how many minutes ago, the status (✓ Up / ✗ Down), the number of checks in that minute, and the average latency — e.g. "5m ago — ✓ Up (3 checks, 87ms avg)".

### Statistics grid (4 cards)

- **Current Latency** — most recent response time (`87 ms`; `-` if none).
- **Average Latency** — mean over the selected range, labeled with it (`Avg (4h)`, `Avg (24h)`).
- **Uptime %** — success rate over the range, color-coded (green ≥99%, yellow 95–99%, red <95%).
- **Total Checks** — executions over the range (e.g. `240` for 4h at a 60s interval).

### SSL certificate (HTTPS checks)

For HTTP checks against HTTPS endpoints, the panel validates and shows the certificate:

| Badge | Condition |
|-------|-----------|
| **Valid** (green) | Not expiring soon |
| **Expiring Soon** (yellow) | < 30 days to expiry |
| **Critical** (orange) | < 7 days to expiry |
| **Expired** (red) | Already expired |

Plus days until expiration (or "Expired X days ago"), the expiration date, the subject (usually the domain), and the issuer (e.g. Let's Encrypt, DigiCert). The card's color matches the status severity.

### Check-type-specific metrics

| Type | Metrics shown |
|------|---------------|
| **DNS** | Nameserver, record type, flags (authoritative/recursive), record count, the returned records, TTL, canonical name |
| **MySQL / PostgreSQL** | Connection latency, query latency, row count, column names, error type (on failure) |
| **Synthetic** | Screenshot, browser console logs, script execution time, any custom metrics your script returned |

### Performance chart

A Chart.js line chart of latency over the selected range (X = time, Y = ms; hover for exact values). It updates when you change the time range and shows gaps where data is missing (no interpolation).

### Recent events

A collapsible accordion (collapsed by default) listing the last 50 results in a scrollable table: **Status** (✓/✗), **Latency** (ms), **Time**, and **Error** (message if it failed).

---

## Common workflows

**Monitoring during an incident.** Set the **Status** filter to "Down" → check the **Down** count for scope → click failing checks → review **Recent Events** for when the failure started and the **error messages** for root cause.

**Reviewing a specific agent.** Select it in the **Agent** filter → review the summary stats for that agent's health → check the **Last Check** column to confirm it's actively running checks → verify the blue agent-level **Tags**.

**SLA reporting.** Set **Per Page** to 200 → review the **Uptime (24h)** column → filter by **Tag** to group by customer/service → click low-uptime checks for detailed history.

**Performance investigation.** Scan the **Latency** column for red/yellow values → open those checks → review the **Performance Chart** for trends → compare **Current** vs **Average** latency for spikes → check the **30-minute status bar** for recent patterns.

**Infrastructure review.** Use the **Type** filter one type at a time → for database checks verify **Connection Latency** → for HTTP checks review **SSL certificates** for upcoming expirations → for DNS checks verify **TTL** values.

---

## Performance considerations

For large deployments (hundreds/thousands of checks): use filters to narrow scope before loading, increase **Per Page** to 100–200 to reduce pagination, raise the refresh interval to 30s+ to cut database load, and organize with tags. The Dashboard is optimized for modern browsers; HTMX auto-refresh uses minimal memory, and ~200 checks per page is the upper limit for smooth performance.

---

## Troubleshooting

| Symptom | Cause | Solution |
|---------|-------|----------|
| Checks show **"Unknown"** | No recent results (last ~5 min) | Verify the agent is online; confirm the check is enabled; if the interval is longer than 5 min, that's expected |
| Dashboard **not auto-refreshing** | HTMX/JS issue | Check the browser console (F12); ensure JS isn't blocked by an ad blocker; hard-refresh (Ctrl/Cmd+Shift+R) |
| **"No checks found"** | Filters too restrictive, or none configured | Reset filters to "All"; verify checks exist (Checks page) and agents are approved/online |
| **Latency seems wrong** | It's measured from the agent, not the server | Check the agent's network connectivity; compare with `ping`/`curl` from the agent host |
| **Detail panel won't open** | JS/HTMX error | Check the console; use a modern browser; try another row; refresh |

---

## Related

- [Checks](checks.md) — creating and configuring health checks
- [Agents](agents.md) — managing monitoring agents
- [Alerts](alerts.md) — alerts for failing checks
- [Settings](settings.md) — dashboard refresh interval and defaults
