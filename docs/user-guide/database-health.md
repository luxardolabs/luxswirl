# Database Health

The Database Health page provides monitoring and metrics for TimescaleDB performance, storage, compression, and retention policies.

**Access:** **Database Health** in the sidebar, or `/database-health`. Requires an **admin** account.

The page has four parts: a **Storage Breakdown** grid (a card per hypertable/table with size, chunk count, and compression status — `check_results`, `check_results_hourly`/`_daily`, `agent_metrics`, `check_artifacts`, and a Total Database summary), an **Active Retention Policies** section, a **Data Growth Over Time** chart, and a **Refresh** button (top-right) to re-pull all metrics after maintenance.

---

## Storage concepts

### Hypertables vs regular tables

| | Hypertable (TimescaleDB) | Regular table (PostgreSQL) |
|---|--------------------------|----------------------------|
| Data | Time-series, partitioned by time | Standard relational |
| Partitioning | Automatic, into time **chunks** | None |
| Compression / retention | Supported (per-chunk) | Manual |
| Optimized for | Time-range queries | General queries |
| Examples | `check_results`, `agent_metrics` | `checks`, `agents`, `check_artifacts` |

### Chunks

TimescaleDB partitions each hypertable into chunks, each covering a time interval (default 1 day for `check_results`). Chunks can be compressed individually, and retention drops entire chunks. A higher chunk count means more granular time coverage — e.g. 90 chunks with 1-day chunks ≈ 90 days of data. Chunks matter because time-range queries only scan the relevant chunks, compression operates per chunk, and retention drops whole chunks at once.

### Compression

TimescaleDB compresses old chunks using columnar compression (typically **80–90%** reduction). Compressed chunks are read-only but still queryable, and compression runs automatically via a policy. The **compression ratio** shown on the Total Database card is the share of chunks compressed:

```
ratio = (compressed_chunks / total_chunks) × 100
Total: 100   Compressed: 85   Uncompressed: 15   →   85%
```

Typical savings: check results 80–90%, agent metrics 85–95% (e.g. 1 GB uncompressed → ~100 MB compressed). The status shows **Compressed** (green) or **Uncompressed** (gray) per hypertable.

### Retention

A retention policy automatically deletes data older than a set duration, operating at the chunk level (drops entire chunks), on a schedule (typically daily). It prevents unbounded growth. Duration is configured in Settings → Defaults → Database Settings; the default for `check_results` is 90 days (range 1–3650 days, i.e. up to 10 years).

```
Hypertable: check_results   Retain: 90 days
Effect: data older than 90 days is automatically deleted
```

> ⚠️ Retention is permanent — deleted data cannot be recovered. Keep backups if you need the history.

---

## Storage breakdown details

### `check_results` (hypertable)

The primary time-series data — every check execution result.

- **Structure:** partitioned by the `timestamp` column; chunk interval 1 day (default); composite primary key `(id, timestamp)`.
- **Typical size:** ~1.2 KB per result. Example: 100 checks × 60s interval × 1,440 min/day = 144,000 results/day ≈ 170 MB/day.
- **Compression:** compresses chunks older than 7 days (default); 80–90% reduction; compressed chunks remain queryable.
- **Retention:** default 90 days (configurable).
- **Growth factors:** number of checks, check interval (60s makes more than 300s), and result size (JSON checks with large responses).

### `agent_metrics` (hypertable)

Agent performance metrics (CPU, memory, queue depth).

- **Structure:** partitioned by `timestamp`; reports sent every 60 seconds per agent.
- **Typical size:** ~5–10 KB per report. 1 agent × 60s = 1,440 reports/day ≈ 7–15 MB/day; 10 agents ≈ 70–150 MB/day.
- **Compression:** 85–95% typical.
- **Retention:** default 30 days (shorter than `check_results` — less critical long-term).

### `check_artifacts` (regular table)

Binary artifacts — synthetic-check screenshots and large response bodies.

- **Structure:** a regular PostgreSQL table (not a hypertable); binary data in a `data` `bytea` column; indexed by `created_at` for cleanup.
- **Typical size:** highly variable. Screenshots 50–500 KB each; e.g. 10 synthetic checks × 60s × 200 KB ≈ 2.8 GB/day.
- **Retention:** manual (no TimescaleDB policy) — configured in Settings → Defaults → Database Settings and cleaned by a scheduled daily job.
- **Growth factors:** number of synthetic checks, screenshot frequency/size, large responses stored as artifacts.

### `check_results_hourly` and `check_results_daily` (continuous aggregates)

Pre-computed rollups for faster dashboard queries.

- **Hourly:** aggregates results by hour; used for 24-hour and 7-day dashboard views.
- **Daily:** aggregates by day; used for 30-day, 90-day, and 1-year views.
- **Benefits:** faster dashboard loads, simpler queries, lower load on raw data for historical views.
- **Storage:** much smaller than raw data — e.g. 1 GB raw → ~10 MB hourly → ~1 MB daily.
- **Refresh:** maintained automatically by TimescaleDB refresh policies.

---

## Data growth chart

