# Technical FAQ (Internal)

**For Developers/Maintainers Only**

This document explains implementation details, architectural decisions, and technical nuances that developers need to understand but users don't need to know about.

---

## Timeout Architecture

### Q: Why do we have so many different timeouts?

**A:** LuxSwirl has three separate timeout systems serving different purposes:

#### 1. Check Timeouts (Health Checks)
- **Purpose:** Control how long to wait for a check operation to complete
- **Configured:** Per-check in check configuration
- **Example:** `http_timeout: 30` means wait 30 seconds for HTTP response
- **Scope:** Individual check operations (ping, HTTP request, TCP connection, etc.)

#### 2. Job Timeouts (Background Jobs)
Jobs have **three layers** of timeouts:

**Layer 1: Job-Level Timeout** (`default_timeout_seconds`)
- **Purpose:** Kill ENTIRE job if it runs too long
- **Configured:** Class attribute on job class (e.g., `NetworkScanJob.default_timeout_seconds = 600`)
- **Example:** Network scan must complete in 10 minutes or get killed
- **Scope:** Entire job execution from start to finish

**Layer 2: Per-Operation Timeout** (job params)
- **Purpose:** Control timeout for each individual operation within the job
- **Configured:** Job parameter loaded from database settings (e.g., `job.network_scan_timeout`)
- **Example:** `timeout: 10` means wait 10 seconds per host in network scan
- **Scope:** Single operation (ping one host, scan one port, etc.)
- **User-configurable:** Yes, via job creation form

**Layer 3: Subprocess Cleanup Margins** (hardcoded)
- **Purpose:** Safety margins for process cleanup to prevent resource leaks
- **Configured:** Hardcoded in `get_subprocess_config()` defaults
- **Values:**
  - `grace_seconds: 2.0` - Extra time before SIGKILL after timeout expires
  - `kill_timeout: 5.0` - Max time to wait for process termination after SIGKILL
- **Scope:** Subprocess lifecycle management
- **User-configurable:** No, these are implementation details

**Example Timeline for Network Scan:**
```
Network scan job: timeout=10 per host, 254 hosts, job timeout=600s

Host 1 ping:
  0-10s: Wait for ping response (per-operation timeout from params)
  10-12s: Grace period before SIGKILL (subprocess cleanup)
  12-17s: Wait for process to die (subprocess cleanup)
  Max: 17s per host (10 + 2 + 5)

Total job:
  If scanning 254 hosts takes > 600s: Job killed (job-level timeout)
```

### Q: Why did `self.config` not exist on jobs?

**A:** Jobs (`BaseJob` subclasses) are instantiated with only `job_id` and `params`:
```python
job = NetworkScanJob(job_id="abc-123", params={"timeout": 10, "subnet": "10.0.0.0/24"})
```

When subprocess management was added (to fix process leaks), the code called:
```python
subprocess_config = get_subprocess_config(self.config)
```

But `BaseJob.__init__()` never set `self.config`. This was a latent bug that only manifested when:
1. Network scan UI was first used (new feature)
2. Jobs tried to execute subprocesses with the new safe wrapper

**Fix:** Changed to `get_subprocess_config({})` which uses hardcoded defaults (2s grace, 5s kill). The per-operation timeout comes from `self.params.get("timeout")` which is separate.

### Q: How do database settings relate to job timeouts?

**A:** Job creation flow:

1. **Form loads:** Router queries database settings:
   ```python
   timeout = await JobsService.get_setting(db, "job.network_scan_timeout", 10)
   max_concurrent = await JobsService.get_setting(db, "job.network_scan_max_concurrent", 100)
   ```

2. **Settings merged into prefill_params:**
   ```python
   final_defaults = {**schema_defaults, **db_settings, **prefill_data}
   ```

3. **Form renders:** Template uses values from `prefill_params`
   ```html
   <input name="timeout" value="{{ prefill_params.get('timeout') }}">
   ```

4. **User submits:** Form data sent to `/jobs/create`

5. **Transform applied:** `JobsService.transform_job_form_params()` (in `app/services/views/jobs_view_service.py`) converts form strings to types AND applies schema defaults for missing fields

