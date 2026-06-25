# Background Jobs

Jobs are one-time background tasks dispatched to an agent (or run on the server). Unlike checks, which run continuously on an interval, a job executes once and produces a structured JSON result — useful for discovery, diagnostics, and bulk operations. Results auto-purge after 7 days.

**Access:** **Jobs** in the sidebar, or `/jobs`.

| Aspect | Check | Job |
|--------|-------|-----|
| Frequency | Continuous (every N seconds) | One-time |
| Purpose | Health monitoring | Investigation, discovery, automation |
| Result | Pass/fail + latency | Structured JSON |
| Lifetime | Permanent until deleted | Auto-purges after 7 days |

---

## Job types

| Type | What it does | Runs on | Job timeout | Parameters |
|------|--------------|---------|-------------|------------|
| `network_discover` | Enumerates the agent's network interfaces and suggests scan targets | Agent only | 30s | None |
| `network_scan` | Scans a subnet for responsive hosts and open ports | Agent only | 600s | `subnet`, `ports`, `timeout`, `max_concurrent` |

Both types need network access, so they run on an **agent**, never the server. `network_discover` usually finishes in 5–10s; a `network_scan` of a `/24` takes a few minutes (see [Performance](#performance--tuning)).

### network_scan parameters

| Parameter | Required | Default | Notes |
|-----------|----------|---------|-------|
| `subnet` | Yes | — | CIDR notation (e.g. `192.168.1.0/24`); maximum `/16` (65,536 hosts) |
| `ports` | No | `22,80,443,3306,5432,8080,8443` | Comma-separated; each `1–65535`. Empty list = ping-only (no port scan) |
| `timeout` | No | `10` | Seconds per host (`1–30`) for ping + DNS + ports. Raise on slow networks |
| `max_concurrent` | No | `100` | Hosts scanned at once (`10–500`). Higher = faster but more agent load |

`network_discover` takes no parameters — it detects the topology automatically.

### Results

`network_discover` returns the interfaces it found plus host context:

```json
{
  "interfaces": [
    { "name": "eth0", "ip": "192.168.1.100", "netmask": "255.255.255.0",
      "cidr": "192.168.1.0/24", "gateway": "192.168.1.1", "is_up": true,
      "suggested_scan": "192.168.1.0/24" }
  ],
  "hostname": "prod-agent-01",
  "default_gateway": "192.168.1.1",
  "is_containerized": true,
  "duration_seconds": 2.34
}
```

`network_scan` returns the responsive hosts and their open/closed ports:

```json
{
  "hosts_scanned": 254,
  "hosts_responsive": 47,
  "hosts": [
    { "ip": "192.168.1.1", "hostname": "router.local", "response_time_ms": 1.2,
      "ports_open": [80, 443], "ports_closed": [22, 3306, 5432] }
  ],
  "duration_seconds": 245.67
}
```

---

## Creating a job

1. Click **New Job** to open the creation panel.
2. Pick a **Job Type**.
3. Pick an **Agent** (required — `network_discover`/`network_scan` cannot run on the server).
4. Optionally set **Priority** (see below).
5. Fill in any type-specific parameters, then **Create Job**.

Priority governs queue order — higher runs first; ties are FIFO.

| Priority | Range | Use for |
|----------|-------|---------|
| High | 75–100 | Urgent diagnostics, troubleshooting |
| Normal | 25–74 | Standard operations (default: 50) |
| Low | 0–24 | Background discovery, non-urgent work |

---

## Lifecycle

```
pending → assigned → running → completed
                             ↘ failed
                             ↘ cancelled
```

| Status | Meaning | Actions |
|--------|---------|---------|
| `pending` | Created, waiting for assignment | Cancel, Delete |
| `assigned` | Sent to the agent, awaiting execution | Cancel, Delete |
| `running` | Executing now | Cancel |
| `completed` | Finished; results available | Delete |
| `failed` | Errored; error message available | Delete |
| `cancelled` | Stopped before/during execution | Delete |

**Auto-purge:** every job gets an `expires_at` 7 days from creation. A background task deletes expired jobs of any status, **including their results** (jobs are not archived). Delete a job manually to clean it up sooner.

**No auto-retry:** a failed job stays failed — create a new one to retry. Failed jobs may still carry partial results.

---

## The Jobs page

- **Summary cards:** Total, Running, Completed, Failed, Cancelled.
- **Filters:** Type, Agent (including `server`), Status, Priority, Created (Last Hour → Last 30 Days), and Per Page (10–200, default 50). Filters persist across refresh and navigation.
- **Auto-refresh:** the table polls every 10s (HTMX) — statuses, live running-durations, and the summary cards update without a full reload; your filters, page, and scroll are preserved.

**Table columns:** type icon · Job ID (first 8 of the UUID) · Agent (`server` or agent name) · Status badge · Priority · Created (relative time) · Duration · Actions. The **Cancel** action shows for `pending`/`assigned`/`running`; **Delete** shows for all jobs. Both confirm first.

---

## Job detail panel

Click any row to open it. The panel shows:

- **Metadata:** full Job ID, type, agent, priority, created-at, and created-by.
- **Timing:** assigned-at, started-at, completed-at, duration, and expires-at.
- **Parameters** and **Results**, as formatted JSON. Running jobs show a spinner; failed jobs show the error.
- **Actions:** Cancel, Delete, Copy Results, and — for a completed `network_scan` — **Create Checks from Results**, which opens the bulk check form pre-filled with the discovered hosts so you can pick which to monitor.

---

## Execution notes

- **Where jobs run.** `network_discover` and `network_scan` run on an agent (which must be online and approved). Server-side jobs exist for bulk/API operations but have no access to agent networks.
- **Queueing.** Each agent runs up to 5 concurrent jobs by default; extras wait in `pending`/`assigned`. Order is by priority, then FIFO.
- **Timeouts.** Each type has a job-level timeout (`network_discover` 30s, `network_scan` 600s). Exceeding it marks the job `failed` with a timeout error.

---

## Common workflows

**Discover targets, then monitor them.** Run `network_discover` on the agent → copy a `suggested_scan` CIDR from the result → run `network_scan` on that CIDR with the ports you care about → on the completed scan, click **Create Checks from Results** and select hosts.

**Check connectivity.** Run `network_scan` with `ports` empty (ping-only), a low `timeout`, and high `max_concurrent`, then compare `hosts_responsive` against what you expect — missing hosts are unreachable from the agent.

**Audit open ports.** Run `network_scan` with just the port of interest (e.g. `3389`) and review which hosts list it in `ports_open`.

---

## Performance & tuning

`network_scan` speed depends on subnet size, port count, `max_concurrent`, network latency, and `timeout`.

- **Fast scan:** high `max_concurrent` (200–500), low `timeout` (5s), few ports.
- **Thorough scan:** lower `max_concurrent` (50–100), higher `timeout` (15s), more ports.
- **Large networks (`/16`):** keep `max_concurrent` lower so you don't overwhelm the agent.

Scans are network- and file-descriptor-heavy (one connection per concurrent host), with moderate CPU and low memory. If the agent shows CPU >80%, file-descriptor use >80%, queue depth >50, or jobs time out: reduce `max_concurrent`, raise `timeout`, split the scan into smaller subnets, or distribute scans across more agents.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Stuck in `pending` | Agent offline or not pulling jobs | Confirm the agent is online; check its logs; reload it; wait ~60s for the next pull |
| Stuck in `assigned` | Agent received it but is busy | Check agent resource use; wait, or cancel and retry at higher priority |
| Stuck in `running` | Long-running scan | Large scans can take 10+ min; check agent logs; if truly stuck, cancel and adjust parameters |
| `failed` with timeout | Exceeded the job timeout | Smaller subnet, higher `max_concurrent`, or split into multiple jobs |
| Scan finds no hosts | Wrong subnet, firewall, or network unreachable from the agent | Verify the CIDR; `docker exec luxswirl_agent ping <host>`; run `network_discover` first; allow ICMP from the agent |
| Incomplete results | `timeout` too low or hosts slow | Raise `timeout` (15–20s); lower `max_concurrent` |
| Can't delete a job | Insufficient permission, or already purged | Confirm your account can modify jobs; refresh (it may have auto-purged) |

---

## Not yet supported

Scheduled/recurring jobs — jobs are one-time today. To run them on a schedule, drive the API from an external scheduler (e.g. cron).

---

## Related

- [Agents](agents.md) — the agents that execute jobs
- [Checks](checks.md) — turning scan results into checks
- [Dashboard](dashboard.md) — monitoring newly discovered targets
