# LuxSwirl Architecture

**Technical architecture overview for contributors and advanced users.**


---

## Table of Contents

1. [System Overview](#system-overview)
2. [Component Architecture](#component-architecture)
3. [Data Flow](#data-flow)
4. [Database Schema](#database-schema)
5. [API Architecture](#api-architecture)
6. [Check Execution Lifecycle](#check-execution-lifecycle)
7. [Authentication & Security](#authentication--security)
8. [Technology Stack](#technology-stack)
9. [Scalability Design](#scalability-design)
10. [Design Decisions](#design-decisions)

---

## System Overview

### High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Internet / Network                       │
└────────────────────────┬────────────────────────────────────────┘
                         │
         ┌───────────────┴───────────────┐
         │                               │
    ┌────▼─────┐                   ┌────▼─────┐
    │  Agent   │                   │  Agent   │
    │  (N×)    │                   │  (N×)    │
    │          │                   │          │
    │  - Ping  │                   │  - HTTP  │
    │  - DNS   │                   │  - MySQL │
    │  - TCP   │                   │  - JSON  │
    └────┬─────┘                   └────┬─────┘
         │                               │
         │   HTTPS (Bearer Token)        │
         │                               │
         └───────────────┬───────────────┘
                         │
                    ┌────▼─────┐
                    │  Nginx   │  (Reverse Proxy)
                    │  or      │  SSL Termination
                    │ Traefik  │  Rate Limiting
                    └────┬─────┘
                         │ HTTP (localhost)
                    ┌────▼─────────────────────┐
                    │     Server            │
                    │  ┌─────────────────────┐ │
                    │  │   FastAPI           │ │
                    │  │   REST API (v1)     │ │
                    │  ├─────────────────────┤ │
                    │  │   Web UI            │ │
                    │  │   (Jinja2 + HTMX)   │ │
                    │  ├─────────────────────┤ │
                    │  │   Services Layer    │ │
                    │  │   (Business Logic)  │ │
                    │  └─────────────────────┘ │
                    └────┬─────────────────────┘
                         │ asyncpg (async)
                    ┌────▼─────────────────────┐
                    │   TimescaleDB            │
                    │   (PostgreSQL 14+)       │
                    │                          │
                    │   - Hypertables          │
                    │   - Compression (80-90%) │
                    │   - Retention policies   │
                    │   - Continuous aggregates│
                    └──────────────────────────┘
```

### Component Responsibilities

| Component | Responsibility | Technology |
|-----------|---------------|------------|
| **Agent** | Execute checks, report results | Python 3.14+, asyncio |
| **Server** | Receive results, serve UI/API | FastAPI, asyncpg |
| **TimescaleDB** | Time-series storage | PostgreSQL 14 + TimescaleDB 2.11 |
| **Reverse Proxy** | SSL termination, routing | nginx or Traefik |

---

## Component Architecture

### Agent Architecture

```
┌─────────────────────────────────────────────────┐
│              LuxSwirlAgent (agent/core.py)         │
├─────────────────────────────────────────────────┤
│                                                 │
│  ┌──────────────┐  ┌──────────────┐           │
│  │ Registration │  │   Heartbeat  │           │
│  │   Service    │  │    Service   │           │
│  └──────┬───────┘  └──────┬───────┘           │
│         │                  │                    │
│  ┌──────▼──────────────────▼───────┐           │
│  │    Check Scheduler               │           │
│  │    - Manages check intervals     │           │
│  │    - Concurrency control         │           │
│  │    - Timeout enforcement         │           │
│  └──────┬───────────────────────────┘           │
│         │                                        │
│  ┌──────▼───────────────────────────┐           │
│  │    Check Executor Pool            │           │
│  │    - Semaphore (max 200)          │           │
│  │    - Async execution              │           │
│  │    - Result capture               │           │
│  └──────┬───────────────────────────┘           │
│         │                                        │
│  ┌──────▼───────────────────────────┐           │
│  │    Check Registry                 │           │
│  │    - HTTP Check                   │           │
│  │    - JSON Check                   │           │
│  │    - Ping Check                   │           │
│  │    - TCP Check                    │           │
│  │    - DNS Check                    │           │
│  │    - MySQL Check                  │           │
│  │    - PostgreSQL Check             │           │
│  │    - Synthetic Check              │           │
│  └──────┬───────────────────────────┘           │
│         │                                        │
│  ┌──────▼───────────────────────────┐           │
│  │    Batching Reporter              │           │
│  │    - Batch size: 5000             │           │
│  │    - Interval: 10s                │           │
│  │    - Retry logic                  │           │
│  │    - Local storage fallback       │           │
│  └──────┬───────────────────────────┘           │
│         │                                        │
│  ┌──────▼───────────────────────────┐           │
│  │    Credentials Manager            │           │
│  │    - Fernet encryption            │           │
│  │    - Auto-registration            │           │
│  └───────────────────────────────────┘           │
│                                                 │
└─────────────────────────────────────────────────┘
```

**Key features**:
- **Async execution**: All I/O operations are async (httpx, asyncpg, asyncio)
- **Concurrency control**: Semaphore limits concurrent checks (default 200)
- **Batching**: Results batched before sending (reduces HTTP overhead)
- **Retry logic**: Exponential backoff on server failure
- **Local storage**: Failed reports stored locally, retried later
- **Self-monitoring**: Agent reports own health metrics

**Agent entry point**: `apps/agent/app/agent_main.py` **Core logic**: `apps/agent/app/agent/core.py` (LuxSwirlAgent class)

---

### Server Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Server (FastAPI)                      │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌────────────────────────────────────────────────────┐    │
│  │              API Layer (/api/v1)                   │    │
│  │                                                    │    │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐       │    │
│  │  │  Agents  │  │  Checks  │  │ Results  │       │    │
│  │  │  Router  │  │  Router  │  │  Router  │  ...  │    │
│  │  └────┬─────┘  └────┬─────┘  └────┬─────┘       │    │
│  └───────┼─────────────┼─────────────┼──────────────┘    │
│          │             │             │                    │
│  ┌───────▼─────────────▼─────────────▼──────────────┐    │
│  │           Services Layer                          │    │
│  │                                                   │    │
│  │  ┌──────────┐  ┌──────────┐  ┌───────────┐      │    │
│  │  │  Agent   │  │  Check   │  │  Result   │      │    │
│  │  │ Service  │  │ Service  │  │  Service  │ ...  │    │
│  │  └────┬─────┘  └────┬─────┘  └────┬──────┘      │    │
│  └───────┼─────────────┼─────────────┼──────────────┘    │
│          │             │             │                    │
│  ┌───────▼─────────────▼─────────────▼──────────────┐    │
│  │           Database Layer (SQLAlchemy)             │    │
│  │                                                   │    │
│  │  ┌──────────┐  ┌──────────┐  ┌───────────┐      │    │
│  │  │  Agent   │  │  Check   │  │   Check   │      │    │
│  │  │  Model   │  │  Model   │  │  Result   │ ...  │    │
│  │  └────┬─────┘  └────┬─────┘  └────┬──────┘      │    │
│  └───────┼─────────────┼─────────────┼──────────────┘    │
│          │             │             │                    │
│  ┌───────▼─────────────▼─────────────▼──────────────┐    │
│  │           asyncpg Connection Pool                 │    │
│  └───────┬───────────────────────────────────────────┘    │
│          │                                                │
│  ┌───────▼───────────────────────────────────────────┐    │
│  │              Web UI Layer                         │    │
│  │                                                   │    │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐       │    │
│  │  │ Status   │  │  Checks  │  │  Agents  │       │    │
│  │  │ Router   │  │  Router  │  │  Router  │  ...  │    │
│  │  └────┬─────┘  └────┬─────┘  └────┬─────┘       │    │
│  └───────┼─────────────┼─────────────┼──────────────┘    │
│          │             │             │                    │
│  ┌───────▼─────────────▼─────────────▼──────────────┐    │
│  │           Web Services Layer                      │    │
│  │   (Aggregates data for UI consumption)           │    │
│  └───────┬───────────────────────────────────────────┘    │
│          │                                                │
│  ┌───────▼───────────────────────────────────────────┐    │
│  │           Templates (Jinja2)                      │    │
│  │   - pages/                                        │    │
│  │   - partials/ (HTMX)                              │    │
│  │   - macros/                                       │    │
│  └───────────────────────────────────────────────────┘    │
│                                                           │
└───────────────────────────────────────────────────────────┘
```

**Layered architecture** (enforced by import-linter + `tests/test_architecture.py`):
1. **Routers** (API/Web): HTTP plumbing only — parsing, validation; zero business logic, zero raw SQL
2. **View services** (`services/views/`): assemble template context for web pages
3. **Core services** (`services/core/`): business logic and transactions; never touch raw SQL
4. **CRUD** (`crud/`): the only layer that runs SQL (`select()`, `update()`, `text()`, …)
5. **Models**: SQLAlchemy ORM models (database schema)
6. **Schemas**: Pydantic validation models (API contracts)

Chain — JSON API: router → core service → crud → models. Web UI: router → view service → core service → crud → models.

**Design principle**: **ZERO business logic in routers, and raw SQL only in CRUD.**

**Server entry point**: `apps/backend/app/main.py`

---

## Data Flow

### Check Result Submission Flow

```
┌─────────┐                                    ┌──────────┐
│  Agent  │                                    │Server │
└────┬────┘                                    └────┬─────┘
     │                                              │
     │  1. Execute checks                          │
     │     (async, concurrent)                     │
     ├──────────────────────┐                      │
     │  Check 1: SUCCESS    │                      │
     │  Check 2: TIMEOUT    │                      │
     │  Check 3: SUCCESS    │                      │
     └──────────────────────┘                      │
     │                                              │
     │  2. Batch results                           │
     │     (5000 max, 10s interval)                 │
     ├──────────────────────┐                      │
     │  [{result1}, ...]    │                      │
     └──────────────────────┘                      │
     │                                              │
     │  3. POST /api/v1/reports                    │
     │     Authorization: Bearer <token>           │
     ├────────────────────────────────────────────>│
     │                                              │
     │                             4. Validate auth│
     │                                (verify_agent_token)
     │                                              ├────┐
     │                                              │    │
     │                                              │<───┘
     │                                              │
     │                             5. Process batch│
     │                               (CheckResultService)
     │                                              ├────┐
     │                                              │    │
     │  For each result:                           │    │
     │    - Upsert check                           │    │
     │    - Insert result                          │    │
     │    - Update agent last_seen                 │    │
     │                                              │<───┘
     │                                              │
     │  6. Response: {"received": 500}             │
     │<────────────────────────────────────────────┤
     │                                              │
     │  7. Clear local batch                       │
     ├──────────────────────┐                      │
     │  Ready for next batch │                     │
     └──────────────────────┘                      │
     │                                              │
```

**Error handling**:
- **Network failure**: Store results locally (`reports/` directory)
- **429 Rate Limit**: Exponential backoff (10s → 20s → 40s → ...)
- **500 Server Error**: Retry with backoff
- **401 Unauthorized**: Log error, stop agent (credential issue)

---

### Dashboard Load Flow

```
┌─────────┐                                    ┌──────────┐
│ Browser │                                    │Server │
└────┬────┘                                    └────┬─────┘
     │                                              │
     │  1. GET /                                    │
     ├────────────────────────────────────────────>│
     │                                              │
     │                           2. Authenticate    │
     │                              (session cookie)│
     │                                              ├────┐
     │                                              │    │
     │                                              │<───┘
     │                                              │
     │                         3. Query dashboard   │
     │                           (StatusViewService)    │
     │                                              ├────┐
     │  SQL:                                        │    │
     │   - Get all agents (LIMIT 100)              │    │
     │   - Get all checks with latest result       │    │
     │     (window function for latest)            │    │
     │   - Calculate summary stats                 │    │
     │                                              │<───┘
     │                                              │
     │  4. Render template (Jinja2)                │
     │     HTML + HTMX                              │
     │<────────────────────────────────────────────┤
     │                                              │
     │  5. Browser renders page                    │
     ├──────────────────────┐                      │
     │  Dashboard displayed │                      │
     └──────────────────────┘                      │
     │                                              │
     │  (Every 10 seconds)                         │
     │  6. HTMX auto-refresh                       │
     │     hx-get="/partials/status_table"         │
     │     hx-trigger="every 10s"                  │
     ├────────────────────────────────────────────>│
     │                                              │
     │                         7. Query latest data│
     │                            (StatusViewService)   │
     │                                              ├────┐
     │                                              │    │
     │                                              │<───┘
     │                                              │
     │  8. Partial HTML response                   │
     │<────────────────────────────────────────────┤
     │                                              │
     │  9. HTMX swaps content                      │
     │     (no full page reload)                   │
     ├──────────────────────┐                      │
     │  Table updated live  │                      │
     └──────────────────────┘                      │
     │                                              │
```

**Performance optimizations**:
- **Batch queries**: Single query for all checks (not N+1)
- **Window functions**: Latest result per check (efficient)
- **Partial updates**: HTMX refreshes only table (not whole page)
- **Pagination**: Configurable page size (10, 25, 50, 100, 200)

---

## Database Schema

### Entity-Relationship Diagram

```
┌────────────────┐
│     Agent      │
├────────────────┤
│ id (UUID)      │◄──┐
│ agent_name     │   │
│ approval_status│   │
│ heartbeat_int  │   │
│ last_seen      │   │
│ created_at     │   │
│ updated_at     │   │
└────────────────┘   │
                     │ 1:N
                     │
                ┌────┴────────┐
                │    Check    │
                ├─────────────┤
                │ id (UUID)   │◄──┐
                │ agent_id    │   │
                │ display_name│   │
                │ check_type  │   │
                │ target      │   │
                │ interval    │   │
                │ timeout     │   │
                │ config (JSONB)  │
                │ enabled     │   │
                │ created_at  │   │
                │ updated_at  │   │
                └─────────────┘   │ 1:N
                                  │
                         ┌────────┴────────┐
                         │  CheckResult    │ (Hypertable)
                         ├─────────────────┤
                         │ id (UUID)       │
                         │ timestamp       │ ◄── Partition key
                         │ check_id        │
                         │ success (bool)  │
                         │ latency_ms      │
                         │ error (text)    │
                         │ metrics (JSONB) │
                         └─────────────────┘

┌────────────────┐
│      User      │
├────────────────┤
│ id (UUID)      │
│ username       │
│ email          │
│ password_hash  │
│ role           │
│ is_active      │
│ created_at     │
│ updated_at     │
└────────────────┘

┌────────────────┐
│  StatusPage    │
├────────────────┤
│ id (UUID)      │
│ name           │
│ slug           │
│ description    │
│ is_public      │
│ items (JSONB)  │
│ created_at     │
│ updated_at     │
└────────────────┘
```

**Key design decisions**:
- **UUID primary keys**: Better for distributed systems. (The schema does not enforce a specific UUID version — agent-generated result IDs are stored as free-form strings, so do not assume strict UUIDv7 time-ordering.)
- **JSONB config**: Flexible check configuration without schema changes
- **Hypertable partitioning**: TimescaleDB partitions `check_results` by timestamp (1-day chunks)
- **Composite FK**: `(check_result_id, check_result_timestamp)` for hypertable compatibility

---

### TimescaleDB Optimizations

**Hypertable configuration**:
```sql
-- Create hypertable (partitioned by timestamp)
SELECT create_hypertable(
  'check_results',
  'timestamp',
  chunk_time_interval => INTERVAL '1 day'
);
```

**Compression policy** (80-90% reduction):
```sql
-- Compress chunks older than 7 days
SELECT add_compression_policy(
  'check_results',
  INTERVAL '7 days'
);
```

**Retention policy** (auto-delete):
```sql
-- Delete chunks older than 90 days
SELECT add_retention_policy(
  'check_results',
  INTERVAL '90 days'
);
```

**Continuous aggregates** (5-minute rollups):
```sql
CREATE MATERIALIZED VIEW check_results_5min
WITH (timescaledb.continuous) AS
SELECT
  time_bucket('5 minutes', timestamp) AS bucket,
  check_id,
  AVG(latency_ms) AS avg_latency,
  COUNT(*) FILTER (WHERE success) AS success_count,
  COUNT(*) AS total_count
FROM check_results
GROUP BY bucket, check_id;
```

**Benefits**:
- **Fast queries**: 5-minute aggregates reduce rows scanned
- **Auto-refresh**: Continuous aggregates update automatically
- **Dashboard performance**: 30-50× faster than raw table queries

---

## API Architecture

### REST API Design Principles

**Versioning**: All endpoints under `/api/v1` **Authentication**: Bearer token (HTTP header: `Authorization: Bearer <token>`) **Response format**: JSON (consistent envelope structure) **Pagination**: Offset-based (`offset`, `limit` query params) **Filtering**: Query params (`agent`, `type`, `status`) **Sorting**: Query params (`sort_by`, `order`)

### API Endpoint Structure

```
/api/v1
├── Agent-facing (Bearer token)
│   ├── GET    /checks?agent_id={id}                   Fetch this agent's checks
│   ├── POST   /agents/heartbeat                             Agent heartbeat
│   └── POST   /reports                                Submit batch of results
│
├── /agents
│   ├── GET    /                    List agents
│   ├── POST   /                    Create agent
│   ├── GET    /{agent_id}          Get agent
│   ├── PATCH  /{agent_id}          Update agent
│   ├── DELETE /{agent_id}          Delete agent
│   ├── GET    /{agent_id}/stats    Get stats
│   └── POST   /{agent_id}/approve  Approve agent
│
├── Check management (UI/admin)
│   ├── GET    /agents/{agent_id}/checks              List checks
│   ├── POST   /agents/{agent_id}/checks              Create check
│   ├── GET    /agents/{agent_id}/checks/{check_id}  Get check
│   ├── PATCH  /agents/{agent_id}/checks/{check_id}  Update check
│   ├── DELETE /agents/{agent_id}/checks/{check_id}  Delete check
│   └── POST   /agents/{agent_id}/checks/{check_id}/clone  Clone check
│
├── Results
│   ├── GET    /agents/{agent_name}/results            Get latest
│   ├── GET    /agents/{agent_name}/checks/{check_id}/history   Get history
│   └── GET    /agents/{agent_name}/checks/{check_id}/summary   Get summary
│
└── /metrics + /health (root level, not under /api/v1)
    ├── GET    /metrics               Prometheus format (auth configurable)
    └── GET    /health                Health check
```

> **Status pages are web-only.** There is no `/api/v1/status-pages` endpoint group; status pages are managed entirely through the Web UI (`status_pages_router` in the web layer), not the JSON API.

> **Note on the check fetch path**: the agent fetches its assigned checks via `GET /api/v1/checks?agent_id={id}` (agent UUID passed as a *query parameter*). The `/agents/{agent_id}/checks` paths are the separate **management** endpoints used by the UI/admin, not by the agent runtime.

**HTTP Status Codes**:
- `200 OK`: Successful GET/PATCH
- `201 Created`: Successful POST
- `204 No Content`: Successful DELETE
- `400 Bad Request`: Invalid input
- `401 Unauthorized`: Missing/invalid auth
- `403 Forbidden`: Insufficient permissions
- `404 Not Found`: Resource not found
- `409 Conflict`: Resource already exists
- `422 Unprocessable Entity`: Validation error
- `500 Internal Server Error`: Server error

---

## Check Execution Lifecycle

### Check Lifecycle State Machine

```
┌─────────────┐
│   CREATED   │  (Configured via UI/API)
└──────┬──────┘
       │
       │ Agent fetches config
       │
┌──────▼──────┐
│  SCHEDULED  │  (Waiting for interval)
└──────┬──────┘
       │
       │ Interval elapsed
       │
┌──────▼──────┐
│  EXECUTING  │  (Running check)
└──────┬──────┘
       │
       │ Check completes
       │
    ┌──┴──┐
    │     │
┌───▼──┐ ┌▼────┐
│SUCCESS│ │FAIL │
└───┬──┘ └┬────┘
    │     │
    │     │ Result captured
    │     │
    └──┬──┘
       │
┌──────▼──────┐
│  REPORTING  │  (Sending to server)
└──────┬──────┘
       │
       │ Result received
       │
┌──────▼──────┐
│   STORED    │  (Saved in database)
└──────┬──────┘
       │
       │ Alert evaluation
       │
    ┌──┴──┐
    │     │
┌───▼──┐ ┌▼────────┐
│ OK   │ │ ALERTING│
└───┬──┘ └┬────────┘
    │     │
    │     │ Notification sent
    │     │
    └──┬──┘
       │
       │ Wait for next interval
       │
┌──────▼──────┐
│  SCHEDULED  │  (Loop continues)
└─────────────┘
```

### Check Execution Details

**1. Configuration Fetch** (Agent startup + periodic reload):
```python
# Agent fetches its check configs from the server (agent_id as query param)
GET /api/v1/checks?agent_id={agent_id}
Authorization: Bearer {agent_token}
# Returns: {"checks": [ ...check configurations... ]}
```

**2. Scheduling**:
```python
# Agent schedules each check based on interval
# Example: check with interval=60 → runs every 60 seconds
# Uses asyncio.create_task() for concurrent execution
```

**3. Execution** (Async, concurrent):
```python
async def execute_check(check):
    # Acquire semaphore (limit concurrent checks to 200)
    async with self.semaphore:
        try:
            # Start timer
            start = time.time()

            # Execute check (type-specific logic)
            result = await check.run()

            # Calculate latency
            latency = (time.time() - start) * 1000

            # Return result
            return {
                "check_id": check.id,
                "timestamp": time.time(),
                "success": result.success,
                "latency_ms": latency,
                "error": result.error
            }
        except asyncio.TimeoutError:
            return {
                "check_id": check.id,
                "success": False,
                "error": f"Timeout after {check.timeout}s"
            }
```

**4. Batching**:
```python
# Results accumulated in memory
# Batch sent when:
#   - Batch size reaches 5000, OR
#   - 10 seconds elapsed since last send
```

**5. Reporting**:
```python
POST /api/v1/reports
Authorization: Bearer {agent_token}
Content-Type: application/json

# Shape matches AgentReportRequest: agent_id, agent_run_id, checks (list).
# The per-result timestamp lives on each item in `checks`. The top-level
# `timestamp` field exists but is optional and not required.
{
  "agent_id": "uuid",
  "agent_run_id": "abc123-run-id",
  "checks": [
    {"timestamp": "2024-01-01T12:00:00Z", "success": true,  "display_name": "...", "check_type": "...", "target": "...", ...},
    {"timestamp": "2024-01-01T12:00:01Z", "success": false, "display_name": "...", "check_type": "...", "target": "...", ...}
  ]
}
```

**6. Storage** (Server):
```python
# For each result in batch:
#   1. Upsert check (if config changed)
#   2. Insert check_result row
#   3. Update agent.last_seen

# All in single transaction for consistency
```

---

## Authentication & Security

### Authentication Flow

**Web UI** (Session-based):
```
1. User → POST /login (username + password)
2. Server → Verify password (bcrypt)
3. Server → Create session (secure cookie)
4. Browser → Stores session cookie (httpOnly, SameSite=Lax)
5. Subsequent requests → Cookie sent automatically
6. Server → Validates session from cookie
```

**Agent API** (Bearer token):
```
1. Agent → POST /api/v1/agents/register (with LUXSWIRL_AUTH_KEY)
2. Server → Generate unique API key for agent
3. Agent → Stores API key (encrypted at rest)
4. Subsequent requests → Authorization: Bearer {api_key}
5. Server → Validates token (global token OR agent-specific key)
6. Server → Checks approval_status (must be "active")
```

### Security Layers

**Layer 1: Network** (Reverse proxy)
- SSL/TLS termination
- Rate limiting (nginx/Traefik)
- DDoS protection (Cloudflare)

**Layer 2: Application** (FastAPI middleware)
- CORS headers (`SERVER__CORS_ORIGINS`)
- CSRF protection (SameSite cookies)
- Security headers (X-Frame-Options, CSP)

**Layer 3: Authentication**
- Session validation (Web UI)
- Bearer token validation (API)
- Role-based access control (Admin/Editor/Viewer)

**Layer 4: Authorization**
- User role check (admin vs user)
- Agent approval status (active only)
- Resource ownership (users can only see their data)

**Layer 5: Input Validation**
- Pydantic schemas (all API inputs)
- SQL injection prevention (SQLAlchemy ORM)
- Command injection prevention (parameterized subprocess)
- XSS prevention (Jinja2 auto-escaping)

**Layer 6: Data Protection**
- Password hashing (bcrypt, 12 rounds)
- Credential encryption (Fernet AES-128)
- Log scrubbing (automatic credential masking)

---

## Technology Stack

### Backend

| Component | Technology | Version | Purpose |
|-----------|-----------|---------|---------|
| **Language** | Python | 3.14+ | Async/await, type hints |
| **Web Framework** | FastAPI | 0.104+ | REST API, async |
| **ORM** | SQLAlchemy | 2.0+ | Database abstraction |
| **Database Driver** | asyncpg | 0.29+ | Async PostgreSQL |
| **Database** | TimescaleDB | 2.11+ | Time-series storage |
| **Validation** | Pydantic | 2.5+ | Schema validation |
| **HTTP Client** | httpx | 0.25+ | Async HTTP requests |
| **Template Engine** | Jinja2 | 3.1+ | HTML rendering |
| **Session Management** | itsdangerous | 2.1+ | Secure cookies |
| **Password Hashing** | bcrypt | 4.1+ | Secure hashing |
| **Encryption** | cryptography | 41.0+ | Fernet encryption |

### Frontend

| Component | Technology | Version | Purpose |
|-----------|-----------|---------|---------|
| **HTML** | HTML5 | - | Markup |
| **CSS** | Tailwind CSS | 3.4+ | Styling |
| **JavaScript** | Vanilla JS + HTMX | 1.9+ | Interactivity |
| **Charts** | Chart.js | 4.4+ | Visualizations |

### Infrastructure

| Component | Technology | Purpose |
|-----------|-----------|---------|
| **Containerization** | Docker | 20.10+ | Isolation |
| **Orchestration** | Docker Compose | 2.0+ | Multi-container |
| **Reverse Proxy** | nginx / Traefik | - | SSL, routing |
| **Database** | PostgreSQL | 14+ | Relational data |
| **Time-Series Extension** | TimescaleDB | 2.11+ | Time-series optimization |

### Development Tools

| Tool | Purpose |
|------|---------|
| **pytest** | Unit/integration testing |
| **black** | Code formatting |
| **ruff** | Linting |
| **mypy** | Type checking |
| **pre-commit** | Git hooks |

---

## Scalability Design

### Horizontal Scaling

**Agents** (✅ Supported):
```
Deploy N agents → All report to same server
No limit on agent count (tested: 1,000+)
Each agent operates independently
```

**Servers** (⚠️ not currently supported):
```
Deploy N servers → Load balancer in front
Session store shared (Redis or DB)
Database connection pool per server
```

**Database** (⚠️ not currently supported):
```
Primary + read replicas
Write to primary, read from replicas
Connection pooling per server
```

### Vertical Scaling

A single server handles many agents on modest hardware (e.g. 4 CPU, 8 GB RAM is a reasonable starting point), but absolute limits depend heavily on check frequency, result-payload size, retention period, and Postgres tuning. LuxSwirl has not yet been published with formal benchmark numbers — operators should size by running a representative subset and watching CPU, write throughput, and disk usage.

**Bottlenecks** (in roughly the order you'll hit them):
1. **Database writes** — TimescaleDB compression and continuous aggregates help significantly
2. **Dashboard queries** — continuous aggregates back the most-viewed pages
3. **Result ingestion** — agent-side batching keeps HTTP overhead low

### Performance Characteristics

The agent-side `max_concurrent_checks` (default 200) and the server-side `report_batch_size` (default 5000) are the main throughput knobs. Per-request latency on the server is dominated by Postgres write throughput; dashboard latency is dominated by whether the query hits a continuous aggregate vs. the raw hypertable.

---

## Design Decisions

### Why FastAPI?

**Pros**:
- ✅ Native async/await support
- ✅ Automatic OpenAPI docs (Swagger)
- ✅ Pydantic validation built-in
- ✅ Type hints throughout
- ✅ High performance (Starlette + uvloop)

**Cons**:
- ⚠️ Newer framework (less mature than Django)
- ⚠️ Fewer batteries included (need to add auth, sessions)

### Why TimescaleDB?

**Pros**:
- ✅ PostgreSQL-based (familiar, mature)
- ✅ Automatic partitioning (1-day chunks)
- ✅ Compression (80-90% reduction)
- ✅ Continuous aggregates (fast queries)
- ✅ Retention policies (auto-delete)
- ✅ SQL compatibility (no new query language)

**Cons**:
- ⚠️ Extension (not core PostgreSQL)
- ⚠️ More complex than SQLite (overkill for <100 checks)

**Alternatives considered**:
- InfluxDB: Custom query language (Flux), less mature
- Prometheus: Pull-based (doesn't fit agent-push model)
- SQLite: Doesn't scale beyond 10 GB

### Why HTMX?

**Pros**:
- ✅ Minimal JavaScript (server-rendered HTML)
- ✅ Progressive enhancement (works without JS)
- ✅ Simple mental model (HTML attributes)
- ✅ Fast development (no complex frontend build)

**Cons**:
- ⚠️ Limited interactivity (not SPA)
- ⚠️ Polling-based (not WebSocket real-time)

**Alternatives considered**:
- React: Overkill for mostly server-rendered app
- Vue: More complexity than needed
- Alpine.js: Considered, but HTMX simpler

### Why SQLAlchemy 2.0?

**Pros**:
- ✅ Async support (asyncpg)
- ✅ Type hints (Mapped, mapped_column)
- ✅ Mature, battle-tested
- ✅ Migration support (Alembic)

**Cons**:
- ⚠️ Learning curve (ORM complexity)
- ⚠️ Performance overhead vs raw SQL

### Why Agents Push (Not Pull)?

**Design**: Agents push results to server (not server pulls from agents)

**Rationale**:
- ✅ Agents can be behind NAT/firewall (no inbound required)
- ✅ Agents control when to report (batching, retry logic)
- ✅ Server doesn't need to know agent addresses
- ✅ Scales better (server doesn't poll thousands of agents)

**Trade-off**:
- ⚠️ Server must be publicly accessible (or VPN)
- ⚠️ Agents need to know server URL (configured)

---

## Contributing to Architecture

**Want to propose architectural changes?**

1. Open GitHub Discussion (for major changes)
2. Provide rationale (problem + proposed solution)
3. Consider backward compatibility
4. Performance implications
5. Security implications

**Architecture documentation**: Keep this document updated with major changes.

---