Visualizes how storage increases over time. **Time ranges:** Last 24 Hours (hourly buckets), Last 7 Days (daily, default), Last 30 Days, Last 90 Days, Last Year (weekly buckets). **Series:** Results (blue), Artifacts (purple), Checks (green, minimal), Agent Metrics (orange), and a combined Total line. Y-axis is storage in MB; hover for exact values.

### Interpreting the chart

| Pattern | Meaning |
|---------|---------|
| **Steady linear growth** | Normal — consistent check execution, predictable storage needs |
| **Sudden spikes** | New checks added, increased frequency, large artifacts stored, or retention not running |
| **Flattening (plateau)** | Retention is working — old data dropped as fast as new data is added (the desired steady state) |
| **Decreasing** | Compression activating and reclaiming storage |

```
Steady:   Day 1: 100 MB → Day 2: 125 MB → Day 3: 150 MB   (~25 MB/day)
Plateau:  Day 1–30: 0 → 3 GB, then Day 31–60 flat at ~3 GB  (retention kicking in at 30 days)
```

### Capacity planning

```
Daily rate (7-day view):
  7 days ago: 2,500 MB    Today: 2,675 MB
  (2,675 − 2,500) / 7 = 25 MB/day

Project:
  Current: 2,675 MB    Daily growth: 25 MB
  Days until 10 GB: (10,000 − 2,675) / 25 = 293 days
```

If growth is too fast, reduce the retention period (Settings → Defaults → Database Settings → Check Results Retention Days) or lengthen check intervals.

---

## Compression statistics

The page shows total chunks, compressed chunks, uncompressed chunks, and the compression ratio. Recent chunks remain uncompressed for fast writes; older ones get compressed.

```
Total chunks: 100   Compressed: 85   Uncompressed: 15   Ratio: 85%
```

**How compression runs:** a policy compresses chunks older than a threshold (default 7 days), automatically on schedule:

1. A chunk ages past the threshold (e.g. 7 days old).
2. The compression policy job triggers.
3. The chunk is compressed atomically using columnar format.
4. The original uncompressed data is deleted.
5. The compressed chunk remains queryable (read-only).

**Performance:** compressed chunks are read-only (slower writes) but read similarly or faster (columnar). Recent uncompressed data is unaffected.

**Configuration** (Settings → Defaults → Database Settings → "Compression After Days", default 7):

```
Compress After Days: 3   → less storage, chunks become read-only sooner
Compress After Days: 14  → more storage, chunks stay writable longer
```

**Best practice:** compress after your typical data-update window. If you never update old data, compress aggressively (3–7 days).

---

## Retention policies

The **Active Retention Policies** section shows each hypertable and its retention (e.g. `check_results` → 90 days, `agent_metrics` → 30 days).

**How retention runs** (on schedule, typically daily ~3 AM):

1. The policy job runs.
2. It identifies chunks older than the retention period.
3. It drops entire chunks atomically.
4. Storage is reclaimed immediately.

```
Retention: 90 days   Today: 2025-11-11
→ chunks older than 2025-08-13 are dropped
```

**Configuring** (Settings → Defaults → Database Settings):

- **Check Results Retention Days** — default 90, range 1–3650.
- **Notification Logs Retention Days** — default 90, range 1–3650.

A change takes effect on the next scheduled run, which drops anything older than the new duration. (Reducing 90 → 30 means the next run drops all data older than 30 days.)

> ⚠️ Data deleted by retention cannot be recovered — ensure backups if you need the history.

### Retention vs compression

They serve different purposes — **compression** reduces storage while keeping data; **retention** deletes old data permanently. The typical lifecycle:

```
Day 0–7:   uncompressed, writable        Day 1:  1 GB uncompressed
Day 7–90:  compressed, read-only         Day 8:  100 MB compressed (90% saved)
Day 90+:   deleted by retention          Day 90: 100 MB → Day 91: 0 MB (deleted)
```

---

## Table row counts and sizes

Not shown in the UI but available via the API or direct queries, for key tables (`check_results`, `checks`, `agents`, `check_artifacts`): row count, oldest record, newest record, raw size (bytes), and a human-readable size.

```
check_results:
  Row count: 97,092
  Oldest: 2025-11-07 10:00:00   Newest: 2025-11-11 15:32:01
  Size: 459 MB
```

---

## Troubleshooting

### Database growing too fast

Storage is increasing rapidly. Likely causes: too many checks (100+ at 60s), short intervals (10–30s generate massive data), large synthetic artifacts, retention not running, or compression disabled.

1. **Review check count and intervals** — Checks page, sort by interval; raise intervals for non-critical checks (60s → 300s ≈ 80% less data).
2. **Enable and verify compression** — the breakdown should show "Compressed" for older hypertables; if "Uncompressed", enabling the policy requires a database migration.
3. **Reduce retention** — Settings → Database Settings; lower Check Results Retention Days from 90 to 30/60 (less history, less storage).
4. **Tame synthetic artifacts** — reduce synthetic frequency or stop capturing screenshots you don't need; delete old artifacts.
5. **Monitor the rate** — Data Growth Chart (7 days), compute MB/day, project ahead.

