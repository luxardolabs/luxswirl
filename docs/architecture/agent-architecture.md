# Agent Architecture & Check Assignment

## Overview

LuxSwirl supports three check assignment modes to handle different monitoring scenarios. The mode is stored per-check on the `Check` model via the `assignment_mode` column and selected agents are matched via the `agent_selector` JSON column. All three modes are implemented and shipped.

1. **MANUAL** (default) - Check runs on one specific agent (`Check.agent_id` FK).
1. **REPLICATE** - Same check runs from every agent matching the selector (multi-region monitoring, redundancy).
1. **DISTRIBUTE** - Checks are split across the agents matching the selector (load sharing, HA).

The assignment logic lives in `apps/backend/app/services/core/agent_assignment_core_service.py` (`AgentAssignmentCoreService`). Agents fetch their work via `GET /api/v1/agent/checks?agent_id={agent_id}` (`apps/backend/app/api/v1/routers/check_router.py`), which calls `AgentAssignmentCoreService.check_list_for_agent`, and that calls `get_checks_for_agent(db, agent)`.

## Use Cases

### Use Case 1: Multi-Region External Monitoring (REPLICATE)

**Scenario:** Monitor external services from multiple geographic locations

```
Check: http_google
Assignment Mode: replicate
Agent Selector: {"tags": ["role:external-monitor"]}

Agents:
- london-agent (tags: role:external-monitor,region:london)
- tokyo-agent  (tags: role:external-monitor,region:tokyo)
- nyc-agent    (tags: role:external-monitor,region:nyc)

Result: All 3 agents run http_google.
You see: "Google is reachable from London (50ms), Tokyo (120ms), NYC (30ms)"
```

**Benefits:**

- Geographic latency monitoring
- Detect regional outages
- Verify global availability

### Use Case 2: Load Distribution & High Availability (DISTRIBUTE)

**Scenario:** Split monitoring work among multiple agents for capacity and redundancy

```
Checks: [ping_device1, ping_device2, ..., ping_device50]
Assignment Mode: distribute
Agent Selector: {"tags": ["location:home"]}

Agents:
- home-agent-1 (tags: location:home)
- home-agent-2 (tags: location:home)

Result: each check is hashed to exactly one of the two agents
(~25 checks each).

If home-agent-1 stops heartbeating:
- After 3 missed heartbeats plus 60 s it counts as offline (at once, if its
  last heartbeat said it was shutting down), and its checks move to
  home-agent-2 (only its checks: home-agent-2 keeps its own).
- home-agent-2 learns at its next heartbeat (its config_version changes)
  and fetches its new check list.
- When home-agent-1 heartbeats again, its checks move back to it.
```

**Failover.** A distributed check runs on the highest-weighted agent of its pool that is online, by the same rule the silence alerts and the dashboard use (`app.core.liveness`: an agent is offline once its newest heartbeat is older than 3 of its heartbeat intervals plus 60 s). One lost heartbeat therefore moves nothing. An agent whose latest heartbeat said it was shutting down (status `offline`) hands over at once, since nothing will run its checks. The heartbeat's `config_version` is a digest of the agent's check list, an empty one included, and the agent re-fetches its checks whenever it changes. It also re-fetches them once its check sync interval has passed since the last fetch, version changed or not, so a change that reached the server without moving the version is picked up too (checked at each heartbeat, so at most one heartbeat late). A re-fetch keeps each check's schedule. When every agent of the pool is offline, the check stays with its first choice, and the Agent Offline alert names that agent. Any agent of the pool may report the check's results: a result is measured before it reaches the server, so the owner's results buffered through an outage and the stand-in's last batch both arrive after the check has moved.

**Benefits:**

- Distribute load across multiple agents
- Scale by adding more agents
- Works at any scale (2 agents / 50 checks OR 4 agents / 1000 checks)

### Use Case 3: Capacity Scaling (DISTRIBUTE)

**Scenario:** Monitor large infrastructure (Puppet nodes, Kubernetes clusters, etc.)

```
Checks: [puppet_node_001, ..., puppet_node_1000]
Assignment Mode: distribute
Agent Selector: {"tags": ["role:puppet-monitor"]}

Agents:
- puppet-monitor-1 .. puppet-monitor-4 (tags: role:puppet-monitor)

Result: each check hashes to ~one of 4 agents (~250 checks each).
Add a 5th matching agent → checks rehash toward ~200 each.
```

