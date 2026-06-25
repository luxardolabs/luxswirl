# Agent Architecture & Check Assignment

## Overview

LuxSwirl supports three check assignment modes to handle different monitoring scenarios. The mode is stored per-check on the `Check` model via the `assignment_mode` column and selected agents are matched via the `agent_selector` JSON column. All three modes are implemented and shipped.

1. **MANUAL** (default) - Check runs on one specific agent (`Check.agent_id` FK).
2. **REPLICATE** - Same check runs from every agent matching the selector (multi-region monitoring, redundancy).
3. **DISTRIBUTE** - Checks are split across the agents matching the selector (load sharing, HA).

The assignment logic lives in `apps/backend/app/services/core/agent_assignment_core_service.py` (`AgentAssignmentService`). Agents fetch their work via `GET /api/v1/checks?agent_id={agent_id}` (`apps/backend/app/api/v1/routers/check_router.py`), which calls `AgentAssignmentService.get_checks_for_agent(db, agent)`. (The `/api/v1/agents/{agent_id}/checks` paths are the separate check-management CRUD endpoints, not the agent sync endpoint.)

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

If home-agent-1 dies:
- It stops appearing in the matching pool, so its checks rehash onto
  home-agent-2 the next time agents fetch their config.
- No gaps in monitoring once the offline agent is excluded from the pool.
```

**Note on failover:** `get_matching_agents()` currently matches by selector against *all* agents (`AgentCRUD.list_all`); it does not filter on online status. Rebalancing therefore depends on offline agents being removed from the pool by whatever populates the agent list, not on liveness within this function.

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

For **DISTRIBUTE** mode, `AgentAssignmentService.get_assigned_agent_for_check` picks the responsible agent using rendezvous (highest-random-weight / HRW) hashing:

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

**Why HRW + sha256 over the UUIDs (LUXSWIRL-183):**

- **Process-independent.** `sha256` is not seeded, so the mapping is identical across server restarts, multiple uvicorn workers, and replicas. (The previous implementation used the builtin `hash()` over `check.display_name`, which is `PYTHONHASHSEED`-salted per process — stable only within a single process, and prone to overlapping/conflicting assignments across workers.)
- **Keyed on the immutable `check.id`, not `display_name`.** `display_name` is editable and not unique, so hashing it meant a rename silently reassigned the check and same-named checks always co-located. The UUID is the stable identity.
- **Minimal reshuffle on pool change.** With HRW, when an agent joins or leaves the matching pool, only the checks owned by that agent move; everyone else stays put. Plain `hash % n` reshuffles *every* check whenever the pool size changes.

> **Failover caveat.** This function only assigns among the agents it is *given*. `get_matching_agents()` does not yet filter on liveness, so a dead-but-still-listed agent stays in the pool and its checks are not reassigned to survivors. Health-based failover is tracked separately in **LUXSWIRL-184**.

### Agent Check Fetch Logic (shipped)

`AgentAssignmentService.get_checks_for_agent(db, agent)` builds the per-agent list. Simplified:

```python
async def get_checks_for_agent(db, agent):
    checks_to_run = []
    all_checks = await CheckCRUD.list_all(db)

    for check in all_checks:
        if check.assignment_mode == "manual":
            if check.agent_id == agent.id:          # FK match
                checks_to_run.append(check)

        elif check.assignment_mode == "replicate":
            if agent_matches_selector(agent, check.agent_selector):
                checks_to_run.append(check)

        elif check.assignment_mode == "distribute":
            if agent_matches_selector(agent, check.agent_selector):
                matching_agents = await get_matching_agents(db, check.agent_selector)
                assigned = get_assigned_agent_for_check(check, matching_agents)
                if assigned and assigned.id == agent.id:
                    checks_to_run.append(check)

    return checks_to_run
```

Note that all comparisons use UUID primary keys (`check.agent_id == agent.id`, `assigned.id == agent.id`), not name strings.

### Selector Matching (shipped)

`AgentAssignmentService.agent_matches_selector`:

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

**Tag format:** `key:value` pairs, comma-separated

```
Examples:
- "role:external-monitor,region:london"
- "role:puppet-monitor,pool:puppet-pool"
- "location:home,role:internal-monitor"
```

## Data Model

### Agent Model (shipped)

`Agent` has a UUID primary key `id` and a friendly, editable `agent_name` (nullable until an admin assigns it during approval). Relevant assignment and auth fields:

```python
class Agent(UUIDBaseModel):
    agent_name: Mapped[str | None]    # unique friendly name (set on approval)
    tags: Mapped[str | None]          # comma-separated key:value tags
    approval_status: Mapped[str]      # pending | active | paused | disabled | rejected

    # Per-agent API key (shipped)
    api_key_hash: Mapped[str | None]        # bcrypt hash of the agent's key
    api_key_created_at: Mapped[datetime | None]
    api_key_last_used: Mapped[datetime | None]
```

## Authentication

Two distinct mechanisms exist and are both shipped:

1. **Server/admin API tokens** — used for management endpoints (e.g. creating registration keys). Validated by `verify_api_token` (`app/core/security.py`) against `SecuritySettings.auth_tokens` (`app/core/config.py`), populated from the `SECURITY__AUTH_TOKENS` env var. When unset, a token is resolved/generated at startup (`app/core/secrets.py`) — do not hard-code tokens in docs or config.

   ```
   # Example only — provide your own value, never commit a real token.
   SECURITY__AUTH_TOKENS=["<your-admin-api-token>"]
   ```

2. **Per-agent API keys** — each agent has its own key, stored as a bcrypt hash in `Agent.api_key_hash` (with `api_key_created_at` / `api_key_last_used`). Agent-facing endpoints (e.g. `GET /api/v1/checks?agent_id={agent_id}`) authenticate the agent via `verify_agent_token`, which also enforces the agent's `approval_status`. This enables per-agent revocation and isolation.

Agents bootstrap onto the platform using **shared registration keys** (`app/api/v1/routers/registration_key_router.py`, `RegistrationKeyService`): the plaintext key is shown once at creation and stored only as a hash. Used for initial registration and recovery.

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
**A:** Its checks rehash onto the remaining agents the next time agents refetch, *provided the offline agent is removed from the matching pool*. See the failover note under Use Case 2 — `get_matching_agents()` itself does not filter on liveness.

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
**A:** The check is assigned to no agent (`get_matching_agents` returns empty, `get_assigned_agent_for_check` returns `None`). The check stays in the database and is picked up automatically once a matching agent appears.

## Known Limitations

- **DISTRIBUTE hashing is not stable across processes.** It uses builtin `hash()`, which is per-process randomized for strings. See the hash stability note above. Switch to a deterministic digest for stable cross-process / multi-worker assignment.
- **Pool membership ignores liveness.** `get_matching_agents()` matches against all agents regardless of online status; failover relies on offline agents being excluded upstream.
