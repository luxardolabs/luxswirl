# Public Status Pages

A status page is a custom, branded dashboard showing the health of selected services to customers, stakeholders, or internal teams. Each page has a unique URL slug and can be **public** (no login) or **private** (requires authentication). Pages show an overall system-health indicator, per-check status with latency and 24-hour uptime, optional grouping, and auto-refresh every 20 seconds.

**Access:** **Status Pages** in the sidebar, or `/status-pages`. Public view: `/status/{slug}`.

---

## Creating a status page

**Create New** opens a form:

| Field | Required | Notes |
|-------|----------|-------|
| **Name** | Yes | 3–100 chars; shown at the top of the public page |
| **Slug** | Yes | 3–50 chars, `[a-z0-9-]` only, unique; auto-generated as the kebab-case name. Public URL is `/status/{slug}` |
| **Description** | No | ≤500 chars; subtitle on the public page |
| **Is Public** | No (default false) | Public = viewable by anyone with the URL; Private = requires a LuxSwirl login |

> **Security:** public pages expose check names, targets, and current status. Don't put internal hostnames or infrastructure details on a public page — see [Data exposure](#data-exposure).

After creating, you land on the **Manage** screen to add checks and groups.

---

## Managing layout

The **Manage** screen is a two-panel drag-and-drop editor: the left panel is your live layout (drag to reorder); the right panel lists all checks with **agent / type / status / tag** filters. Click a check to append it, or drag it into place. **Add Container Group** and **Add Dynamic Group** create groupings. Click **Save Changes** to persist order, group membership, and group settings (the public page updates immediately). Removing a check or deleting a group never deletes the underlying checks.

### Container groups (manual)

A static, hand-picked list of checks. Create one with a **Group Name**, an optional **collapsed by default**, and **Sort by** (Manual / Name / Status) + direction. Drag checks into it from the Available panel or other groups.

### Dynamic groups (filter-based)

Auto-populate with checks matching filter criteria and update themselves as you add/remove/retag checks. Same name/collapse/sort options, plus **filter criteria**: **Agent ID**, **Tags** (comma-separated), **Type**.

```
"All Production Checks"   → Tags: production            Sort: Status (desc)   # failing first
"Customer A - Services"   → Tags: customer-a            Sort: Name (asc)
"Database Health"         → Type: mysql,postgres  Tags: production  Sort: Status
"Agent-Specific Checks"   → Agent ID: abc-123-def       Sort: Name
```

| | Container group | Dynamic group |
|---|----------------|---------------|
| Check selection | Manual (drag) | Automatic (filter) |
| Updates | Static list | Auto-updates as checks change |
| Best for | A curated set of services | Broad categorization by tag/type/agent |

---

## Public view

At `/status/{slug}` the page shows an **overall status hero**, then ungrouped checks and groups in your saved order, refreshing every 20 seconds via HTMX (no full reload).

**Overall status** is computed from current results:

| Indicator | Condition |
|-----------|-----------|
| 🟢 **All Systems Operational** | All checks passing |
| 🟡 **Degraded Performance** | Some failing, but ≥50% up |
| 🔴 **Service Outage** | <50% up |
| ⚪ **Status Unknown** | No recent results |

It also shows overall 24-hour uptime. **Each check** displays its name, target, current status (Up/Down/Unknown), most-recent latency (color-coded: green <100ms, yellow 100–500ms, red >500ms), and 24h uptime % (green ≥99%, yellow 95–99%, red <95%). **Groups** show a header with check count and a collapse/expand toggle; collapsed groups show only the group's overall status.

**Public vs private:** a public page (`is_public=true`) is viewable at `/status/{slug}` with no auth; a private page redirects to login. Slugs aren't secret tokens — for sensitive internal pages, either use an unguessable slug or keep the page private.

---

## Editing and deleting

**Edit** changes the metadata — name, slug, description, and public/private. (Use **Manage** for the layout.)

> ⚠️ Changing the **slug** breaks existing shared links — the old `/status/{slug}` will 404.

**Delete** removes the status page and its group configurations and check associations. It does **not** delete the checks, their results, or agents. The public URL then 404s and the slug becomes reusable.

---

## Data exposure

Review names and targets before making a page public.

| Visible on public pages | Never exposed |
|-------------------------|---------------|
| Check names | Agent details (name, ID, location) |
| Check targets | Check configuration (interval, timeout, headers) |
| Current status (up/down) | Credentials |
| Latency | Historical event logs |
| 24h uptime % | Other status pages, user accounts |

Use generic, customer-friendly names ("Database Server", not `db-prod-01.internal.company.com`), avoid internal IPs and admin interfaces, and consider separate public and internal pages with different detail levels.

---

## Common workflows

### Customer-facing page

Show customers the health of the services they depend on.

1. **Create New** → Name "Customer Portal Status", Slug `customer-portal`, Description "Real-time status of customer-facing services", **Is Public = true**.
2. On **Manage**, create a Container Group "API Services" and drag in the relevant HTTP checks ("Login API", "Dashboard API", "Reports API").
3. Create a Container Group "Web Services" and drag in "Customer Portal", "Documentation Site".
4. **Save Changes**, then **View**, and share `https://your-domain.com/status/customer-portal`.