**Benefits:**

- Scale horizontally by adding agents
- Hash-based rebalancing as the pool changes
- No manual per-check assignment

## Technical Implementation

### Check Model (shipped)

The relevant columns on `Check` (`apps/backend/app/models/check_model.py`):

```python
class Check(UUIDBaseModel):
    # UUID primary key `id` inherited from UUIDBaseModel.

    agent_id: Mapped[UUID]            # FK -> agents.id (used by MANUAL mode)
    depends_on_check_id: Mapped[UUID | None]  # FK -> checks.id (dependency suppression)
    display_name: Mapped[str]         # friendly, editable name

    assignment_mode: Mapped[str]      # "manual" | "replicate" | "distribute"
                                      # NOT NULL, server_default "manual"
    agent_selector: Mapped[dict | None]  # JSON: {"tags": [...]} or {"agent_ids": [...]}
```

**Modes:**

- `manual` (default) - check runs on the agent referenced by `Check.agent_id`.
- `replicate` - check runs on ALL agents matching `agent_selector`.
- `distribute` - check runs on ONE agent from the matching pool, chosen by hash.

### Rendezvous-Hash Distribution Algorithm (shipped)

For **DISTRIBUTE** mode, `AgentAssignmentCoreService.get_assigned_agent_for_check` picks the responsible agent using rendezvous (highest-random-weight / HRW) hashing:

```python
@staticmethod
def get_assigned_agent_for_check(check, available_agents):
    if not available_agents:
        return None

    def _score(agent):
        digest = hashlib.sha256(f"{check.id}:{agent.id}".encode()).digest()
        # Tie-break on the agent UUID for full determinism.
        return (int.from_bytes(digest, "big"), str(agent.id))

    return max(available_agents, key=_score)
```

**Why HRW + sha256 over the UUIDs:**

- **Process-independent.** `sha256` is not seeded, so the mapping is identical across server restarts, multiple uvicorn workers, and replicas. (The previous implementation used the builtin `hash()` over `check.display_name`, which is `PYTHONHASHSEED`-salted per process — stable only within a single process, and prone to overlapping/conflicting assignments across workers.)
- **Keyed on the immutable `check.id`, not `display_name`.** `display_name` is editable and not unique, so hashing it meant a rename silently reassigned the check and same-named checks always co-located. The UUID is the stable identity.
- **Minimal reshuffle on pool change.** With HRW, when an agent joins or leaves the matching pool, only the checks owned by that agent move; everyone else stays put. Plain `hash % n` reshuffles *every* check whenever the pool size changes.

`distribute_owner(check, pool, now)` calls this with the pool's online agents (the whole pool when none is online), so an offline agent's checks go to the next-highest weight among the others.

### Agent Check Fetch Logic (shipped)

`AgentAssignmentCoreService.get_checks_for_agent(db, agent)` builds the per-agent list. It reads only the candidates the agent could run (`CheckCRUD.list_assignment_candidates`: its own manual checks plus every replicate/distribute check), never the whole table. Simplified:

```python
async def get_checks_for_agent(db, agent):
    checks_to_run = []
    all_checks = await CheckCRUD.list_assignment_candidates(db, agent.id)

    for check in all_checks:
        if check.assignment_mode == "manual":
            if check.agent_id == agent.id:          # FK match
                checks_to_run.append(check)

        elif check.assignment_mode == "replicate":
            if agent_matches_selector(agent, check.agent_selector):
                checks_to_run.append(check)

        elif check.assignment_mode == "distribute":
            if agent_matches_selector(agent, check.agent_selector):
                pool = filter_agents_by_selector(active_agents, check.agent_selector)
                owner = distribute_owner(check, pool, now)
                if owner and owner.id == agent.id:
                    checks_to_run.append(check)

    return checks_to_run
```

Note that all comparisons use UUID primary keys (`check.agent_id == agent.id`, `assigned.id == agent.id`), not name strings.

### Selector Matching (shipped)

`AgentAssignmentCoreService.agent_matches_selector`:

```python
@staticmethod
def agent_matches_selector(agent, selector):
    if not selector:
        return False

    # Specific agents by friendly name.
    if "agent_ids" in selector:
        return agent.agent_name in selector["agent_ids"]

    # Tag-based.
    if "tags" in selector:
        if not agent.tags:
            return False
        agent_tags = {t.strip() for t in agent.tags.split(",") if t.strip()}
        required = set(selector["tags"])
        match_mode = selector.get("match_mode", "all")   # "all" (AND) or "any" (OR)
        if match_mode == "any":
            return bool(required.intersection(agent_tags))
        return required.issubset(agent_tags)

    return False
```

Selector keys:

- `agent_ids` - list of `Agent.agent_name` values (the friendly name, not the UUID).
- `tags` - list of `key:value` tags; `match_mode` is `"all"` (default, AND) or `"any"` (OR).

### Agent Tags

`Agent.tags` is a comma-separated string (`apps/backend/app/models/agent_model.py`).

Tags are admin-managed. An agent declares tags at registration, and its heartbeat may update them while it is pending, as a suggestion the admin sees when approving. From approval on, a heartbeat's tags never change the row (`AgentCoreService._apply_heartbeat`); only an admin does, through the agent's Edit panel. Tags decide which replicate and distribute checks GET `/api/v1/agent/checks` hands the agent and which it may report on, so an agent must not be able to retag itself into another group's checks.

**Tag format:** `key:value` pairs, comma-separated

```
Examples:
- "role:external-monitor,region:london"
- "role:puppet-monitor,pool:puppet-pool"
- "location:home,role:internal-monitor"
```

## Result Reporting

The agent's reporter (`apps/agent/app/agent/reporter.py`) batches check results and POSTs them to `/api/v1/agent/reports`. Before a result is queued it is fitted to the server's per-result limits (`apps/agent/app/agent/result_limits.py`): an `error` over 4,000 characters or a `response_data` over 65,535 is truncated with a `... [truncated]` marker, and an `http_status_code` outside 100-599 (a real server can answer 999) is sent as `null`, with the code itself in `metrics.http_status_code_raw`.