6. **Job created:** Params stored in database
   ```sql
   INSERT INTO jobs (job_type, params, ...) VALUES ('network_scan', '{"timeout": 10, ...}', ...)
   ```

7. **Agent executes:** Job reads timeout from `self.params`:
   ```python
   timeout = self.params.get("timeout", 10)
   await run_subprocess_safely(*cmd, timeout=float(timeout), ...)
   ```

**Key Point:** Database settings are for UI defaults. Once a job is created, params are stored with the job record. The job execution uses `self.params`, not database settings.

---

## Subprocess Management

### Q: Why do we need subprocess cleanup margins?

**A:** Python's asyncio subprocess handling can leak resources:

**Problem:**
- Subprocess times out but process keeps running
- File descriptors leak (stdout/stderr pipes not closed)
- Zombie processes accumulate if not reaped
- Container hits file descriptor limits (default: 1024)

**Solution:**
```python
async def run_subprocess_safely(*args, timeout, grace_seconds, kill_timeout):
    # 1. Start subprocess
    proc = await asyncio.create_subprocess_exec(...)

    # 2. Wait for completion or timeout
    await asyncio.wait_for(proc.communicate(), timeout=timeout)

    # 3. On timeout: Give grace period, then SIGTERM
    await asyncio.sleep(grace_seconds)
    proc.terminate()

    # 4. Wait for termination, then SIGKILL if needed
    await asyncio.wait_for(proc.wait(), timeout=kill_timeout)
    proc.kill()

    # 5. Always reap zombie
    await proc.wait()
```

**Margins:**
- `grace_seconds`: Prevents race conditions where process exits just as we kill it
- `kill_timeout`: Prevents hanging on unkillable processes (rare but possible)

---

## Job Parameter Schema Defaults

### Q: When are schema defaults applied?

**A:** Schema defaults are applied in **two places**:

**Place 1: Form Rendering** (router)
```python
# app/web/routers/jobs_router.py
schema_defaults = {}
for field_name, field_info in properties.items():
    if "default" in field_info:
        schema_defaults[field_name] = field_info["default"]
```

**Place 2: Form Submission** (service)
```python
# app/services/views/jobs_view_service.py - JobsService.transform_job_form_params()
if field_name not in form_data:
    if "default" in field_info:
        params[field_name] = field_info["default"]
```

**Why two places?**
- Place 1: Pre-fills form with defaults (visual feedback to user)
- Place 2: Ensures defaults applied even if field missing from form_data

**Example:**
```python
# Schema definition
class NetworkScanParams(BaseModel):
    subnet: str
    ports: list[int] = [22, 80, 443, 3306, 5432, 8080, 8443]  # Default
    timeout: int = 10  # Default
    max_concurrent: int = 100  # Default

# If form only submits subnet (other fields use defaults):
form_data = {"subnet": "10.0.0.0/24"}

# After transform:
params = {
    "subnet": "10.0.0.0/24",
    "ports": [22, 80, 443, 3306, 5432, 8080, 8443],  # Applied from schema
    "timeout": 10,  # Applied from database setting OR schema default
    "max_concurrent": 100  # Applied from database setting OR schema default
}
```

### Q: What's the precedence order for defaults?

**A:** From lowest to highest priority:

1. **Schema defaults** (Pydantic model field defaults)
2. **Database settings** (`job.network_scan_timeout`, etc.)
3. **Prefill data** (from URL query params, e.g., clicking "Scan" button)
4. **Form input** (user manually types value)

```python
# Merge logic in router:
final_defaults = {**schema_defaults, **db_settings, **prefill_data}

# If user types value in form, it overrides everything:
if field_name in form_data:
    params[field_name] = form_data[field_name]
```

---

## Network Discovery Enrichment

### Q: Why does network_discover need enrichment?

**A:** The agent returns RAW network data (interfaces, gateways, ARP neighbors) but doesn't know what to DO with it. The server enriches this data with actionable suggestions.

**Agent returns (raw data):**
```json
{
  "default_gateway": "172.18.0.1",
  "interfaces": [
    {"name": "eth0", "ip": "10.50.0.77", "cidr": "10.50.0.0/24", "suggested_scan": "10.50.0.0/24"}
  ],
  "is_containerized": true
}
```