### Compression not working

Storage isn't decreasing, ratio is low, chunks show "Uncompressed".

```sql
-- Is the compression policy present?
SELECT * FROM timescaledb_information.jobs WHERE proc_name = 'policy_compression';

-- Which chunks are compressed? (older-than-threshold chunks should be)
SELECT chunk_name, range_start, range_end, is_compressed
FROM timescaledb_information.chunks
WHERE hypertable_name = 'check_results'
ORDER BY range_end DESC;

-- Manually run the job (get <job_id> from the jobs query above)
CALL run_job(<job_id>);
```

If no policy exists, enabling compression requires a database migration.

### Retention policy not running

The database keeps growing and old data isn't deleted.

```sql
-- Is the retention policy present?
SELECT * FROM timescaledb_information.jobs WHERE proc_name = 'policy_retention';

-- Manually run it
CALL run_job(<job_id>);

-- Add one if missing
SELECT add_retention_policy('check_results', INTERVAL '90 days');
```

Also confirm the Active Retention Policies section shows a configured duration; if it's missing, the policy isn't configured.

### High artifact storage

`check_artifacts` is very large (>1 GB) and growing — synthetic checks storing many large screenshots (10 checks × 60s × 200 KB ≈ 2.8 GB/day).

1. **Review synthetic check count** — Checks page, filter Type = Synthetic.
2. **Reduce frequency** — increase intervals (60s → 300s/600s) to cut artifact generation 80–90%.
3. **Disable screenshots** if not needed — edit the synthetic script to stop capturing them.
4. **Manual cleanup:**
   ```sql
   DELETE FROM check_artifacts WHERE created_at < NOW() - INTERVAL '7 days';
   ```
5. **Configure artifact retention** — Settings → Database Settings (if available).

### Chart not loading

Spinner forever or an error. Causes: no data (fresh install), the range too large for the data present, a query timeout, or a JS error.

1. Check the browser console (F12 → Console) for JS errors or failed requests.
2. Try a shorter range (24 Hours / 7 Days).
3. Verify data exists — the breakdown should show non-zero `check_results` size.
4. Check the network tab for the `/database-health/chart-data` request and its response.

---

## API

`Authorization: Bearer {token}`; full schemas at `/docs`.

**Health summary** — `GET /api/v1/database-health`

```json
{
  "database_size": { "total_size_bytes": 481689600, "total_size_pretty": "459 MB" },
  "hypertables": [
    { "name": "check_results", "compression_enabled": true, "chunk_count": 4, "total_size": "420 MB" }
  ],
  "compression": { "total_chunks": 100, "compressed_chunks": 85,
                   "uncompressed_chunks": 15, "compression_ratio": 85.0 },
  "retention_policies": [ { "hypertable": "check_results", "drop_after": "90 days" } ],
  "row_counts": {
    "check_results": { "row_count": 97092, "oldest_record": "2025-11-07T10:00:00Z",
                       "newest_record": "2025-11-11T15:32:01Z",
                       "size_bytes": 481689600, "size_pretty": "459 MB" }
  },
  "daily_growth": [ { "date": "2025-11-11", "row_count": 15234 },
                    { "date": "2025-11-10", "row_count": 14876 } ]
}
```

**Growth chart data** — `GET /api/v1/database-health/chart-data?hours=168` (`hours`: 24, 168, 720, 2160, 8760)

```json
{
  "data": [
    { "timestamp": "2025-11-04T00:00:00Z", "results_mb": 350.5, "artifacts_mb": 12.3,
      "checks_mb": 0.5, "agent_metrics_mb": 5.2, "total_mb": 368.5 },
    { "timestamp": "2025-11-05T00:00:00Z", "results_mb": 375.2, "artifacts_mb": 13.1,
      "checks_mb": 0.5, "agent_metrics_mb": 5.8, "total_mb": 394.6 }
  ]
}
```

---

## Best practices

**Storage management:**
- Review the Database Health page weekly; track the growth rate via the chart; catch anomalies early.
- Enable compression on all hypertables, with a 7-day threshold (balance writable period vs storage); keep the ratio above 80%.
- Set retention by business need and capacity — start at 90 days, shorter for `agent_metrics` (30) than `check_results` (90).
- Calculate the daily growth rate monthly and project storage 6–12 months ahead; upgrade storage before hitting capacity.

**Performance:**
- Lean on continuous aggregates (hourly/daily) for dashboards — they cut query load on raw data and are maintained automatically.
- Keep proper indexes on hypertables and regularly VACUUM/ANALYZE.
- Size the connection pool sensibly (Settings; default 20) — balance concurrency and resource use.

**Backup and recovery:**
- Automate daily backups, test restore procedures, and store backups off-site.
- Data dropped by retention can't be recovered from backups older than the retention window — back up before reducing retention.
- Document your compression and retention policies in your disaster-recovery plan and test a full restore.

---

## Related

- [Settings](settings.md) — configuring retention and compression
- [Dashboard](dashboard.md) — how aggregates speed up historical views
- [Agents](agents.md) — agent-metrics storage