The server validates each result in a report on its own. One it still cannot store is counted in `results_failed` and logged (the result and the failing fields, never the values), like a result for a check the agent was not assigned, for a check deleted while the report was in flight, or timestamped more than five minutes past the server's clock (`RESULT_CLOCK_TOLERANCE`: every reader takes a check's newest result as its status, so a future-dated one would stand until the clock reached it); the rest of the report is stored. An agent deleted while its report is in flight is answered 404 `NOT_FOUND`, which the agent keeps for replay. The report locks the checks its results reference, then the agent, FOR KEY SHARE in id order, and every writer of many checks (agent delete, bulk delete, bulk enable/disable, the stale-agent cleanup) locks them in id order first, so none of them deadlocks a report (`CheckCRUD.lock_for_delete`, `key_share_existing`). The agent's row, read FOR KEY SHARE as stored then, is where the report is judged again (`AgentCoreService.require_still_admitted`): a key revoked or regenerated, or the agent paused, disabled or archived, since the key was checked refuses the report (401, 403), and none of those can land after that read before the report commits, since each locks the row. The report decides no alert: in its own transaction, after the insert, it queues the key of each stored result an enabled alert evaluates in `alert_decision_queue` (`AlertDecisionCoreService.queue_report_results`), takes no decision lock and writes no notification, so the agent's row and its checks are held only for the report's reads and inserts: an operator can always stop the agent, a report never waits on another's decisions, and the agent is answered as soon as its results are stored. A result already stored (a report sent again after its answer was lost) is queued again. The alert decision worker (`app/background/alert_decision_worker.py`, started in the lifespan beside the delivery worker) then takes the checks with queued results, the longest waiting first, and decides each check in a short transaction of its own (`AlertDecisionCoreService.decide_check`): it tries the check's decision lock (an advisory lock per check, `decision_lock_key`) and leaves a check another worker holds for the next pass, locks the check FOR KEY SHARE, takes its oldest queued results (at most `RESULTS_PER_DECISION`) and evaluates each one newer than the check's cursor (`alert_decision_cursors.decided_through`) oldest first, in its own savepoint, against the decisions already committed; each notification decided is a `pending` row of `notification_logs`. The entries taken are removed and the cursor moved to the newest of them in the same transaction, so a decision is never lost (a transaction that fails leaves its results queued) and never made twice. A queued result no newer than the cursor is stale (a newer result of the check was decided first: a replicate check's other agent, a delayed report) and is dropped undecided, so a delayed result never sets an alert back to a state its check has left, and one outage is notified once across overlapping reports and agents. A check with no enabled alert queues nothing and has nothing decided; an alert attached to it later decides the results stored after. A check whose alerts are all disabled, deleted or taken off it since its results were queued has its whole queue dropped undecided at its next pass, its cursor moved past it, so no check holds queued rows longer than one pass; the pass lists the checks with queued results from the queue's primary key alone, one probe per check however many results each has queued (`AlertDecisionCRUD.checks_with_queued`). Migration 022 starts every existing check's cursor at its newest stored result: those results were decided by the reports that stored them, so one sent again is stale, while a result an agent buffered while the server was down for the upgrade is newer and is decided when replayed. A status change is judged against the check's results up to that result, and a recovery is sent only when the alert has notified something for the check. The delivery worker (`app/background/notification_delivery_worker.py`) keeps a pool of `DELIVERY_SLOTS` (20) attempts in flight and claims more as slots free, in turn across providers (the provider with the fewest attempts in flight first), with at most `PROVIDER_SHARE` (10) in flight for one provider counted across every server process (its `sending` rows under a live lease), and a provider with nothing in flight given its first due row even when every slot is held (one attempt past the slots), so providers whose every attempt waits out its timeout, however many, never hold up another's: rows are claimed FOR UPDATE SKIP LOCKED in a short transaction and marked `sending` under a lease, each is delivered with no transaction open, and each outcome is recorded as it ends in another short transaction, retrying a failure that may pass with backoff; leases are judged by the database's clock, and a graceful stop hands back the rows it cut off, due again at once; one alert and check's notifications reach a provider in the order they were decided. At its claim a row is judged as the alert stands then: a payload this release cannot read fails that row alone (`Unreadable payload`); an alert, provider or check disabled or deleted, a provider or check no longer on the alert, or the pair snoozed suppresses it; and the provider's rate limit counts the deliveries in its window by when they were delivered (`delivered_at`) plus every other one being sent under a live lease (never the row itself, which a stopped worker leaves `sending`), so a backlog decided before the window is still held to it. Only a bad envelope (an invalid `agent_id`, more than 10,000 results, a result that is not a JSON object) refuses the whole report with 422.

Each response is classified (`classify_response` in `apps/agent/app/agent/dead_letter.py`):

| Outcome       | Response                                                                                        | What happens to the batch                                                         |
| ------------- | ----------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| **Sent**      | 2xx                                                                                             | Done                                                                              |
| **Permanent** | 400 or 422 with the server's JSON error envelope, `error` = `VALIDATION_ERROR` or `BAD_REQUEST` | Not retried, not stored: moved to the `dead_letters` table with an ERROR log line |
| **Too large** | 413 (a proxy's body limit, such as nginx `client_max_body_size`; the app never sends 413)       | Sent in halves, each halved again while still refused (below)                     |
| **Transient** | everything else: network errors, 5xx, 408, 429, 401/403, any 4xx without that envelope          | Retried `report_max_retries` times, then stored in `pending_batches` for replay   |

Only the server itself can say a payload is invalid. The server's trusted-host middleware answers a request whose `Host` is not in `allowed_hosts` with a plain-text `400 Invalid host header`, and a proxy such as nginx answers with its own HTML 400. Those mean the agent is misaddressed or the route is misconfigured, so the batches stay pending and are delivered once it is fixed, instead of being dead-lettered and pruned by the cap. Auth failures (401/403) stay transient too: the agent's key-recovery path can fix them.

A 413 is never dead-lettered either. The agent sends the batch in halves, splitting each again while it is still refused. A single result the limit still refuses is kept pending and the split goes on past it: a result's long text fields are capped far below any body limit, so that usually says the route is wrong. Two refused results with nothing sent stop the split, as a transient failure does. What was not sent is stored as one batch, refused results last so the next try reaches the others first; splitting never adds stored rows.

Both tables live in one SQLite file, `reports/pending_reports.db` (WAL, gzip-compressed payloads, every query through `asyncio.to_thread`):

- **`pending_batches`** — capped by `report_max_stored_batches` (default 10,000). A background task replays the oldest batches, combined up to `report_batch_size` results per POST. If a combined POST is refused permanently or is too large, each batch in it is retried alone: one the server takes is deleted, one it refuses moves to `dead_letters`, and one still too large is sent in halves, with what was not sent written back into the same row (same row, same age, so the cap never prunes another row for it). A row the size limit still holds is passed over by later reads until nothing else is left, then retried, so it never fills the read window. A transient failure ends the pass and leaves the rest pending. Replay never stops at a refused or oversized batch.
- **`dead_letters`** — the original `created_at`, `failed_at`, `status_code`, the server's error detail (scrubbed by `shared/log_redaction.py`, at most 2 KB) and the gzip payload. Capped by `report_max_dead_letters` (default 1,000), oldest pruned first.

Each heartbeat carries `dead_letter_count`. The server stores it on `Agent.dead_letter_count`, and the agents page shows a yellow "N dead-lettered" badge on the agent's card when it is above zero.

## Data Model

### Agent Model (shipped)

`Agent` has a UUID primary key `id` and a friendly, editable `agent_name` (nullable until an admin assigns it during approval). Relevant assignment and auth fields:

```python
class Agent(UUIDBaseModel):
    agent_name: Mapped[str | None]    # unique friendly name (set on approval)
    tags: Mapped[str | None]          # comma-separated key:value tags
    approval_status: Mapped[str]      # pending | active | paused | disabled | archived | rejected

    # Per-agent API key (shipped)
    api_key_hash: Mapped[str | None]        # bcrypt hash of the agent's key
    api_key_created_at: Mapped[datetime | None]
    api_key_last_used: Mapped[datetime | None]
```

## Authentication

Agents authenticate with keys, never a user session:

1. **Per-agent API keys** — each agent has its own key, stored as a bcrypt hash in `Agent.api_key_hash` (with `api_key_created_at` / `api_key_last_used`). Agent-facing endpoints (e.g. `GET /api/v1/agent/checks?agent_id={agent_id}`) authenticate the agent via `verify_agent_token`, which also enforces the agent's `approval_status`. The bcrypt check, and the hash of each key issued, run on a worker thread (`bcrypt_matches_threaded`, `bcrypt_hash_threaded`), so a wrong key sent by anyone who knows an agent id never stalls the event loop. This enables per-agent revocation and isolation.

1. **Shared registration keys** — created by an admin under **Settings → API Keys & Metrics** (`RegistrationKeyCoreService`); the plaintext key is shown once at creation and stored only as a hash. An agent presents one to register (`POST /api/v1/agent/register`) and to recover its own key (`POST /api/v1/agent/{agent_id}/recover-key`).

1. **Per-agent recovery secrets** — registration returns each agent a random recovery secret once; the server stores only its SHA-256 digest (`Agent.recovery_secret_hash`). recover-key needs the registration key **and** that agent's secret (header `X-Agent-Recovery-Secret`), compared in constant time, so one install holding the shared registration key cannot take over another agent by its id. The secret is also a pending agent's heartbeat credential (`verify_agent_recovery_secret`): until the agent holds its own key it sends the secret on every heartbeat, and a guess at it is a guess at one agent, not at every install. Each refused pending credential, on that agent's heartbeat from that address, counts against the registration budget (`reserve_pending_heartbeat_attempt`), except a token in the agent-key format, which cannot be a registration key and gets its marked 401 unchecked; past the budget the heartbeat answers 429 with `Retry-After`. Only an agent that never had a secret (registered by a build from before secrets) is checked against the registration key instead, under the same budget, with a warning to upgrade it; a heartbeat for an agent that has a secret on record and sends none, or for one whose secret a revoke voided (`Agent.recovery_secret_voided_at`), is refused as a wrong secret is (401 marked `recovery-secret`, budgeted). A key recovery is held to the same budget per client address, whatever agent id it names (`reserve_key_recovery_attempt`). Both budgets count only refusals in the window; a check in flight holds room in this process until it is settled (`CredentialAttempt.settle`), so concurrent guesses cannot outrun the budget and concurrent right checks past it are told to wait `BUSY_RETRY_AFTER_SECONDS`, never the window (the agent asks again at its next heartbeat). A refusal is counted when it is settled, even late, except a recovery refused for a known agent with no secret on record (revoked, or registered before secrets), which has nothing to guess, so revoking a fleet behind one address never locks its recoveries out; a check never settled stops holding room after `IN_FLIGHT_SECONDS` (five minutes), far past any check, each of whose statements the database cuts at its statement timeout. An unknown agent, a missing secret and a wrong one are refused identically (401); every recovery, granted or refused, is logged as a `SECURITY AUDIT` event without the secret. Agents registered before recovery secrets existed have no digest and cannot recover: they register again and are approved again.

## UI Surface

### Agent Management

**List view:** all agents with status (online/offline/approval status), tags, and per-agent check count.

**Edit agent:** `agent_name` (editable), hostname, tags (`key:value,key:value`), heartbeat interval, check sync interval (NULL = global default). A "Force Config Reload" action sets `checks_updated_at = now()` to trigger the agent to refetch.

### Check Management

**Assignment section of the check form:**

```
Assignment Mode: [ Manual | Replicate | Distribute ]

[IF Manual]      Agent: [select one agent -> Check.agent_id]
[IF Replicate /
    Distribute]  Agent Selector:
                   ( ) Specific agents -> {"agent_ids": [...]}  (agent_name values)
                   ( ) Tag-based       -> {"tags": [...], "match_mode": "all"|"any"}
```

**Check list view:** assignment-mode badge; for replicate/distribute show the matched-agent count and the selector; for manual show the assigned agent.

## Examples

### Example 1: External Monitoring (REPLICATE)

```python
london = Agent(agent_name="london-agent", tags="role:external-monitor,region:london")
tokyo  = Agent(agent_name="tokyo-agent",  tags="role:external-monitor,region:tokyo")
nyc    = Agent(agent_name="nyc-agent",    tags="role:external-monitor,region:us-east")

http_google = Check(
    display_name="http_google",
    target="https://google.com",
    assignment_mode="replicate",
    agent_selector={"tags": ["role:external-monitor"]},
)
# All 3 agents run http_google and report independently.
```

### Example 2: Home Network HA (DISTRIBUTE)

```python
home1 = Agent(agent_name="home-agent-1", tags="location:home")
home2 = Agent(agent_name="home-agent-2", tags="location:home")

for device in ["printer", "switch", "camera1", "camera2", ...]:  # 50 devices
    Check(
        display_name=f"ping_{device}",
        target=f"{device}.local",
        assignment_mode="distribute",
        agent_selector={"tags": ["location:home"]},
    )
# Each check hashes to one of the two agents (~25 each).
```

### Example 3: Puppet Monitoring at Scale (DISTRIBUTE)

```python
for i in range(1, 5):
    Agent(agent_name=f"puppet-monitor-{i}", tags="role:puppet-monitor,pool:puppet-pool")

for node in range(1, 1001):  # 1000 nodes
    Check(
        display_name=f"puppet_node_{node:04d}",
        target=f"puppet-node-{node}.internal",
        assignment_mode="distribute",
        agent_selector={"tags": ["role:puppet-monitor"]},
    )
# ~250 checks per agent; adding a 5th agent rehashes toward ~200 each.
```

## Questions & Decisions

### Q: What happens when an agent with DISTRIBUTE checks goes offline?

**A:** Once it has missed 3 heartbeats plus 60 s, or at once when it shuts down cleanly, its checks move to the other online agents of each check's pool, and each of those agents fetches its new list at its next heartbeat. They move back when it heartbeats again. See **Failover** under Use Case 2.

### Q: Can a check be both REPLICATE and DISTRIBUTE?

**A:** No. `assignment_mode` is a single value per check; the modes are mutually exclusive.

### Q: How do you view results for REPLICATE checks?

**A:** Each agent reports independently; results are grouped by check with a per-agent breakdown:

```
http_google (replicate)
  ✓ london-agent - 50ms - OK
  ✓ tokyo-agent  - 120ms - OK
  ✗ nyc-agent    - timeout - FAILED
```

### Q: What if no agents match the selector?

**A:** The check is assigned to no agent (`filter_agents_by_selector` returns empty, `get_assigned_agent_for_check` returns `None`). The check stays in the database and is picked up automatically once a matching agent appears.

## Known Limitations

- **Failover waits for the offline threshold.** A distributed check whose agent dies goes unrun for 3 heartbeat intervals plus 60 s before it moves.