**Server enriches (adds intelligence):**
```json
{
  "default_gateway": "172.18.0.1",
  "interfaces": [...],
  "is_containerized": true,
  "inferred_network": "10.50.0.0/24",  // Added by enrichment
  "suggested_scan": "10.50.0.0/24",    // Added by enrichment
  "network_size": 256,                 // Added by enrichment (optional)
  "warning": "Agent in container mode..."  // Added by enrichment
}
```

**Why two suggested_scan fields?**
- `interfaces[0].suggested_scan`: Per-interface suggestion (from agent)
- `inferred_network`: Top-level suggestion (from server enrichment)

**UI uses inferred_network:**
```html
<!-- Top-level "Suggested Scan Target" card -->
{% if result.inferred_network %}
  <button hx-get="/jobs/create-form?job_type=network_scan&prefill_params={{ {'subnet': result.inferred_network}|tojson }}">
    Scan This Network
  </button>
{% endif %}

<!-- Per-interface "Scan" button -->
{% if iface.get('suggested_scan') %}
  <button hx-get="/jobs/create-form?job_type=network_scan&prefill_params={{ {'subnet': iface.suggested_scan}|tojson }}">
    Scan
  </button>
{% endif %}
```

**Enrichment strategies:**
1. **Strategy 1:** Container mode with real gateway (TTL tracing found host gateway)
   - Set `inferred_network` from gateway IP
2. **Strategy 2:** Host mode with interfaces
   - Copy first interface's `suggested_scan` to top-level `inferred_network`
3. **Strategy 3:** Container mode without gateway
   - Add warning, no scan suggestion

---

## Common Pitfalls

### Double-Init Bug (Fixed 2025-11-12)

**Problem:** JavaScript init functions called twice:
```html
<!-- base.html -->
<script>
  import { initAgents } from '/static/js/agents.js';
  initAgents();  // First call
</script>

<!-- pages/agents.html -->
<script>
  import { initAgents } from '/static/js/agents.js';
  initAgents();  // Second call - DUPLICATE!
</script>
```

**Impact:** Double event listeners registered. Delete buttons executed on SINGLE click instead of double-click.

**Fix:** Remove duplicate init() calls from page-specific templates. Init once in base.html.

### CSRF Token Rotation

**Symptom:** CSRF validation fails intermittently after page actions.

**Cause:** Agent approval flow triggers page reload which creates NEW CSRF token. Old token in pending forms becomes invalid.

**Solution:** Use `await db.expire_all()` after commit to force SQLAlchemy to reload fresh data, then reload page immediately. New page load gets new CSRF token.

### Template Hardcoded Fallbacks

**Anti-pattern:**
```html
<input name="timeout" value="{{ prefill_params.get('timeout', 10) }}">
```

The `10` is a hardcoded fallback that bypasses database settings!

**Correct:**
```html
<input name="timeout" value="{{ prefill_params.get('timeout') }}">
```

Router loads database settings into `prefill_params` so fallback is unnecessary.

---

## Debugging Tips

### Check if job params are being applied:

```sql
-- View job params in database
SELECT id, job_type, params
FROM jobs
WHERE job_type = 'network_scan'
ORDER BY created_at DESC
LIMIT 5;
```

### Check if database settings are loaded:

```sql
-- View job-related settings
SELECT key, value
FROM settings
WHERE key LIKE '%job%' OR key LIKE '%network_scan%'
ORDER BY key;
```

### Trace job execution:

```bash
# Watch agent logs during job execution
docker logs -f luxswirl_agent | grep -E "network_scan|timeout|subprocess"

# Check server logs for enrichment
docker logs -f luxswirl_server | grep -E "Enriched results|network_discover"
```

### Check if form defaults are loaded:

Open browser DevTools → Network tab → Click "New Job" button → Look at response:
```json
{
  "prefill_params": {
    "timeout": 10,  // Should match database setting
    "max_concurrent": 100  // Should match database setting
  }
}
```

---

**Maintainer Notes:**

This document should be updated whenever:
- New timeout types are added
- Job parameter handling changes
- Subprocess management is modified
- Bugs related to these systems are discovered and fixed