Use generic, customer-friendly names ("Customer Portal", not `web-prod-01`), group by visible services rather than infrastructure, and keep the description clear and reassuring.

### Internal team dashboard

Show the team the health of all internal systems.

1. **Create New** → Name "Platform Team — Production Monitoring", Slug `platform-prod`, leave **Is Public = false**.
2. On **Manage**, create a Dynamic Group "All Production Checks" — Tags = `production`, Sort by Status (descending, failing first).
3. Create a Container Group "Critical Services" and drag in the most critical checks.
4. **Save Changes** and share `…/status/platform-prod` (requires login).

Use detailed technical names internal teams understand, dynamic groups for auto-categorization, and keep it private.

### Environment-specific pages (recommended: dynamic groups)

One page per environment, each with a Dynamic Group filtered by the environment tag, so new checks appear automatically as you tag them:

1. Create "Production Status" (slug `production`) with a Dynamic Group, Tags = `production`, Sort = Status.
2. Repeat for staging and development.

Result: `/status/production`, `/status/staging`, `/status/development`, each self-maintaining. (The manual alternative is a Container Group you drag checks into — but you'd have to update it by hand.)

### Per customer / tenant

1. Tag checks per customer — e.g. Customer A's DB and API get `customer-a, database` / `customer-a, api`.
2. Create a status page per customer ("Customer A Status", slug `customer-a`).
3. Add a Dynamic Group filtered by Tags = `customer-a`.
4. Toggle **Is Public** per sharing needs.

One URL per customer (`/status/customer-a`), auto-updating as you add customer-specific checks, with isolated visibility.

### By severity / priority

Show critical services first, less important ones below.

1. Create "System Status" (slug `system`).
2. Container Group "Critical Services" at the top — drag in critical checks.
3. Container Group "Standard Services" below; "Monitoring & Tools" at the bottom (optionally collapsed by default).

Or use Dynamic Groups filtered by `critical` / `standard` / `monitoring` tags, each sorted by Status so failures surface first.

---

## Accessibility

The public page is built for accessibility: color-coded status indicators paired with text alternatives (not color alone), proper heading hierarchy (h1/h2/h3), ARIA labels on status indicators, keyboard-accessible interactive elements with a logical tab order and visible focus, and an ARIA live region so screen readers announce auto-refresh updates.

## Performance

Each check on a page costs a status query, and the 20-second auto-refresh adds load (public pages can have many concurrent viewers). For large pages, use groups (default lower-priority ones to collapsed), aim for **<50 checks per page**, and split by environment or service. There is no caching layer today, and the 20s refresh interval is fixed.

---

## Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| `/status/{slug}` returns 404 | Wrong slug, page deleted, or slug changed — verify on the Status Pages list |
| Wrong checks shown | Container: open Manage and remove them. Dynamic: edit the group's filter (tags/agent/type) |
| Dynamic group missing checks | The checks aren't tagged as expected — fix tags on the Checks page; the group re-populates |
| Overall status looks wrong | It's derived from current results — confirm checks are running and agents online; remove disabled/non-reporting checks from the page |
| Can't make a page public | It was created private — **Edit** → toggle **Is Public** → Save; verify in an incognito window |
| Auto-refresh not updating | JS disabled or HTMX blocked — check the browser console/network tab; hard-refresh; disable ad blockers |
| Changes don't save | Network/DB error during save — check for UI errors and server logs, disable interfering extensions, retry |

---

## API

`Authorization: Bearer {token}` (except a public page by slug); full schemas at `/docs`.

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/v1/status-pages` | List (`skip`, `limit`, `is_public`, `search`) |
| `GET` | `/api/v1/status-pages/slug/{slug}` | Get one (no auth if public) — checks/groups populated |
| `POST` | `/api/v1/status-pages` | Create |
| `PATCH` | `/api/v1/status-pages/{id}` | Update metadata or `items` layout |
| `DELETE` | `/api/v1/status-pages/{id}` | Delete (204) |

**List response:**

```json
{
  "items": [
    {
      "id": "uuid",
      "name": "Production Services",
      "slug": "production",
      "description": "Production infrastructure status",
      "is_public": true,
      "config": {},
      "items": [],
      "created_at": "2024-01-15T10:30:00Z",
      "updated_at": "2024-01-20T14:22:00Z"
    }
  ],
  "total": 5, "skip": 0, "limit": 50
}
```

**Create body:**

```json
{ "name": "Production Services", "slug": "production",
  "description": "Production infrastructure status", "is_public": true,
  "config": {}, "items": [] }
```

**Update the layout** (`PATCH`) with an ordered `items` list of checks and groups:

```json
{ "items": [
    { "type": "check", "check_id": "uuid-1", "order": 0 },
    { "type": "group", "name": "API Services", "order": 1, "checks": ["uuid-2", "uuid-3"] }
] }
```

---

## Related

- [Dashboard](dashboard.md) — the full internal monitoring view
- [Checks](checks.md) — the checks shown on status pages, and their tags
- [Alerts](alerts.md) — alerting for the services on a status page
